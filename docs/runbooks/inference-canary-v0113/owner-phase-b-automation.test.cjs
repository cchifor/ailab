"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { EventEmitter } = require("node:events");
const { createHash } = require("node:crypto");
const Module = require("node:module");
const production = require("./owner-phase-a-automation.cjs");
const { sourceFixture } = require("./owner-phase-b-fence-fixture.cjs");
const candidate = sourceFixture();
// Candidate-only source pins come from the explicit shared fixture proof.
// Host and CLI bytes are unchanged; dependencies remain the actual modules.
function loadHostModule(name, bindings) {
  const filename = path.join(__dirname, name);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(__dirname);
  const originalRequire = mod.require.bind(mod);
  mod.require = (dependency) =>
    Object.hasOwn(bindings, dependency)
      ? bindings[dependency]
      : originalRequire(dependency);
  mod._compile(fs.readFileSync(filename, "utf8"), filename);
  return mod.exports;
}
const hostModule = loadHostModule("owner-phase-a-automation.cjs", {
  "./owner-phase-a.js": candidate.a,
  "./owner-phase-b.js": candidate.b,
});
const { createClaudePhaseBAutomation } = hostModule;
const { runPhaseBCli, completedPhaseA } = loadHostModule(
  "owner-phase-b-cli.cjs",
  {
    "./owner-phase-a-automation.cjs": hostModule,
  },
);

// Synthetic structure only; no live report or account authority is implied.
function aReport() {
  return JSON.stringify({
    version: 1,
    status: "phase-a-passed",
    phaseACompleted: true,
    replacement: { carriedPrompts: 1, carriedAttempts: 2 },
    predecessor: { raw: "synthetic-original" },
    runs: [
      ["codex", false],
      ["qwen", false],
      ["codex", true],
    ].map(([provider, effort]) => ({
      provider,
      effort,
      status: "passed-with-live-wire-unobserved",
    })),
  });
}
function inputs() {
  return {
    version: 1,
    account: "synthetic-claude",
    model: "synthetic-model",
    lifecycleRunId: "11111111-1111-4111-8111-111111111111",
    effortRunId: "22222222-2222-4222-8222-222222222222",
    window: { startsAt: Date.now() - 1000, expiresAt: Date.now() + 60000 },
  };
}
function fixture(t, changes = {}) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "phase-b-host-"));
  fs.chmodSync(parent, 0o700);
  t.after(() => fs.rmSync(parent, { recursive: true }));
  const directory = path.join(parent, "run");
  let calls = 0;
  const options = {
    directory,
    phaseInputs: inputs(),
    phaseAReportSha256: completedPhaseA(aReport()),
    attemptFence: Object.fromEntries(
      ["inspect", "arm", "advance", "close", "readOperation"].map((name) => [
        name,
        async () => {
          calls++;
          throw Error("No fence operation expected before readiness");
        },
      ]),
    ),
    authenticatedFetch: async () => {
      calls++;
      throw Error("No HTTP expected");
    },
    collectCheckpoint: async () => {
      throw Error("No valid evidence supplied");
    },
    cleanupTimeoutMs: 1000,
    ...changes,
  };
  return { parent, directory, options, calls: () => calls };
}
function snapshots(directory) {
  return fs
    .readdirSync(directory)
    .filter((n) => /^state-\d+\.json$/.test(n))
    .sort()
    .map((n) => JSON.parse(fs.readFileSync(path.join(directory, n), "utf8")));
}

test("checked-in B release gate remains closed even with all adapter methods linked", async (t) => {
  const f = fixture(t, {
    collectCheckpoint: () => {
      assert.fail("Unqualified production source must not collect evidence");
    },
  });
  const host = production.createClaudePhaseBAutomation(f.options);
  await assert.rejects(host.run(), /Automation stopped/);
  assert.equal(f.calls(), 0);
  assert.match(snapshots(f.directory).at(-1).reason, /qualif|release/i);
  assert.equal(candidate.proof.qualifiedLiveBundle, false);
});

