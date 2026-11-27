"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto, createHash } = require("node:crypto");
const a = require("./owner-phase-a.js");
const b = require("./owner-phase-b.js");
const c = require("./owner-phase-c.cjs");
const NOW = Date.parse("2026-11-27T22:00:00Z");
const ID = (n) => "11111111-1111-4111-8111-" + String(n).padStart(12, "0");
const clone = (v) => JSON.parse(JSON.stringify(v));
const digest = (v) => createHash("sha256").update(a.canonical(v)).digest("hex");
const SHA = "a".repeat(64),
  SOURCE = "1".repeat(40);
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
function input() {
  return {
    version: 1,
    runId: ID(1),
    phaseId: ID(2),
    account: "synthetic-claude",
    model: "synthetic-sonnet",
    window: { startsAt: NOW, expiresAt: NOW + 900000 },
    phaseAReportSha256: SHA,
    coldReportSha256: SHA,
    pairedReportSha256: SHA,
    exclusiveWindowSha256: SHA,
    phaseBReportRaw: JSON.stringify({
      version: 1,
      relay: b.PINS.hub,
      router: b.PINS.router,
      kind: "claude-phase-b",
      status: "phase-b-passed",
      phaseBCompleted: true,
      inputs: { account: "synthetic-claude", model: "synthetic-sonnet" },
      runs: [false, true].map((effort, i) => ({
        runId: ID(30 + i),
        provider: "claude",
        effort,
        status: "passed-with-live-wire-unobserved",
      })),
    }),
  };
}
function scope() {
  return {
    version: 1,
    runId: ID(1),
    phaseId: ID(2),
    hubSource: b.PINS.hub,
    routerSource: SOURCE,
    runtimeInstanceId: ID(3),
    deploymentId: ID(4),
    targetDeploymentId: "target-fixture",
    account: "synthetic-claude",
    model: "synthetic-sonnet",
    providerGeneration: 2,
    namespace: "default",
    principalId: "key:key_123456789abc",
    keyId: "key_123456789abc",
    keyCreatedAt: NOW - 1000,
    bindingId: b.PROFILE.binding,
    credentialId: ID(5),
    credentialGeneration: 1,
    profileId: b.PROFILE.id,
    profileRevision: 1,
    root: { pid: 100, startTicks: "1000" },
    deadline: NOW + 900000,
  };
}
function processEvent(extra = {}) {
  return {
    requestId: "req-1",
    attemptId: "attempt-1",
    sequence: 1,
    phase: "dispatch",
    mode: "warm",
    turn: 1,
    reused: false,
    identity: { id: ID(9), pid: 200, ppid: 100, startTicks: "2000" },
    observedMonotonicNs: "1000000000",
    accountId: "synthetic-claude",
    providerGeneration: 2,
    ...extra,
  };
}
function sample(extra = {}) {
  return {
    version: 1,
    source: SOURCE,
    scopeDigest: digest(scope()),
    runtimeInstanceId: ID(3),
    observedUtc: new Date(NOW).toISOString(),
    projectionSha256: SHA,
    cursor: 1,
    root: scope().root,
    coverageComplete: true,
    gaps: [],
    events: [],
    processes: [],
    absent: [],
    settledRequestIds: [],
    openRequests: 0,
    ...extra,
  };
}
function group(requestId = "req-1", start = 1000000000n, extra = {}) {
  return ["dispatch", "fetch-invoked", "native-complete", "terminal"].map(
    (phase, i) =>
      processEvent({
        requestId,
        attemptId: "attempt-" + requestId,
        sequence: i + 1,
        phase,
        observedMonotonicNs: (start + BigInt(i)).toString(),
        ...(phase === "terminal" ? { outcome: "retained" } : {}),
        ...extra,
      }),
  );
}

test("fixed four prompts exactly preserve reviewed packet and independent row caps", () => {
  // SHA256 of JSON.stringify(packet.warm indices 0,1,3,5), independently
  // calculated from the preserved reviewed packet. No workstation path in CI.
  assert.equal(
    createHash("sha256")
      .update(JSON.stringify(c.ROWS.map((r) => r.text)))
      .digest("hex"),
    "912630f665dc7e8fb79384dc7ecf41ccb0b9366330f15ff2234a5464d9db4878",
  );
  assert.deepEqual(c.PLAN.rows, [
    { id: "P1", limit: 2 },
    { id: "P2", limit: 2 },
    { id: "P3", limit: 3 },
    { id: "P4", limit: 2 },
  ]);
  assert.equal(c.PLAN.phaseLimit, 9);
});

for (const [name, mutate] of [
  [
    "arbitrary plan",
    (i) => {
      i.rows = [];
    },
  ],
  [
    "oversized window",
    (i) => {
      i.window.expiresAt++;
    },
  ],
  [
    "ancient window start",
    (i) => {
      i.window.startsAt -= 1;
    },
  ],
  [
    "future window",
    (i) => {
      i.window.startsAt++;
    },
  ],
  [
    "incomplete B",
    (i) => {
      i.phaseBReportRaw = JSON.stringify({ status: "running" });
    },
  ],
  [
    "missing A hash",
    (i) => {
      delete i.phaseAReportSha256;
    },
  ],
  [
    "unselected account",
    (i) => {
      i.account = "other";
    },
  ],
])
  test("inputs refuse " + name, () => {
    const value = input();
    mutate(value);
    assert.throws(() => c.inputs(value, NOW));
  });

