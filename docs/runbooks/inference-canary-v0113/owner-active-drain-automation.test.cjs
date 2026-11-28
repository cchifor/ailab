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
  createClaudeActiveDrainAutomation,
} = require("./owner-phase-a-automation.cjs");
const { runActiveDrainCli } = require("./owner-active-drain-cli.cjs");

const id = (n) => "11111111-1111-4111-8111-" + String(n).padStart(12, "0");
const sha = (value) => createHash("sha256").update(value).digest("hex");
function inputs() {
  const now = Date.now();
  const context = {
    account: "synthetic-claude",
    model: "synthetic-sonnet",
    phaseAReportSha256: "a".repeat(64),
    phaseBReportSha256: "b".repeat(64),
    coldReportSha256: "c".repeat(64),
    pairedReportSha256: "d".repeat(64),
  };
  // Synthetic completed-report shape, matching the retained C projection.
  // This tests the disabled entry boundary; it grants no runtime qualification.
  const phaseCReportRaw = JSON.stringify({
    version: 1,
    relay: b.PINS.hub,
    router: "1".repeat(40),
    kind: "claude-phase-c",
    status: "phase-c-passed",
    phaseCCompleted: true,
    completed: false,
    inputs: { ...context, runId: id(10), phaseId: id(11) },
    rows: [1, 2, 3, 4].map((n) => ({ rowId: `P${n}`, state: "passed" })),
    scope: {
      runId: id(10),
      phaseId: id(14),
      routerSource: "1".repeat(40),
      hubSource: b.PINS.hub,
      account: context.account,
      model: context.model,
      profileId: b.PROFILE.id,
      profileRevision: 1,
      bindingId: b.PROFILE.binding,
    },
    claudeCredential: { definitionHash: b.PROFILE.definitionHash },
    fences: [id(11), id(14)].map((phaseId) => ({
      runId: id(10),
      phaseId,
      state: "closed",
      openRequests: 0,
      staleOpenRequests: 0,
      unattributed: 0,
    })),
    process: { processes: [] },
    processEpochs: [{ transition: "final-drained-and-joined" }],
    evidence: [
      {
        kind: "launch",
        profileId: b.PROFILE.id,
        sessionId: id(12),
        nativeSessionId: id(13),
      },
      { kind: "closed", sessionId: id(12) },
    ],
  });
  return {
    version: 1,
    runId: id(1),
    phaseId: id(2),
    ...context,
    window: { startsAt: now - 1000, expiresAt: now + 60000 },
    phaseCReportSha256: sha(phaseCReportRaw),
    phaseCReportRaw,
    exclusiveWindowSha256: "e".repeat(64),
  };
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-drain-host-"));
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
      deadline: Date.now() + 30000,
      claudePorts,
      authenticatedFetch: refuse("fetch"),
      collectCheckpoint: refuse("checkpoint"),
      onSnapshot: (state) => snapshots.push(state),
      cleanupTimeoutMs: 200,
    },
  };
}

test("named drain host preserves the pinned raw C report and refuses the unqualified release before traffic", async (t) => {
  const f = fixture(t),
    original = JSON.stringify(f.options.phaseInputs);
  const host = createClaudeActiveDrainAutomation(f.options);
  f.options.phaseInputs.phaseCReportRaw = "caller mutation";
  f.options.phaseInputs.window.expiresAt = Date.now() + 900000;
  await assert.rejects(host.run(), /stopped/);
  assert.deepEqual(f.calls, []);
  const saved = path.join(f.options.directory, "active-drain-inputs.json");
  assert.equal(fs.readFileSync(saved, "utf8"), original);
  assert.equal(fs.statSync(saved).mode & 0o777, 0o600);
  assert.equal(
    fs
      .readFileSync(
        path.join(f.options.directory, "active-drain-inputs.sha256"),
        "utf8",
      )
      .trim(),
    sha(original),
  );
  assert(fs.existsSync(path.join(f.options.directory, "run.lock")));
  await assert.rejects(host.run(), /single-use/);
  assert.throws(
    () =>
      createClaudeActiveDrainAutomation({
        ...f.options,
        phaseInputs: JSON.parse(original),
      }),
    /EEXIST/,
  );
  assert.equal(fs.readFileSync(saved, "utf8"), original);
});

test("drain requires an explicit deadline within the supplied window and caller budget before creating a store", (t) => {
  const f = fixture(t);
  for (const change of [
    { deadline: undefined },
    { deadline: Date.now() - 1 },
    { deadline: f.options.phaseInputs.window.expiresAt + 1 },
    { deadline: "1795825600000" },
    { maxDurationMs: 1 },
    { maxDurationMs: 2700001 },
  ])
    assert.throws(
      () => createClaudeActiveDrainAutomation({ ...f.options, ...change }),
      /deadline|budget/,
    );
  assert(!fs.existsSync(f.options.directory));
  assert.deepEqual(f.calls, []);
});