test("B keeps immutable inputs and A hash, then rejects missing readiness before any HTTP", async (t) => {
  let f;
  f = fixture(t, {
    onSnapshot: (value) => {
      assert.deepEqual(
        snapshots(f.directory).at(-1),
        value,
        "callback follows durable save",
      );
    },
  });
  const before = JSON.stringify(f.options.phaseInputs);
  const host = createClaudePhaseBAutomation(f.options);
  f.options.phaseInputs.account = "changed-after-creation";
  await assert.rejects(host.run(), /Automation stopped/);
  assert.equal(f.calls(), 0);
  const bytes = fs.readFileSync(path.join(f.directory, "phase-b-inputs.json"));
  const saved = JSON.parse(bytes);
  assert.deepEqual(saved.inputs, JSON.parse(before));
  assert.equal(saved.phaseAReportSha256, completedPhaseA(aReport()));
  assert.equal(
    fs.readFileSync(path.join(f.directory, "phase-b-inputs.sha256"), "utf8"),
    createHash("sha256").update(bytes).digest("hex") + "\n",
  );
  assert.equal(
    fs.existsSync(path.join(f.directory, "predecessor.json")),
    false,
  );
  assert.equal(snapshots(f.directory).at(-1).status, "stopped");
  assert.equal(snapshots(f.directory).at(-1).kind, "claude-phase-b");
  await assert.rejects(host.run(), /single-use/);
  assert.throws(() => createClaudePhaseBAutomation(f.options), /EEXIST/);
});

test("B refuses A bootstrap hooks, bad references and oversized deadlines before creating a store", (t) => {
  const f = fixture(t);
  const cases = [
    { predecessorRaw: aReport() },
    { readPredecessorEvidence: () => {} },
    { predecessorEvidencePin: {} },
    { renewalPrelude: () => {} },
    { observeDeployment: () => {} },
    { phaseAReportSha256: "bad" },
    { maxDurationMs: 1800001 },
    { deadline: NaN },
    { attemptFence: undefined },
    { attemptFence: { inspect: async () => ({}) } },
    { phaseInputs: { ...inputs(), window: { startsAt: 0, expiresAt: 1 } } },
  ];
  for (const change of cases) {
    assert.throws(() =>
      createClaudePhaseBAutomation({ ...f.options, ...change }),
    );
    assert.equal(fs.existsSync(f.directory), false);
  }
  assert.equal(f.calls(), 0);
});

test("B Stop cancels the actual Runner readiness collector without bootstrap writes", async (t) => {
  let entered;
  const waiting = new Promise((resolve) => {
    entered = resolve;
  });
  const f = fixture(t, {
    collectCheckpoint: ({ context, signal }) => {
      assert.equal(context.kind, "claude-readiness");
      return new Promise((_, reject) => {
        signal.addEventListener("abort", () => reject(Error("cancelled")), {
          once: true,
        });
        entered();
      });
    },
  });
  const host = createClaudePhaseBAutomation(f.options);
  const result = host.run();
  const rejected = assert.rejects(result, /Automation stopped/);
  await waiting;
  await host.stop();
  await rejected;
  assert.equal(f.calls(), 0);
  assert.equal(snapshots(f.directory).at(-1).status, "stopped");
});

test("B retains unresolved collector cleanup and forbids another run", async (t) => {
  let entered;
  const waiting = new Promise((resolve) => {
    entered = resolve;
  });
  const f = fixture(t, {
    cleanupTimeoutMs: 20,
    collectCheckpoint: () => {
      entered();
      return new Promise(() => {});
    },
  });
  const host = createClaudePhaseBAutomation(f.options);
  const result = host.run();
  const rejected = assert.rejects(result, /cleanup unresolved/);
  await waiting;
  await assert.rejects(host.stop(), /Cleanup unresolved/);
  await rejected;
  assert.equal(
    snapshots(f.directory).at(-1).automationHost.state,
    "cleanup-unresolved",
  );
  assert.equal(f.calls(), 0);
  assert.equal(fs.existsSync(path.join(f.directory, "run.lock")), true);
});