for (const [name, mutate] of [
  [
    "runtime change",
    (v) => {
      v.runtimeInstanceId = ID(99);
    },
  ],
  [
    "source change",
    (v) => {
      v.source = "2".repeat(40);
    },
  ],
  [
    "observer gap",
    (v) => {
      v.gaps.push({ code: "read-denied" });
    },
  ],
  [
    "unavailable identity",
    (v) => {
      v.events[0].identity = null;
    },
  ],
  [
    "unknown process outcome",
    (v) => {
      v.events[3].outcome = "unreaped";
    },
  ],
  [
    "wall stale",
    (v) => {
      v.observedUtc = new Date(NOW - 5001).toISOString();
    },
  ],
  [
    "payload field",
    (v) => {
      v.events[0].argv = "secret";
    },
  ],
  [
    "duplicate phase",
    (v) => {
      v.events.push(v.events[0]);
    },
  ],
  [
    "missing monotonic",
    (v) => {
      delete v.events[0].observedMonotonicNs;
    },
  ],
  [
    "counter generation changed",
    (v) => {
      v.events[0].providerGeneration = 3;
    },
  ],
])
  test("process projection refuses " + name, () => {
    const value = sample({ events: group() });
    mutate(value);
    assert.throws(() => c.observation(value, scope(), NOW));
  });

test("native deltas use monotonic pairs; incomplete, reversed and overlapping groups refuse", () => {
  const [p] = c.attempts(group());
  const [n] = c.attempts(group("req-2", 5000000000n));
  assert.equal(c.exactGap(p, n, 10), "3999999999");
  assert.throws(() => c.exactGap(n, p, 10), /window/);
  assert.throws(
    () => c.attempts(group().filter((e) => e.phase !== "native-complete")),
    /incomplete/,
  );
  assert.throws(
    () => c.attempts([...group(), ...group("req-2", 1000000001n)]),
    /Overlapping/,
  );
  assert.throws(
    () => c.exactGap(p, c.attempts(group("req-2", 12000000000n))[0], 10),
    /window/,
  );
});

