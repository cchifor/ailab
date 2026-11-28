"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const { runCli, readPredecessor } = require("./owner-phase-a-cli.cjs");

async function fixture(t, handler) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "relay-socket-"));
  fs.chmodSync(directory, 0o700);
  const socketPath = path.join(directory, "session.sock");
  const server = http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(directory, { recursive: true });
  });
  return { directory, socketPath, fetch: createSessionFetch(socketPath) };
}

test("dedicated control socket receives exact lease paths without retrying on general socket", async (t) => {
  const generalSeen = [],
    controlSeen = [];
  const general = await fixture(t, (req, res) => {
    generalSeen.push([req.method, req.url]);
    req.resume();
    res.end("{}");
  });
  const control = await fixture(t, (req, res) => {
    controlSeen.push([req.method, req.url]);
    req.resume();
    res.writeHead(403);
    res.end("{}");
  });
  const fetch = createSessionFetch(general.socketPath, control.socketPath);
  const session = "/api/agent-sessions/11111111-1111-4111-8111-111111111111";
  for (const [method, target] of [
    ["GET", session + "/control"],
    ["POST", session + "/control"],
    ["DELETE", session + "/control/22222222-2222-4222-8222-222222222222"],
  ]) {
    const response = await fetch(target, {
      method,
      ...(method === "POST" ? { body: "{}" } : {}),
    });
    assert.equal(response.status, 403);
  }
  for (const target of [
    session,
    session + "/permissions",
    session + "/control/other",
  ])
    assert.equal((await fetch(target)).status, 200);
  assert.equal(controlSeen.length, 3);
  assert.deepEqual(
    generalSeen,
    [session, session + "/permissions", session + "/control/other"].map((p) => [
      "GET",
      p,
    ]),
  );
  assert.throws(
    () => createSessionFetch(general.socketPath, general.socketPath),
    /Distinct/,
  );
  assert.throws(() =>
    createSessionFetch(
      general.socketPath,
      path.join(general.directory, "absent"),
    ),
  );
});

test(
  "a pending general request does not prevent a control request",
  { timeout: 2000 },
  async (t) => {
    let entered;
    const waiting = new Promise((resolve) => {
      entered = resolve;
    });
    const general = await fixture(t, (req) => {
      req.resume();
      entered();
    });
    const control = await fixture(t, (req, res) => {
      req.resume();
      res.end("{}");
    });
    const fetch = createSessionFetch(general.socketPath, control.socketPath);
    const abort = new AbortController();
    const pending = assert.rejects(
      fetch("/api/agent-launches", { signal: abort.signal }),
      /request failed/,
    );
    try {
      await waiting;
      const response = await fetch(
        "/api/agent-sessions/11111111-1111-4111-8111-111111111111/control",
      );
      assert.equal(response.status, 200);
    } finally {
      abort.abort();
      await pending;
    }
  },
);

test("Unix HTTP preserves path/body and does not forward credentials or response cookies", async (t) => {
  const seen = [];
  const f = await fixture(t, (req, res) => {
    let body = "";
    req.on("data", (part) => {
      body += part;
    });
    req.on("end", () => {
      seen.push({
        path: req.url,
        method: req.method,
        headers: req.headers,
        body,
      });
      res.writeHead(200, {
        "content-type": "application/json",
        "set-cookie": "synthetic=never-return",
      });
      res.end('{"ok":true}');
    });
  });
  const result = await f.fetch("/api/agent-launches?after=1", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: '{"operationId":"synthetic"}',
  });
  assert.equal(await result.text(), '{"ok":true}');
  assert.equal(result.headers.get("set-cookie"), null);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].path, "/api/agent-launches?after=1");
  assert.equal(seen[0].body, '{"operationId":"synthetic"}');
  assert.equal(seen[0].headers.cookie, undefined);
  assert.equal(seen[0].headers.authorization, undefined);
  assert.equal(seen[0].headers["transfer-encoding"], undefined);
});