test("drain rejects mismatched predecessor bytes, missing pins and incomplete C before any adapter", (t) => {
  const f = fixture(t);
  const changed = {
    ...f.options.phaseInputs,
    phaseCReportRaw: f.options.phaseInputs.phaseCReportRaw + "\n",
  };
  assert.throws(() =>
    createClaudeActiveDrainAutomation({ ...f.options, phaseInputs: changed }),
  );
  const unpinned = { ...f.options.phaseInputs };
  delete unpinned.phaseCReportSha256;
  assert.throws(() =>
    createClaudeActiveDrainAutomation({ ...f.options, phaseInputs: unpinned }),
  );
  const report = JSON.parse(f.options.phaseInputs.phaseCReportRaw);
  report.rows.pop();
  const raw = JSON.stringify(report);
  assert.throws(() =>
    createClaudeActiveDrainAutomation({
      ...f.options,
      phaseInputs: {
        ...f.options.phaseInputs,
        phaseCReportRaw: raw,
        phaseCReportSha256: sha(raw),
      },
    }),
  );
  assert(!fs.existsSync(f.options.directory));
  assert.deepEqual(f.calls, []);
});

test("drain refuses A bootstrap, extra clocks and incomplete or arbitrary service methods", (t) => {
  const f = fixture(t);
  assert.throws(
    () =>
      createClaudeActiveDrainAutomation({
        ...f.options,
        predecessorRaw: "ignored",
      }),
    /bootstrap/,
  );
  assert.throws(
    () =>
      createClaudeActiveDrainAutomation({
        ...f.options,
        claudePorts: { ...f.options.claudePorts, monotonicNs: () => 0n },
      }),
    /ports/,
  );
  const withoutClose = { ...f.options.claudePorts.fence };
  delete withoutClose.close;
  assert.throws(
    () =>
      createClaudeActiveDrainAutomation({
        ...f.options,
        claudePorts: { ...f.options.claudePorts, fence: withoutClose },
      }),
    /methods/,
  );
  f.options.claudePorts.settings.arbitrary = async () => {};
  assert.throws(() => createClaudeActiveDrainAutomation(f.options), /methods/);
  assert(!fs.existsSync(f.options.directory));
  assert.deepEqual(f.calls, []);
});

test("standalone drain CLI refuses unlinked adapters before file or socket access", async () => {
  const records = [];
  const code = await runActiveDrainCli({
    argv: [
      "--inputs",
      "/not-present",
      "--phase-c-report",
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

test("actual drain CLI consumes its separate pinned private C file and retains the null release gate", async (t) => {
  const f = fixture(t),
    values = { ...f.options.phaseInputs };
  const report = values.phaseCReportRaw;
  delete values.phaseCReportRaw;
  const inputFile = path.join(f.root, "inputs.json"),
    reportFile = path.join(f.root, "c.json");
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
  const code = await runActiveDrainCli({
    argv: [
      "--inputs",
      inputFile,
      "--phase-c-report",
      reportFile,
      "--output",
      f.options.directory,
    ],
    env: {
      RELAY_SESSION_SOCKET: socketPath,
      RUNNER_DEADLINE: String(f.options.deadline / 1000),
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
      fs.readFileSync(
        path.join(f.options.directory, "active-drain-inputs.json"),
      ),
    ).phaseCReportRaw,
    report,
  );
});

test("drain CLI rejects inline predecessor substitution and never manufactures a deadline", async (t) => {
  const f = fixture(t),
    records = [];
  const inputFile = path.join(f.root, "inputs.json"),
    reportFile = path.join(f.root, "c.json");
  fs.writeFileSync(inputFile, JSON.stringify(f.options.phaseInputs), {
    mode: 0o600,
  });
  fs.writeFileSync(reportFile, f.options.phaseInputs.phaseCReportRaw, {
    mode: 0o600,
  });
  for (const deadline of [undefined, String(f.options.deadline)]) {
    const code = await runActiveDrainCli({
      argv: [
        "--inputs",
        inputFile,
        "--phase-c-report",
        reportFile,
        "--output",
        f.options.directory,
      ],
      env: { RUNNER_DEADLINE: deadline },
      collectCheckpoint: f.options.collectCheckpoint,
      claudePorts: f.options.claudePorts,
      signals: new EventEmitter(),
      writeLine: (line) => records.push(JSON.parse(line)),
    });
    assert.equal(code, 1);
  }
  assert(
    records.every((r) => r.ok === false && r.lastDurableSnapshot === null),
  );
  assert(!fs.existsSync(f.options.directory));
  assert.deepEqual(f.calls, []);
});