// Synthetic component environment, not a published-Pi/full-host qualification.
// Actual C methods and actual A HTTP/permission/operation validators are used.
// We prime an already admitted synthetic session; start() remains release-gated.
function fixture({
  withRead = false,
  reuse = true,
  slowP2 = false,
  lostSettings = false,
  foreignSettings = false,
  pwdCommand = "pwd",
  permissionLifetime = 30000,
  secondRead = false,
  delayedTimeline = false,
  unknownTokens = false,
  sameGeneration = false,
  runtimeChanged = false,
  coldArmLost = false,
  stopFailure = false,
  coldCounterDrift = false,
} = {}) {
  let clock = NOW,
    seq = 0,
    counter = 0,
    cursor = 0,
    queued = [],
    waiting;
  const calls = [],
    snapshots = [],
    settingsCalls = [],
    fenceCalls = [],
    holds = [];
  const trace = [];
  const events = [],
    live = [],
    absent = [],
    settled = [];
  const commands = new Map(),
    permissions = new Map(),
    timeline = [];
  const signal = new AbortController(),
    cleanup = new AbortController();
  let worker = null,
    retainUntil = 0,
    fence;
  const session = {
    id: ID(10),
    generation: 1,
    kind: "holder",
    connector_id: HOST,
    parent_session_id: null,
    rebinding: false,
    state: "open",
    control_epoch: 1,
  };
  let lease = {
    id: ID(11),
    generation: 1,
    expires_at: new Date(NOW + 900000).toISOString(),
    owned: true,
  };
  let config = {
    source: SOURCE,
    revision: "b".repeat(64),
    providerRuntime: {
      runtimeInstanceId: ID(3),
      providerGeneration: 2,
      observedAt: NOW,
    },
    projectionSha256: SHA,
    pending: false,
    values: {
      transport: "sdk-resume",
      warmSessions: false,
      warmIdleMs: 45000,
      maxRunning: 8,
    },
  };
  const common = () => ({
    ...sample(),
    scopeDigest: digest(runner.scope),
    cursor: ++cursor,
    observedUtc: new Date(clock).toISOString(),
    events: clone(events),
    processes: clone(live),
    absent: clone(absent),
    settledRequestIds: [...settled],
  });
  function pulse() {
    if (worker && retainUntil && clock >= retainUntil) reap(worker);
    const value = common();
    if (waiting) {
      const w = waiting;
      waiting = null;
      w.resolve(value);
    } else queued.push(value);
  }
  function reap(child) {
    const at = live.findIndex(
      (p) => p.pid === child.pid && p.startTicks === child.startTicks,
    );
    if (at >= 0) live.splice(at, 1);
    if (
      !absent.some(
        (p) => p.pid === child.pid && p.startTicks === child.startTicks,
      )
    )
      absent.push({
        pid: child.pid,
        startTicks: child.startTicks,
        status: "absent-at-read",
      });
    if (worker?.pid === child.pid) {
      worker = null;
      retainUntil = 0;
    }
  }
  async function flush() {
    await new Promise(setImmediate);
  }
  function generation(tool = false) {
    assert(fence && fence.state === "active");
    assert.equal(
      fence.selection.providerGeneration,
      config.providerRuntime.providerGeneration,
      "Server rejects obsolete provider generation",
    );
    assert(fence.attempts - fence.baselineAttempts < fence.phaseLimit);
    const cap = c.ROWS.find((r) => r.id === fence.activeRowId).limit;
    assert(
      fence.attempts - fence.rowStartAttempts < cap,
      "Synthetic upstream fence refuses excess before generation",
    );
    fence.attempts++;
    if (unknownTokens) fence.tokenUsageUnknown++;
    const mode = config.values.warmSessions ? "warm" : "cold";
    const reused = mode === "warm" && worker && reuse;
    if (!reused) {
      if (worker) reap(worker);
      worker = {
        id: ID(100 + ++counter),
        pid: 200 + counter,
        ppid: 100,
        startTicks: String(2000 + counter),
        turn: 0,
      };
    }
    const turn = ++worker.turn;
    const identity = {
      id: worker.id,
      pid: worker.pid,
      ppid: worker.ppid,
      startTicks: worker.startTicks,
    };
    if (!live.some((p) => p.pid === worker.pid))
      live.push({
        pid: worker.pid,
        ppid: worker.ppid,
        startTicks: worker.startTicks,
        rssBytes: 123456,
      });
    const requestId = "request-" + fence.attempts,
      attemptId = "attempt-" + fence.attempts;
    for (const [i, phase] of (mode === "warm"
      ? ["dispatch", "fetch-invoked", "native-complete", "terminal"]
      : ["dispatch", "terminal"]
    ).entries()) {
      clock += 1;
      events.push({
        requestId,
        attemptId,
        sequence: i + 1,
        phase,
        mode,
        turn,
        reused: Boolean(reused),
        identity,
        observedMonotonicNs: (BigInt(clock - NOW) * 1000000n).toString(),
        accountId: runner.scope.account,
        providerGeneration: config.providerRuntime.providerGeneration,
        ...(phase === "terminal"
          ? { outcome: tool || mode === "cold" ? "reaped" : "retained" }
          : {}),
      });
    }
    pulse(); // Observe the actual identity alive before any disappearance.
    if (tool || mode === "cold") reap(identity);
    else retainUntil = clock + 10000;
    settled.push(requestId);
    pulse();
  }
  function toolEvents(name, status, id) {
    timeline.push({
      sequence: String(++seq),
      event: {
        kind: "update",
        type: status === "in_progress" ? "tool_call" : "tool_call_update",
        title: name,
        status,
        toolCallId: id,
      },
    });
  }
  function requestPermission(cmd, read) {
    generation(true);
    const id = ID(300 + permissions.size),
      toolId = "tool-" + permissions.size;
    const title = read
      ? "Allow mcp__relay__relay_messages_read?"
      : "Allow bash?";
    const description = JSON.stringify(
      read
        ? { channelId: a.COMMUNICATION_CHANNEL, limit: 10 }
        : { command: pwdCommand },
    );
    const p = {
      id,
      session_id: session.id,
      generation: 1,
      state: "pending",
      delivery: "pending",
      expires_at: new Date(clock + permissionLifetime).toISOString(),
      context: {
        title,
        description,
        toolCallId: toolId,
        options: [
          { optionId: "yes", kind: "allow_once", name: "Allow once" },
          { optionId: "no", kind: "reject_once", name: "Deny once" },
        ],
      },
      cmd: cmd.operation_id,
      read,
      created: clock,
    };
    permissions.set(id, p);
    toolEvents(
      read ? "mcp__relay__relay_messages_read" : "pwd",
      "in_progress",
      toolId,
    );
    cmd.permission = id;
  }
  function finish(cmd) {
    generation();
    cmd.state = "completed";
    cmd.receipt = { status: "completed", result: { stopReason: "end_turn" } };
    const row = c.ROWS.find((r) => r.text === cmd.text);
    timeline.push({
      sequence: String(++seq),
      event: {
        kind: "update",
        type: "agent_message_chunk",
        text: row.expectedText || "Denied.",
      },
    });
    pulse();
  }
  const env = {
    origin: a.ORIGIN,
    crypto: webcrypto,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
      pulse();
      await flush();
    },
    load: () => null,
    save: (s) => snapshots.push(clone(s)),
    ask: async (_title, _instructions, validate) => validate(true),
    fetch: async (url, init) => {
      clock += 2;
      pulse();
      await flush();
      const u = new URL(url, a.ORIGIN),
        body = init.body ? JSON.parse(init.body) : null,
        suffix = u.pathname.split("/").at(-1);
      calls.push({ path: u.pathname, method: init.method, body });
      let out;
      if (init.method === "POST" && body?.operationId)
        assert(
          snapshots
            .at(-1)
            .operations.some((o) => o.body?.operationId === body.operationId),
          "Intent persisted BEFORE HTTP",
        );
      if (u.pathname === "/api/agent-sessions/" + session.id) out = session;
      else if (suffix === "control") {
        if (init.method === "POST")
          lease = {
            ...lease,
            expires_at: new Date(clock + 60000).toISOString(),
          };
        out = init.method === "POST" ? lease : { lease };
      } else if (suffix === "permissions")
        out = [...permissions.values()].map((p) => ({
          id: p.id,
          state: p.state,
        }));
      else if (suffix === "commands") {
        const cmd = {
          id: ID(500 + commands.size),
          operation_id: body.operationId,
          session_id: session.id,
          generation: 1,
          kind: body.kind,
          state: "accepted",
          text: body.text,
        };
        commands.set(body.operationId, cmd);
        if (body.kind === "prompt") {
          if (slowP2 && fence.activeRowId === "P2") clock += 21000;
          if (withRead) requestPermission(cmd, true);
          else if (fence.activeRowId === "P3") requestPermission(cmd, false);
          else finish(cmd);
        } else {
          cmd.state = "completed";
          cmd.receipt = { status: "completed", result: { cancelSent: true } };
          if (body.kind === "close") {
            session.state = "closed";
            lease = null;
          }
        }
        out = cmd;
      } else if (u.pathname.includes("/operations/"))
        out = { command: commands.get(suffix) };
      else if (suffix === "timeline") {
        if (delayedTimeline && fence.activeRowId === "P2") clock += 11000;
        out = {
          sessionId: session.id,
          generation: 1,
          text: true,
          events: timeline,
          prompts: [...commands.values()].filter((v) => v.kind === "prompt"),
          nextAfter: null,
          nextPromptsAfter: null,
        };
      } else if (u.pathname.startsWith("/api/agent-permissions/")) {
        const p = permissions.get(u.pathname.split("/")[3]);
        assert(p);
        if (suffix === "decision") {
          p.state = "resolved";
          p.delivery = "accepted";
          p.decision = body.outcome;
          if (body.outcome.outcome !== "cancelled") {
            assert.equal(body.outcome.optionId, p.read ? "yes" : "no");
            if (!p.read) {
              holds.push(clock - p.created);
              assert(clock - p.created >= 15000 && clock - p.created < 30000);
            }
            toolEvents(
              p.read ? "mcp__relay__relay_messages_read" : "pwd",
              p.read ? "completed" : "failed",
              p.context.toolCallId,
            );
            const cmd = commands.get(p.cmd);
            if (p.read && secondRead) requestPermission(cmd, true);
            else if (p.read && fence.activeRowId === "P3")
              requestPermission(cmd, false);
            else finish(cmd);
          }
        }
        out = p;
      } else throw Error("Unexpected synthetic HTTP " + u.pathname);
      return {
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        text: async () => JSON.stringify(out),
      };
    },
  };
  const ports = {
    signal: signal.signal,
    cleanupSignal: cleanup.signal,
    monotonicNs: () => BigInt(clock - NOW) * 1000000n,
    readiness: {
      read: async () => {
        throw Error("Not used by primed component fixture");
      },
    },
    process: {
      start: async () => {
        events.length = absent.length = settled.length = 0;
        queued = [];
        return common();
      },
      next: async ({ signal }) => {
        signal.throwIfAborted();
        if (queued.length) return queued.shift();
        return new Promise((resolve, reject) => {
          const abort = () => {
            waiting = null;
            reject(Error("Observer cancelled"));
          };
          waiting = {
            resolve: (v) => {
              signal.removeEventListener("abort", abort);
              resolve(v);
            },
          };
          signal.addEventListener("abort", abort, { once: true });
        });
      },
      stop: async () => {
        if (stopFailure) throw Error("Observer stop failed");
        queued = [];
      },
    },
    settings: {
      read: async () => ({
        ...clone(config),
        active: clone(config.values),
        providerRuntime: { ...config.providerRuntime, observedAt: clock },
      }),
      replace: async (v) => {
        assert(
          snapshots
            .at(-1)
            .settingsOperations.some(
              (o) => o.intentId === v.intentId && o.state === "intent",
            ),
        );
        assert.equal(v.expectedRevision, config.revision);
        settingsCalls.push(
          clone({
            kind: snapshots.at(-1).settingsOperations.at(-1).kind,
            values: v.values,
          }),
        );
        trace.push({ action: "settings", warm: v.values.warmSessions });
        config.values = clone(v.values);
        config.revision = digest({ n: settingsCalls.length });
        if (!sameGeneration)
          config.providerRuntime.providerGeneration =
            settingsCalls.length === 1 ? 19 : 44;
        if (runtimeChanged) config.providerRuntime.runtimeInstanceId = ID(888);
        if (!v.values.warmSessions && worker) {
          reap(worker);
          pulse();
        }
        if (!v.values.warmSessions && coldCounterDrift) fence.attempts++;
        if (foreignSettings) config.values.maxRunning++;
        if (lostSettings && settingsCalls.length === 1)
          throw Error("Lost mutation response");
        return { intentId: v.intentId, projectionSha256: SHA };
      },
    },
    fence: {
      arm: async (v) => {
        assert(snapshots.at(-1).subplans.length === 2);
        assert(
          snapshots
            .at(-1)
            .fenceOperations.some(
              (o) => o.operationId === v.operationId && o.state === "intent",
            ),
        );
        assert.equal(
          v.selection.providerGeneration,
          config.providerRuntime.providerGeneration,
        );
        if (fence) {
          assert.equal(fence.state, "closed");
          assert.equal(v.predecessorFenceId, fence.fenceId);
          assert.equal(v.expectedFenceRevision, fence.revision);
          assert.equal(v.expectedAttempts, fence.attempts);
          assert.equal(v.expectedUnknownUsage, fence.tokenUsageUnknown);
        } else {
          assert.equal(v.expectedFenceRevision, 0);
          assert.equal(v.expectedAttempts, 100);
          assert.equal(v.expectedUnknownUsage, 0);
        }
        const {
          scope: _,
          signal: ___,
          expectedAttempts,
          expectedUnknownUsage,
          expectedFenceRevision,
          operationId,
          scopeDigest: __,
          ...plan
        } = v;
        fence = {
          ...clone(plan),
          coveredFrom: v.keyCreatedAt,
          state: "active",
          activeRowId: v.rows[0].id,
          revision: expectedFenceRevision + 1,
          attempts: expectedAttempts,
          baselineAttempts: expectedAttempts,
          rowStartAttempts: expectedAttempts,
          tokenUsageUnknown: expectedUnknownUsage,
          baselineTokenUsageUnknown: expectedUnknownUsage,
          rowStartTokenUsageUnknown: expectedUnknownUsage,
          openRequests: 0,
          staleOpenRequests: 0,
          unattributed: 0,
        };
        fenceCalls.push({
          action: "arm",
          kind: v.rows[0].id === "P1" ? "warm" : "cold",
          body: clone(plan),
        });
        trace.push({
          action: "arm",
          generation: v.selection.providerGeneration,
          cap: v.phaseLimit,
        });
        if (coldArmLost && v.rows[0].id === "P4")
          throw Error("Cold arm response lost");
        return { ...(await ports.fence.inspect()), operationId };
      },
      inspect: async () => ({
        version: 1,
        source: SOURCE,
        scopeDigest: digest(runner.fenceContext.scope),
        projectionSha256: SHA,
        fence: clone(fence),
      }),
      advance: async (v) => {
        assert.equal(v.expectedFenceRevision, fence.revision);
        assert.equal(v.expectedAttempts, fence.attempts);
        assert.equal(v.expectedUnknownUsage, fence.tokenUsageUnknown);
        assert.equal(v.completedRowId, fence.activeRowId);
        assert.equal(
          fence.rows.findIndex((r) => r.id === v.nextRowId),
          fence.rows.findIndex((r) => r.id === v.completedRowId) + 1,
        );
        assert.equal(runner.pending, null);
        assert([...commands.values()].every((x) => x.state === "completed"));
        fenceCalls.push(
          clone({ action: "advance", from: v.completedRowId, to: v.nextRowId }),
        );
        fence.revision++;
        fence.activeRowId = v.nextRowId;
        fence.rowStartAttempts = fence.attempts;
        fence.rowStartTokenUsageUnknown = fence.tokenUsageUnknown;
        return { ...(await ports.fence.inspect()), operationId: v.operationId };
      },
      close: async (v) => {
        assert.equal(v.expectedUnknownUsage, fence.tokenUsageUnknown);
        assert.equal(v.expectedFenceRevision, fence.revision);
        assert.equal(v.expectedAttempts, fence.attempts);
        fenceCalls.push({ action: "close", id: fence.fenceId });
        trace.push({ action: "close", cap: fence.phaseLimit });
        fence.revision++;
        fence.state = "closed";
        return { ...(await ports.fence.inspect()), operationId: v.operationId };
      },
    },
  };
  const runner = c.createClaudePhaseCRunner(env, input(), ports);
  runner.scope = scope();
  runner.session = session;
  runner.lease = lease;
  runner.run = {
    provider: "claude",
    runId: input().runId,
    effort: false,
    prompts: 0,
    deadline: input().window.expiresAt,
  };
  runner.state.runs.push(runner.run);
  runner.planSegments();
  fence = unknownTokens
    ? {
        fenceId: ID(77),
        state: "closed",
        revision: 5,
        attempts: 100,
        tokenUsageUnknown: 7,
      }
    : null;
  async function begin() {
    runner.originalSettings = c.settings(await ports.settings.read(), SOURCE);
    runner.lastSettings = runner.originalSettings;
    await runner.startMonitor();
    await runner.beginWarm({
      expectedAttempts: 100,
      expectedUnknownUsage: unknownTokens ? 7 : 0,
      expectedFenceRevision: unknownTokens ? 5 : 0,
      ...(unknownTokens ? { predecessorFenceId: ID(77) } : {}),
    });
  }
  async function fourRows() {
    await begin();
    let previous;
    for (const row of c.ROWS) {
      if (row.id === "P4") {
        await runner.beginCold(previous);
        previous = undefined;
      }
      previous = await runner.runRow(row, previous);
    }
    await runner.finishRows(previous);
    return previous;
  }
  return {
    runner,
    ports,
    begin,
    fourRows,
    calls,
    snapshots,
    settingsCalls,
    fenceCalls,
    holds,
    permissions,
    commands,
    env,
    setTime: (ms) => {
      clock = ms;
    },
    advance: async (ms) => env.sleep(ms),
    config: () => config,
    fence: () => fence,
    pulse,
    trace,
  };
}