test("invalid targets/methods/headers/bounds never reach socket server", async (t) => {
  let calls = 0;
  const f = await fixture(t, (req, res) => {
    calls++;
    res.end();
  });
  for (const target of [
    "https://user:secret@relay.chifor.me/api/x",
    "//relay/api/x",
    "/api/../x",
    "/api/%2e/x",
    "/api/x#f",
    "/api/x\\y",
  ]) {
    assert.throws(() => f.fetch(target));
  }
  for (const init of [
    { method: "PUT" },
    { headers: { cookie: "secret" } },
    { headers: { authorization: "secret" } },
    { method: "POST", body: "x".repeat(65537) },
    { method: "POST", body: "{" },
    { method: "POST", body: "null" },
    { method: "POST", body: "[]" },
    { body: "{}" },
  ])
    assert.throws(() => f.fetch("/api/x", init));
  const aborted = new AbortController();
  aborted.abort();
  assert.throws(() => f.fetch("/api/x", { signal: aborted.signal }));
  assert.equal(calls, 0);
});

test("redirect is returned as refusal without following Location", async (t) => {
  let calls = 0;
  const f = await fixture(t, (req, res) => {
    calls++;
    res.writeHead(302, { location: "http://127.0.0.1:1/secret" });
    res.end();
  });
  const result = await f.fetch("/api/x");
  assert.equal(result.status, 302);
  assert.equal(result.ok, false);
  assert.equal(calls, 1);
});

test("abort while body streams closes request; truncated and oversized bodies refuse", async (t) => {
  const controller = new AbortController();
  const f = await fixture(t, (req, res) => {
    if (req.url === "/api/abort") {
      res.write("{");
      setImmediate(() => controller.abort());
    } else if (req.url === "/api/truncated") {
      res.writeHead(200, { "content-length": 100 });
      res.write("{");
      setImmediate(() => res.destroy());
    } else res.end(Buffer.alloc(2 * 1024 * 1024 + 1));
  });
  await assert.rejects(
    f.fetch("/api/abort", { signal: controller.signal }),
    /Session socket request failed/,
  );
  await assert.rejects(
    f.fetch("/api/truncated"),
    /Session socket request failed/,
  );
  await assert.rejects(
    f.fetch("/api/oversized"),
    /Session socket request failed/,
  );
});

test("whole request timeout includes headers and body", async (t) => {
  const f = await fixture(t, (req, res) => {
    res.write("{");
  });
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const result = f.fetch("/api/stall");
  const rejected = assert.rejects(result, /Session socket request failed/);
  t.mock.timers.tick(30000);
  await rejected;
});

test("CLI refuses missing collector and invalid absolute deadlines before any HTTP", async (t) => {
  let calls = 0;
  const f = await fixture(t, (req, res) => {
    calls++;
    res.end();
  });
  for (const deadline of [
    String(Date.now() + 60000),
    "Infinity",
    String(Date.now() - 1),
    String(Date.now() + 2700001),
  ]) {
    const lines = [];
    assert.equal(
      await runCli({
        argv: [
          "--predecessor",
          path.join(f.directory, "absent"),
          "--output",
          path.join(f.directory, "new"),
        ],
        env: { RUNNER_DEADLINE: deadline, RELAY_SESSION_SOCKET: f.socketPath },
        writeLine: (line) => {
          lines.push(JSON.parse(line));
        },
      }),
      1,
    );
    assert.equal(lines.length, 1);
    assert.equal(lines[0].ok, false);
    assert.equal(fs.existsSync(path.join(f.directory, "new")), false);
  }
  assert.equal(calls, 0);
});

test("predecessor is exact private UTF8; symlink/hardlink/oversize refuse", async (t) => {
  const f = await fixture(t, (req, res) => res.end());
  const file = path.join(f.directory, "report.json");
  const bytes = '{ "synthetic": true }\n';
  fs.writeFileSync(file, bytes, { mode: 0o600 });
  assert.equal(readPredecessor(file), bytes);
  const link = path.join(f.directory, "link");
  fs.symlinkSync(file, link);
  assert.throws(() => readPredecessor(link));
  fs.linkSync(file, path.join(f.directory, "hard"));
  assert.throws(() => readPredecessor(file));
  const large = path.join(f.directory, "large");
  fs.writeFileSync(large, Buffer.alloc(1024 * 1024 + 1), { mode: 0o600 });
  assert.throws(() => readPredecessor(large));
});