test("B earlier window deadline stops a pending collector", async (t) => {
  const selected = inputs();
  selected.window.expiresAt = Date.now() + 100;
  const f = fixture(t, {
    phaseInputs: selected,
    deadline: Date.now() + 10000,
    collectCheckpoint: ({ signal }) =>
      new Promise((_, reject) => {
        signal.addEventListener("abort", () => reject(Error("deadline")), {
          once: true,
        });
      }),
  });
  const host = createClaudePhaseBAutomation(f.options);
  const started = Date.now();
  await assert.rejects(host.run(), /Automation stopped/);
  assert(Date.now() - started < 1500);
  assert.equal(f.calls(), 0);
});

test("A reference validates retained completion/accounting structure without rewriting bytes", () => {
  const raw = aReport();
  assert.equal(
    completedPhaseA(raw),
    createHash("sha256").update(raw).digest("hex"),
  );
  for (const change of [
    { status: "stopped" },
    { phaseACompleted: false },
    { runs: [] },
    { replacement: { carriedPrompts: 0, carriedAttempts: 0 } },
    { predecessor: null },
  ])
    assert.throws(() =>
      completedPhaseA(JSON.stringify({ ...JSON.parse(raw), ...change })),
    );
});

test("B CLI without collector refuses before file or socket access", async () => {
  const lines = [];
  const code = await runPhaseBCli({
    argv: [
      "--inputs",
      "/missing/input",
      "--phase-a-report",
      "/missing/report",
      "--output",
      "/missing/run",
    ],
    env: {},
    writeLine: (line) => lines.push(JSON.parse(line)),
  });
  assert.equal(code, 1);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].lastDurableSnapshot, null);
});

test(
  "actual B CLI SIGTERM preserves A bytes and removes signal listeners with no API request",
  {
    skip: process.platform !== "linux",
  },
  async (t) => {
    const f = fixture(t);
    const inputFile = path.join(f.parent, "input.json");
    const reportFile = path.join(f.parent, "a.json");
    fs.writeFileSync(inputFile, JSON.stringify(f.options.phaseInputs), {
      mode: 0o600,
    });
    fs.writeFileSync(reportFile, aReport(), { mode: 0o600 });
    const socket = path.join(f.parent, "session.sock");
    let requests = 0;
    const server = http.createServer((req, res) => {
      requests++;
      res.end("{}");
    });
    await new Promise((resolve) => server.listen(socket, resolve));
    t.after(() => new Promise((resolve) => server.close(resolve)));
    const signals = new EventEmitter();
    const lines = [];
    const code = await runPhaseBCli({
      argv: [
        "--inputs",
        inputFile,
        "--phase-a-report",
        reportFile,
        "--output",
        f.directory,
      ],
      env: {
        RELAY_SESSION_SOCKET: socket,
        RUNNER_DEADLINE: String((Date.now() + 60000) / 1000),
      },
      signals,
      attemptFence: f.options.attemptFence,
      writeLine: (line) => lines.push(JSON.parse(line)),
      collectCheckpoint: ({ context, signal }) => {
        assert.equal(context.kind, "claude-readiness");
        return new Promise((_, reject) => {
          signal.addEventListener("abort", () => reject(Error("cancelled")), {
            once: true,
          });
          signals.emit("SIGTERM");
        });
      },
    });
    assert.equal(code, 1);
    assert.equal(requests, 0);
    assert.equal(fs.readFileSync(reportFile, "utf8"), aReport());
    assert(lines.some((line) => line.type === "snapshot"));
    assert.equal(lines.at(-1).ok, false);
    assert.equal(signals.listenerCount("SIGTERM"), 0);
    assert.equal(signals.listenerCount("SIGINT"), 0);
  },
);
