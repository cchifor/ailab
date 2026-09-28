// Chromium half of tests/e2e-web-paste.sh — see there. CommonJS so NODE_PATH resolves playwright.
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const URL = process.env.E2E_URL;
const PASTES = process.env.E2E_PASTES;
const CAPTURED = process.env.E2E_CAPTURED;
const results = [];
const check = (name, ok, detail) => {
  results.push(ok);
  console.log(`  ${ok ? "ok" : "FAIL"}: ${name}${!ok && detail ? " — " + detail : ""}`);
};
const captured = () => fs.readFileSync(CAPTURED, "latin1");
const pastes = () => fs.readdirSync(PASTES).filter((f) => !f.startsWith("."));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 10000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(100);
  }
  return null;
}
const B0 = "\x1b[200~", B1 = "\x1b[201~";
// The page pastes "<path>" and then " " as two separate bracketed pastes.
const pastedPath = (suffix) => {
  const re = new RegExp(`\\x1b\\[200~(${PASTES.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")}/[^\\x1b]*${suffix})\\x1b\\[201~\\x1b\\[200~ \\x1b\\[201~`);
  const m = captured().match(re);
  return m ? m[1] : null;
};

(async () => {
  const browser = await chromium.launch();
  try {
    const anon = await browser.newContext({ ignoreHTTPSErrors: true });
    const anonResp = await (await anon.newPage()).goto(URL);
    check("no credentials -> 401", anonResp.status() === 401, `got ${anonResp.status()}`);

    const ctx = await browser.newContext({
      ignoreHTTPSErrors: true,
      httpCredentials: { username: "c4", password: process.env.E2E_PASS },
    });
    const page = await ctx.newPage();
    const resp = await page.goto(URL);
    check("LAN login -> 200", resp.status() === 200, `got ${resp.status()}`);
    const cookies = await ctx.cookies();
    check("bridge cookie issued (Secure, HttpOnly, SameSite=Strict)",
      cookies.some((c) => c.name === "dw_lan" && c.secure && c.httpOnly && c.sameSite === "Strict"));

    await page.waitForFunction(() => window.term && document.getElementById("dw-clip"), null, { timeout: 20000 });
    await page.bringToFront();
    await page.click(".xterm");
    await page.keyboard.type("hi");
    check("keystrokes reach the pane (the WebSocket works through the gate)",
      !!(await until(() => captured().includes("hi"))));

    // Behavioural: with the hook, Ctrl+V is a real browser paste — headless Chromium's clipboard is
    // empty, so it arrives as an EMPTY bracketed paste — and xterm never sends ^V (0x16).
    await page.waitForTimeout(700); // the hook is (re)applied every 500 ms
    const mark = captured().length;
    await page.keyboard.press("Control+V");
    const emptyPaste = await until(() => captured().slice(mark).includes(B0 + B1));
    check("Ctrl+V is a browser paste, never ^V", !!emptyPaste && !captured().includes("\x16"),
      JSON.stringify(captured().slice(mark)));

    // 1. Clipboard image.
    await page.evaluate(() => {
      const f = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3])], "shot.png", { type: "image/png" });
      const dt = new DataTransfer();
      dt.items.add(f);
      document.querySelector(".xterm-helper-textarea").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    });
    const shot = await until(() => pastedPath("-shot\\.png"));
    check("clipboard image uploaded and its path bracket-pasted, then a separate space", !!shot, JSON.stringify(captured().slice(-200)));
    if (shot) check("stored bytes intact", fs.readFileSync(shot).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 1, 2, 3])));

    // 2. Plain text still goes through xterm untouched.
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.setData("text/plain", "plain text paste");
      document.querySelector(".xterm-helper-textarea").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    });
    check("text paste still handled by xterm (bracketed, no upload)",
      !!(await until(() => captured().includes(B0 + "plain text paste" + B1))));

    // 3. Drag-and-drop of two files keeps their order, one path per paste.
    const before = pastes().length;
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(["first"], "one.txt", { type: "text/plain" }));
      dt.items.add(new File(["%PDF-1.4 second"], "two.pdf", { type: "application/pdf" }));
      document.querySelector(".xterm").dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
    });
    const one = await until(() => pastedPath("-one\\.txt"));
    const two = await until(() => pastedPath("-two\\.pdf"));
    check("dropped files both uploaded and pasted", !!one && !!two);
    if (one && two) check("dropped files pasted in order", captured().indexOf(one) < captured().indexOf(two));
    check("two new files in the pastes dir", pastes().length === before + 2, `${before} -> ${pastes().length}`);

    // 4. The paperclip picker.
    await page.setInputFiles('input[type="file"]', { name: "picked.csv", mimeType: "text/csv", buffer: Buffer.from("a,b\n1,2\n") });
    check("paperclip-picked file uploaded and pasted", !!(await until(() => pastedPath("-picked\\.csv"))));

    // 5. Oversize files are refused in the browser, before any upload.
    const n = pastes().length;
    await page.evaluate(() => {
      const big = new File([new Uint8Array(64 * 1024 * 1024 + 1)], "big.bin");
      const dt = new DataTransfer();
      dt.items.add(big);
      document.querySelector(".xterm-helper-textarea").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    });
    const refused = await until(() => page.evaluate(() => [...document.querySelectorAll(".dw-toast.err")].some((t) => /larger than 64 MB/.test(t.textContent))));
    check("oversize file refused with a message, nothing stored", !!refused && pastes().length === n);

    check("nothing ever sent Enter to the pane", !/[\r\n]/.test(captured().split(B0).join("").split(B1).join("").replace(/hi/, "")));
  } finally {
    await browser.close();
  }
  const failed = results.filter((r) => !r).length;
  console.log(failed ? `FAILED: ${failed} check(s)` : `PASS (${results.length} checks)`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error("FATAL:", e);
  process.exit(2);
});