test("unpublished entry refuses before any API, settings or fence operation", async () => {
  const f = fixture();
  await assert.rejects(f.runner.start(), /not qualified/);
  assert.equal(f.calls.length, 0);
  assert.equal(f.settingsCalls.length, 0);
  assert.equal(f.fenceCalls.length, 0);
});

for (const withRead of [false, true])
  test(
    "actual four-row core with " +
      (withRead
        ? "one read each: 2/2/3/2"
        : "no reads: unused allowances retired"),
    async () => {
      const f = fixture({ withRead });
      try {
        const before = JSON.stringify(f.runner.inputs);
        await f.fourRows();
        assert.deepEqual(
          f.runner.state.rows.map((r) => r.generations),
          withRead ? [2, 2, 3, 2] : [1, 1, 2, 1],
        );
        assert.deepEqual(
          f.calls
            .filter((x) => x.body?.kind === "prompt")
            .map((x) => x.body.text),
          c.ROWS.map((r) => r.text),
        );
        assert.equal(
          f.runner.state.evidence
            .filter((e) => e.kind === "prompt")
            .reduce((sum, e) => sum + e.communicationReads, 0),
          withRead ? 4 : 0,
        );
        assert.equal(f.holds.length, 1);
        assert(f.holds[0] >= 15000 && f.holds[0] < 30000);
        assert.deepEqual(
          f.settingsCalls.map((x) => x.kind),
          ["enable", "disable"],
        );
        assert.deepEqual(f.config().values, {
          transport: "sdk-resume",
          warmSessions: false,
          warmIdleMs: 45000,
          maxRunning: 8,
        });
        assert.deepEqual(
          f.fenceCalls.filter((x) => x.action === "advance").map((x) => x.to),
          ["P2", "P3"],
        );
        assert.equal(f.runner.session.state, "closed");
        assert.equal(f.runner.lease, null);
        assert.equal(JSON.stringify(f.runner.inputs), before);
        assert.equal(f.runner.state.capacity, "unexercised");
        assert.deepEqual(f.trace, [
          { action: "settings", warm: true },
          { action: "arm", generation: 19, cap: 7 },
          { action: "close", cap: 7 },
          { action: "settings", warm: false },
          { action: "arm", generation: 44, cap: 2 },
          { action: "close", cap: 2 },
        ]);
        const [warm, cold] = f.runner.state.fences;
        assert.deepEqual(warm.rows, [
          { id: "P1", limit: 2 },
          { id: "P2", limit: 2 },
          { id: "P3", limit: 3 },
        ]);
        assert.deepEqual(cold.rows, [{ id: "P4", limit: 2 }]);
        assert.equal(warm.state, "closed");
        assert.equal(cold.state, "closed");
        assert.equal(cold.predecessorFenceId, warm.fenceId);
        assert.notEqual(warm.phaseId, cold.phaseId);
        assert.equal(warm.runId, cold.runId);
        assert.equal(warm.originalDeadline, cold.originalDeadline);
        assert.equal(warm.attempts, cold.baselineAttempts);
        assert.equal(
          cold.attempts - f.runner.state.cBaseline.attempts,
          withRead ? 9 : 5,
        );
        assert.equal(cold.phaseLimit, 2); // Unspent warm allowance cannot fund cold.
        assert.deepEqual(
          f.runner.state.processEpochs.map((e) => e.scope.providerGeneration),
          [2, 19, 44],
        );
        assert(
          f.runner.state.processEpochs[1].process.events.every(
            (e) => e.providerGeneration === 19,
          ),
        );
        assert(
          f.runner.state.processEpochs[2].process.events.every(
            (e) => e.providerGeneration === 44,
          ),
        );
        assert(
          f.snapshots.every(
            (s) => !s.settingsOperations.length || s.subplans.length === 2,
          ),
        );
      } finally {
        await f.runner.stopMonitor();
      }
    },
  );

