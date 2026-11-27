"use strict";
// node --test scripts/relay-phase-a/observer-client.test.cjs: the Runner-side observer client and the entrypoint's
// deadline conversion, against a fake gate on a real Unix socket.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { createObserverClient, plain } = require("./observer-client.cjs");
const { deadlineMs } = require("./phase-a-entry.cjs");

function fakeGate(handler) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gate-"));
  const sock = path.join(dir, "observer.sock");
  const seen = [];
  const server = net.createServer((c) => {
    c.on("error", () => {}); // the client may close first (oversize, abort): not a test failure
    let buf = "";
    c.on("data", (d) => {
      buf += d;
      const nl = buf.indexOf("\n");
      if (nl === -1) return;
      const req = JSON.parse(buf.slice(0, nl));
      seen.push(req);
      handler(req, c);
    });
  });
  return new Promise((resolve) => server.listen(sock, () => resolve({ sock, seen, close: () => server.close() })));
}

test("a value is returned unchanged; instructions travel only as a hash; the signal is stripped", async () => {
  const g = await fakeGate((req, c) => c.end(JSON.stringify({ v: 1, ok: true, value: { a: 1 } }) + "\n"));
  const client = createObserverClient(g.sock);
  const ac = new AbortController();
  const v = await client.collectCheckpoint({
    name: "n", instructions: "do X", deadline: Date.now() + 5000, signal: ac.signal,
    context: { version: 1, kind: "observation", signal: ac.signal, nested: { f: () => 1, x: 2 } },
  });
  assert.deepStrictEqual(v, { a: 1 });
  const r = g.seen[0];
  assert.strictEqual(r.call, "checkpoint");
  assert.strictEqual(r.kind, "observation");
  assert.match(r.instructionsSha256, /^[0-9a-f]{64}$/);
  assert.ok(!JSON.stringify(r).includes("do X"));
  assert.deepStrictEqual(r.context, { version: 1, kind: "observation", nested: { x: 2 } });
  g.close();
});

test("refusals throw fixed words; malformed or oversized replies throw", async () => {
  for (const [reply, want] of [
    [{ v: 1, ok: false, code: "failed" }, /Observer failed/],
    [{ v: 1, ok: false, code: "host text /secret" }, /Observer refused/],
    [{ v: 2, ok: true, value: 1 }, /reply shape/],
  ]) {
    const g = await fakeGate((req, c) => c.end(JSON.stringify(reply) + "\n"));
    await assert.rejects(createObserverClient(g.sock).observeDeployment({ deadline: Date.now() + 3000 }), want);
    g.close();
  }
  const g = await fakeGate((req, c) => c.end("x".repeat(600 * 1024)));
  await assert.rejects(createObserverClient(g.sock).observeDeployment({ deadline: Date.now() + 3000 }), /too large/);
  g.close();
});

test("abort and deadline reject promptly", async () => {
  const g = await fakeGate(() => {});
  const ac = new AbortController();
  const p = createObserverClient(g.sock).observeDeployment({ deadline: Date.now() + 10000, signal: ac.signal });
  setTimeout(() => ac.abort(), 50);
  await assert.rejects(p, /aborted/);
  await assert.rejects(createObserverClient(g.sock).observeDeployment({ deadline: Date.now() + 200 }), /deadline/);
  g.close();
});

test("the predecessor projection must be a string", async () => {
  const g = await fakeGate((req, c) => c.end(JSON.stringify({ v: 1, ok: true, value: { not: "a string" } }) + "\n"));
  await assert.rejects(createObserverClient(g.sock).readPredecessorEvidence({ deadline: Date.now() + 3000 }),
    /must be a string/);
  g.close();
});

test("RUNNER_DEADLINE seconds become the 13-digit milliseconds runCli requires", () => {
  assert.strictEqual(deadlineMs({ RUNNER_DEADLINE: "1795819190.5" }), "1795819190500");
  assert.strictEqual(deadlineMs({ RUNNER_DEADLINE: "1795819190" }), "1795819190000");
  for (const bad of ["", "1795819190000", "abc", "-1", undefined]) {
    assert.throws(() => deadlineMs({ RUNNER_DEADLINE: bad }));
  }
  assert.deepStrictEqual(plain({ a: [1, { b: undefined, c: "x" }] }), { a: [1, { c: "x" }] });
  assert.throws(() => plain({ n: NaN }));
});
