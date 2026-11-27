"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { EventEmitter } = require("node:events");
const { createHash } = require("node:crypto");
const b = require("./owner-phase-b.js");
const {
  createClaudePhaseCAutomation,
} = require("./owner-phase-a-automation.cjs");
const { runPhaseCCli } = require("./owner-phase-c-cli.cjs");

function inputs() {
  const now = Date.now();
  return {
    version: 1,
    runId: "11111111-1111-4111-8111-111111111111",
    phaseId: "22222222-2222-4222-8222-222222222222",
    account: "synthetic-claude",
    model: "synthetic-sonnet",
    window: { startsAt: now - 1000, expiresAt: now + 60000 },
    phaseAReportSha256: "a".repeat(64),
    coldReportSha256: "b".repeat(64),
    pairedReportSha256: "c".repeat(64),
    exclusiveWindowSha256: "d".repeat(64),
    phaseBReportRaw: JSON.stringify({
      version: 1,
      relay: b.PINS.hub,
      router: b.PINS.router,
      kind: "claude-phase-b",
      status: "phase-b-passed",
      phaseBCompleted: true,
      inputs: { account: "synthetic-claude", model: "synthetic-sonnet" },
      runs: [false, true].map((effort) => ({
        provider: "claude",
        effort,
        status: "passed-with-live-wire-unobserved",
      })),
    }),
  };
}
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-c-host-"));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true }));
  const calls = [],
    snapshots = [];
  const refuse = (name) => async () => {
    calls.push(name);
    throw Error("Disabled candidate must not call adapters");
  };
  const claudePorts = Object.fromEntries(
    Object.entries({
      readiness: ["read"],
      process: ["start", "next", "stop"],
      settings: ["read", "replace"],
      fence: ["arm", "inspect", "advance", "close"],
    }).map(([group, methods]) => [
      group,
      Object.fromEntries(
        methods.map((method) => [method, refuse(`${group}.${method}`)]),
      ),
    ]),
  );
  return {
    root,
    calls,
    snapshots,
    options: {
      directory: path.join(root, "run"),
      phaseInputs: inputs(),
      claudePorts,
      authenticatedFetch: refuse("fetch"),
      collectCheckpoint: refuse("checkpoint"),
      onSnapshot: (state) => snapshots.push(state),
      cleanupTimeoutMs: 200,
    },
  };
}

test("named C host preserves exact B report and refuses the unqualified release before traffic", async (t) => {
  const f = fixture(t),
    original = JSON.stringify(f.options.phaseInputs);
  const host = createClaudePhaseCAutomation(f.options);
  f.options.phaseInputs.phaseBReportRaw = "caller changed after construction";
  await assert.rejects(host.run(), /stopped/);
  assert.deepEqual(f.calls, []);
  assert.equal(
    fs.readFileSync(
      path.join(f.options.directory, "phase-c-inputs.json"),
      "utf8",
    ),
    original,
  );
  assert.equal(
    fs
      .readFileSync(
        path.join(f.options.directory, "phase-c-inputs.sha256"),
        "utf8",
      )
      .trim(),
    createHash("sha256").update(original).digest("hex"),
  );
  assert(fs.existsSync(path.join(f.options.directory, "run.lock")));
  await assert.rejects(host.run(), /single-use/);
});

test("C refuses A bootstrap and incomplete service adapters before creating a run", (t) => {
  const f = fixture(t);
  assert.throws(
    () =>
      createClaudePhaseCAutomation({ ...f.options, predecessorRaw: "ignored" }),
    /bootstrap/,
  );
  delete f.options.claudePorts.fence.close;
  assert.throws(() => createClaudePhaseCAutomation(f.options), /methods/);
  assert(!fs.existsSync(f.options.directory));
  assert.deepEqual(f.calls, []);
});

test("C refuses incomplete B, extra service methods and excessive deadlines before dispatch", (t) => {
  const f = fixture(t);
  assert.throws(() =>
    createClaudePhaseCAutomation({
      ...f.options,
      phaseInputs: { ...f.options.phaseInputs, phaseBReportRaw: "{}" },
    }),
  );
  assert.throws(
    () => createClaudePhaseCAutomation({ ...f.options, maxDurationMs: 900001 }),
    /fifteen/,
  );
  f.options.claudePorts.settings.arbitrary = async () => {};
  assert.throws(() => createClaudePhaseCAutomation(f.options), /methods/);
  assert(!fs.existsSync(f.options.directory));
  assert.deepEqual(f.calls, []);
});

test("standalone C CLI without reviewed adapters refuses before any file or socket use", async () => {
  const records = [];
  const code = await runPhaseCCli({
    argv: [
      "--inputs",
      "/not-present",
      "--phase-b-report",
      "/not-present",
      "--output",
      "/not-present",
    ],
    env: {},
    writeLine: (line) => records.push(JSON.parse(line)),
    signals: new EventEmitter(),
  });
  assert.equal(code, 1);
  assert.equal(records.length, 1);
  assert.equal(records[0].ok, false);
  assert.equal(records[0].lastDurableSnapshot, null);
});

test("actual C CLI reads private inputs but retains the source qualification refusal", async (t) => {
  const f = fixture(t),
    values = { ...f.options.phaseInputs };
  const report = values.phaseBReportRaw;
  delete values.phaseBReportRaw;
  const inputFile = path.join(f.root, "input.json"),
    reportFile = path.join(f.root, "b.json");
  fs.writeFileSync(inputFile, JSON.stringify(values), { mode: 0o600 });
  fs.writeFileSync(reportFile, report, { mode: 0o600 });
  const signals = new EventEmitter(),
    records = [];
  const socketPath = path.join(f.root, "unused.sock");
  let connections = 0;
  const server = net.createServer((socket) => {
    connections++;
    socket.destroy();
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const code = await runPhaseCCli({
    argv: [
      "--inputs",
      inputFile,
      "--phase-b-report",
      reportFile,
      "--output",
      f.options.directory,
    ],
    env: {
      RELAY_SESSION_SOCKET: socketPath,
      RUNNER_DEADLINE: String((Date.now() + 30000) / 1000),
    },
    collectCheckpoint: f.options.collectCheckpoint,
    claudePorts: f.options.claudePorts,
    signals,
    writeLine: (line) => records.push(JSON.parse(line)),
  });
  assert.equal(code, 1);
  assert.deepEqual(f.calls, []);
  assert.equal(connections, 0);
  assert.equal(records.at(-1).ok, false);
  assert.equal(signals.listenerCount("SIGTERM"), 0);
  assert.equal(signals.listenerCount("SIGINT"), 0);
  assert.equal(fs.readFileSync(reportFile, "utf8"), report);
  assert.equal(
    JSON.parse(
      fs.readFileSync(path.join(f.options.directory, "phase-c-inputs.json")),
    ).phaseBReportRaw,
    report,
  );
});