test("P2 first-generation mismatch stops despite a correct final text", async () => {
  const f = fixture({ reuse: false });
  try {
    await f.begin();
    const p1 = await f.runner.runRow(c.ROWS[0]);
    await assert.rejects(f.runner.runRow(c.ROWS[1], p1), /first generation/);
  } finally {
    await f.runner.cleanup();
  }
  assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 2);
});

test("conservative deadline refuses P2 without dispatch or row advance", async () => {
  const f = fixture();
  try {
    await f.begin();
    const p1 = await f.runner.runRow(c.ROWS[0]);
    await f.advance(10001);
    await assert.rejects(f.runner.runRow(c.ROWS[1], p1), /pre-dispatch/);
    assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 1);
    assert.equal(f.fenceCalls.filter((x) => x.action === "advance").length, 0);
  } finally {
    await f.runner.cleanup();
  }
});

test("late actual native gap is charged then stops; no third prompt", async () => {
  const f = fixture({ slowP2: true });
  try {
    await f.begin();
    const p1 = await f.runner.runRow(c.ROWS[0]);
    await assert.rejects(f.runner.runRow(c.ROWS[1], p1));
    assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 2);
    assert.equal(f.fence().attempts, 102);
  } finally {
    await f.runner.cleanup();
  }
});

test("unknown settings result is inspected, never replayed, then separate cold cleanup", async () => {
  const f = fixture({ lostSettings: true });
  await assert.rejects(f.begin(), /Lost mutation/);
  await f.runner.cleanup();
  assert.deepEqual(
    f.settingsCalls.map((x) => x.kind),
    ["enable", "cleanup-cold"],
  );
  assert.equal(f.runner.state.settingsOperations[0].state, "uncertain");
  assert.equal(f.config().values.warmSessions, false);
  assert.equal(f.fence(), null); // No arm before successful settings confirmation.
});

test("foreign settings changes are not overwritten during cleanup", async () => {
  const f = fixture({ foreignSettings: true });
  await assert.rejects(f.begin(), /not confirmed/);
  await f.runner.cleanup();
  assert.equal(f.settingsCalls.length, 1);
  assert(
    f.runner.state.evidence
      .find((e) => e.kind === "warm-cleanup")
      .outcomes.some(
        (o) => o.resource === "settings" && o.state === "unresolved",
      ),
  );
});

test("a reused PID never establishes disappearance of the original start identity", async () => {
  const f = fixture();
  f.runner.acceptSample(
    sample({
      processes: [{ pid: 200, ppid: 100, startTicks: "2000", rssBytes: 1 }],
    }),
  );
  f.runner.acceptSample(
    sample({
      cursor: 2,
      processes: [{ pid: 200, ppid: 100, startTicks: "3000", rssBytes: 1 }],
    }),
  );
  assert.equal(f.runner.absent(processEvent().identity), false);
});

test("missed twenty-second conservative bound issues neither P3 nor P4", async () => {
  const f = fixture();
  try {
    await f.begin();
    const first = await f.runner.runRow(c.ROWS[0]);
    const second = await f.runner.runRow(c.ROWS[1], first);
    await f.advance(20001);
    await assert.rejects(f.runner.runRow(c.ROWS[2], second), /pre-dispatch/);
    assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 2);
    assert.deepEqual(
      f.fenceCalls.filter((x) => x.action === "advance").map((x) => x.to),
      ["P2"],
    );
  } finally {
    await f.runner.cleanup();
  }
});

for (const [name, options, pattern] of [
  [
    "changed command",
    { pwdCommand: "pwd; echo unexpected" },
    /command\/options/,
  ],
  ["insufficient lifetime", { permissionLifetime: 14999 }, /lifetime/],
])
  test(
    "pwd permission refuses " + name + " without a selected decision",
    async () => {
      const f = fixture(options);
      try {
        await f.begin();
        const first = await f.runner.runRow(c.ROWS[0]);
        const second = await f.runner.runRow(c.ROWS[1], first);
        await assert.rejects(f.runner.runRow(c.ROWS[2], second), pattern);
        assert.equal(
          f.calls.filter((x) => x.body?.outcome?.outcome === "selected").length,
          0,
        );
      } finally {
        await f.runner.cleanup();
      }
    },
  );

test("a second Relay read is never approved", async () => {
  const f = fixture({ withRead: true, secondRead: true });
  try {
    await f.begin();
    await assert.rejects(f.runner.runRow(c.ROWS[0]), /More than one/);
    assert.equal(
      f.calls.filter((x) => x.body?.outcome?.outcome === "selected").length,
      1,
    );
    assert.equal(f.fence().attempts, 102);
  } finally {
    await f.runner.cleanup();
  }
});

test("native evidence cannot disappear or mutate between complete samples", () => {
  const f = fixture();
  const original = sample({ events: group() });
  f.runner.acceptSample(original);
  assert.throws(
    () => f.runner.acceptSample(sample({ cursor: 2 })),
    /disappeared/,
  );
  const changed = clone(original);
  changed.cursor = 2;
  changed.events[0].observedMonotonicNs = "0";
  assert.throws(() => f.runner.acceptSample(changed), /changed/);
  assert.throws(() => f.runner.acceptSample(original), /cursor/);
});

test("controller monotonic regression stops timing rather than using wall time", () => {
  const f = fixture();
  f.runner.mono();
  f.setTime(NOW - 1);
  assert.throws(() => f.runner.mono(), /clock changed/);
});

for (const [name, change] of [
  [
    "open admission",
    (f) => {
      f.openRequests = 1;
    },
  ],
  [
    "unattributed admission",
    (f) => {
      f.unattributed = 1;
    },
  ],
  [
    "stale open admission",
    (f) => {
      f.staleOpenRequests = 1;
    },
  ],
  [
    "coverage gap",
    (f) => {
      f.coveredFrom++;
    },
  ],
  [
    "unprojected field",
    (f) => {
      f.rawKey = "synthetic-unexpected";
    },
  ],
  [
    "borrowed row",
    (f) => {
      f.attempts = 103;
    },
  ],
  [
    "changed creation",
    (f) => {
      f.keyCreatedAt++;
    },
  ],
  [
    "changed target",
    (f) => {
      f.selection.accountId = "other";
    },
  ],
  [
    "revision change",
    (f) => {
      f.revision++;
    },
  ],
])
  test("fence inspection refuses " + name, async () => {
    const f = fixture();
    try {
      await f.begin();
      await f.runner.inspectFence();
      change(f.fence());
      await assert.rejects(f.runner.inspectFence());
    } finally {
      await f.runner.stopMonitor();
    }
  });

test("settings reject unexpected payload fields, not merely retain them", async () => {
  const f = fixture();
  const value = await f.ports.settings.read();
  value.values.token = "synthetic-unexpected";
  value.active = value.values;
  assert.throws(
    () => c.settings(value, SOURCE),
    /Unexpected projection fields/,
  );
});

test("late timeline read cannot dispatch a now-expired P2 command", async () => {
  const f = fixture({ delayedTimeline: true });
  try {
    await f.begin();
    const first = await f.runner.runRow(c.ROWS[0]);
    await assert.rejects(f.runner.runRow(c.ROWS[1], first), /pre-dispatch/);
    assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 1);
    assert.equal(f.fence().activeRowId, "P2"); // Advanced, but no prompt or refund.
  } finally {
    await f.runner.cleanup();
  }
});

test("unknown TOKENS retain charged attempt accounting and exact CAS through all C rows", async () => {
  const f = fixture({ withRead: true, unknownTokens: true });
  try {
    await f.fourRows();
    assert.equal(f.fence().attempts, 109);
    assert.equal(f.fence().tokenUsageUnknown, 16);
    assert.equal(f.fence().baselineTokenUsageUnknown, 14);
    assert.equal(f.runner.state.cBaseline.tokenUsageUnknown, 7);
    assert.deepEqual(
      f.runner.state.fenceOperations
        .filter((x) => x.method === "advance")
        .map((x) => x.body.expectedUnknownUsage),
      [9, 11],
    );
  } finally {
    await f.runner.stopMonitor();
  }
});

for (const [name, options, pattern] of [
  [
    "unchanged generation after changed settings",
    { sameGeneration: true },
    /not confirmed/,
  ],
  ["replacement Router runtime", { runtimeChanged: true }, /runtime changed/],
  ["unjoined original observer", { stopFailure: true }, /Observer stop failed/],
])
  test("C refuses " + name + " before warm arm or prompt", async () => {
    const f = fixture(options);
    await assert.rejects(f.begin(), pattern);
    await f.runner.cleanup();
    assert.equal(f.fenceCalls.length, 0);
    assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 0);
  });

for (const [name, mutate] of [
  [
    "missing",
    (v) => {
      delete v.providerRuntime;
    },
  ],
  [
    "null generation",
    (v) => {
      v.providerRuntime.providerGeneration = null;
    },
  ],
  [
    "stale",
    (v) => {
      v.providerRuntime.observedAt = NOW - 5001;
    },
  ],
])
  test("generation observation refuses " + name, async () => {
    const f = fixture();
    const v = await f.ports.settings.read();
    mutate(v);
    assert.throws(() => f.runner.settingsObservation(v));
  });

async function warmThree(f) {
  await f.begin();
  let last;
  for (const row of c.ROWS.slice(0, 3)) last = await f.runner.runRow(row, last);
  return last;
}

test("closed predecessor counter drift refuses cold arm and preserves all warm evidence", async () => {
  const f = fixture({ coldCounterDrift: true });
  try {
    const last = await warmThree(f);
    await assert.rejects(f.runner.beginCold(last));
    assert.deepEqual(
      f.fenceCalls.filter((x) => x.action === "arm").map((x) => x.kind),
      ["warm"],
    );
    assert.equal(f.runner.state.fences[0].state, "closed");
    assert.equal(f.runner.state.processEpochs[1].scope.providerGeneration, 19);
    assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 3);
    assert.equal(f.runner.state.cBaseline.attempts, 100);
  } finally {
    await f.runner.cleanup();
  }
});

test("lost cold arm response reconciles that exact fence and never repeats an arm or prompt", async () => {
  const f = fixture({ coldArmLost: true });
  const last = await warmThree(f);
  await assert.rejects(f.runner.beginCold(last), /Cold arm response lost/);
  const intended = f.runner.state.fenceOperations.at(-1);
  assert.equal(intended.state, "uncertain");
  assert.equal(intended.body.fenceId, f.runner.state.subplans[1].fenceId);
  await f.runner.cleanup();
  assert.deepEqual(
    f.fenceCalls.filter((x) => x.action === "arm").map((x) => x.kind),
    ["warm", "cold"],
  );
  assert.equal(f.fence().fenceId, intended.body.fenceId);
  assert.equal(f.fence().state, "closed");
  assert.equal(f.fence().attempts, 104);
  assert.equal(f.calls.filter((x) => x.body?.kind === "prompt").length, 3);
});

test("unsettled warm predecessor cannot close, switch settings or open cold scope", async () => {
  const f = fixture();
  try {
    const last = await warmThree(f);
    f.fence().openRequests = 1;
    await assert.rejects(f.runner.beginCold(last), /unsettled/);
    assert.equal(f.settingsCalls.length, 1);
    assert.equal(f.runner.scope.providerGeneration, 19);
    assert.equal(
      f.runner.state.subplans[1].fenceId === f.fence().fenceId,
      false,
    );
  } finally {
    await f.runner.cleanup();
  }
});

test("cold plan rejects borrowed allowance, wrong predecessor and global counter reset", async () => {
  const f = fixture();
  try {
    await f.runner.beginCold(await warmThree(f));
    const valid = clone(f.fence());
    for (const alter of [
      (v) => v.phaseLimit++,
      (v) => (v.predecessorFenceId = ID(999)),
      (v) => {
        v.attempts = v.baselineAttempts = v.rowStartAttempts = 0;
      },
    ]) {
      const v = clone(valid);
      alter(v);
      assert.throws(() =>
        f.runner.fenceProjection(
          {
            version: 1,
            source: SOURCE,
            scopeDigest: digest(f.runner.scope),
            projectionSha256: SHA,
            fence: v,
          },
          "P4",
        ),
      );
    }
    assert.equal(f.runner.state.cBaseline.attempts, 100);
  } finally {
    await f.runner.cleanup();
  }
});

test("a settled extra admission cannot be hidden by closing the warm fence", async () => {
  const f = fixture();
  try {
    const last = await warmThree(f);
    f.fence().attempts++; // Still within P3 cap, but not in actual terminal evidence.
    await assert.rejects(f.runner.beginCold(last), /Terminal evidence/);
    assert.equal(f.settingsCalls.length, 1);
    assert.equal(f.fenceCalls.filter((x) => x.action === "arm").length, 1);
  } finally {
    await f.runner.cleanup();
  }
});

for (const priorIndex of [0, 1])
  for (const counter of ["attempts", "tokenUsageUnknown"])
    test(`P${priorIndex + 1} terminal ${counter} drift refuses next advance and prompt`, async () => {
      const f = fixture();
      try {
        await f.begin();
        let last;
        for (const row of c.ROWS.slice(0, priorIndex + 1))
          last = await f.runner.runRow(row, last);
        const before = clone(f.fenceCalls);
        f.fence()[counter]++; // Within the old row cap, after recorded settlement.
        await assert.rejects(
          f.runner.runRow(c.ROWS[priorIndex + 1], last),
          /Terminal evidence/,
        );
        assert.deepEqual(f.fenceCalls, before);
        assert.equal(
          f.calls.filter((x) => x.body?.kind === "prompt").length,
          priorIndex + 1,
        );
        assert.equal(f.runner.state.rows.length, priorIndex + 1);
        assert.equal(f.fence().activeRowId, c.ROWS[priorIndex].id);
      } finally {
        await f.runner.cleanup();
      }
    });
