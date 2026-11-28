"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto, createHash } = require("node:crypto");
const fs = require("node:fs"),
  path = require("node:path"),
  Module = require("node:module");
const a = require("./owner-phase-a.js");
const b = require("./owner-phase-b.js");
const c = require("./owner-active-drain.cjs");
const NOW = Date.parse("2026-11-27T22:00:00Z");
const ID = (n) => "11111111-1111-4111-8111-" + String(n).padStart(12, "0");
const clone = (v) => JSON.parse(JSON.stringify(v));
const digest = (v) => createHash("sha256").update(a.canonical(v)).digest("hex");
const SHA = "a".repeat(64),
  SOURCE = "1".repeat(40);
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
// Explicit synthetic prerequisite; no authenticity or live qualification claim.
function input() {
  const value = {
    version: 1,
    runId: ID(1),
    phaseId: ID(2),
    account: "synthetic-claude",
    model: "synthetic-sonnet",
    window: { startsAt: NOW, expiresAt: NOW + 900000 },
    phaseAReportSha256: SHA,
    phaseBReportSha256: SHA,
    coldReportSha256: SHA,
    pairedReportSha256: SHA,
    exclusiveWindowSha256: SHA,
  };
  const report = {
    version: 1,
    kind: "claude-phase-c",
    status: "phase-c-passed",
    phaseCCompleted: true,
    relay: b.PINS.hub,
    router: SOURCE,
    inputs: { ...value, runId: ID(70), phaseId: ID(71) },
    rows: [1, 2, 3, 4].map((n) => ({ rowId: "P" + n, state: "passed" })),
    scope: { ...scope(), runId: ID(70), phaseId: ID(74) },
    claudeCredential: { definitionHash: b.PROFILE.definitionHash },
    fences: [71, 74].map((phase) => ({
      runId: ID(70),
      phaseId: ID(phase),
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
        sessionId: ID(72),
        nativeSessionId: ID(73),
      },
      { kind: "closed", sessionId: ID(72) },
    ],
  };
  value.phaseCReportRaw = JSON.stringify(report);
  value.phaseCReportSha256 = createHash("sha256")
    .update(value.phaseCReportRaw)
    .digest("hex");
  return value;
}

function releaseFixture(release) {
  const filename = path.join(__dirname, "owner-active-drain.cjs");
  const raw = fs.readFileSync(filename, "utf8"),
    token = "const QUALIFIED_ROUTER_RELEASE = null;";
  assert.equal(raw.split(token).length, 2);
  const transformed = raw.replace(
    token,
    "const QUALIFIED_ROUTER_RELEASE = " + JSON.stringify(release) + ";",
  );
  const compiled = new Module(filename, module);
  compiled.filename = filename;
  compiled.paths = Module._nodeModulePaths(__dirname);
  compiled._compile(transformed, filename);
  return {
    core: compiled.exports,
    proof: {
      rawSha256: createHash("sha256").update(raw).digest("hex"),
      transformedSha256: createHash("sha256").update(transformed).digest("hex"),
      substitutions: 1,
      release,
      qualifiedLiveBundle: false,
    },
  };
}
function credentialFixture() {
  const key = scope().keyId,
    id = ID(5),
    connection = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8";
  const row = {
    id,
    binding_id: b.PROFILE.binding,
    generation: 1,
    router_key_id: key,
    namespace: "default",
    remote_acknowledged: true,
    revoke_requested: false,
    cleanup_pending: false,
    failure_code: null,
    retire_after: null,
    state: "ready",
    expires_at: new Date(NOW + 900000).toISOString(),
  };
  const binding = {
    id: b.PROFILE.binding,
    enabled: true,
    canRenew: false,
    state: "ready",
    connection_id: connection,
    host_id: HOST,
    execution_identity: "c4",
    grant_spec: {
      routes: ["canary-pi-claude"],
      access: ["subscription"],
      lifetimeSeconds: 86400,
    },
    revision: 1,
    generation: 1,
    current_credential_id: id,
    pending_credential_id: null,
  };
  const detail = {
    credential: {
      id,
      bindingId: binding.id,
      connectionId: connection,
      generation: 1,
      routerKeyId: key,
      namespace: "default",
      state: "ready",
      remoteAcknowledged: true,
      revokeRequested: false,
      cleanupPending: false,
      failureCode: null,
      retireAfter: null,
      expiresAt: row.expires_at,
    },
    binding: {
      id: binding.id,
      hostId: HOST,
      executionIdentity: "c4",
      enabled: true,
      canRenew: false,
      state: "ready",
      revision: 1,
      generation: 1,
      currentCredentialId: id,
      pendingCredentialId: null,
    },
    consumers: [],
  };
  return {
    inventory: { bindings: [binding], credentials: [row], consumers: [] },
    detail,
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

// Primed component fixture: actual fixed row methods and inherited HTTP flow,
// synthetic services, observer and component clock. No Runner overrides.
function fixture({
  withRead = false,
  earlyComplete = false,
  lateWitness = false,
  lostSettings = false,
  foreignSettings = false,
  unknownTokens = false,
  sameGeneration = false,
  runtimeChanged = false,
  coldArmLost = false,
  stopFailure = false,
  coldCounterDrift = false,
  observerFailure = false,
  postCasRead = false,
  closeFailure = false,
  missingWitness = false,
  cancelled = false,
  wrongColdText = false,
  neverComplete = false,
  completeDuringRead = false,
} = {}) {
  let clock = NOW,
    seq = 0,
    counter = 0,
    cursor = 0,
    queued = [],
    waiting;
  let activeGeneration = null,
    heldCommand = null,
    releaseAfterWitness = false;
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
      observedMonotonicNs: "0",
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
    openRequests: activeGeneration ? 1 : 0,
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
  function generation(tool = false, hold = false) {
    assert(fence && fence.state === "active");
    assert.equal(
      fence.selection.providerGeneration,
      config.providerRuntime.providerGeneration,
    );
    assert(fence.attempts - fence.baselineAttempts < fence.phaseLimit);
    assert(fence.attempts - fence.rowStartAttempts < 2);
    fence.attempts++;
    if (unknownTokens) fence.tokenUsageUnknown++;
    const mode = config.values.warmSessions ? "warm" : "cold";
    const child = {
      id: ID(100 + ++counter),
      pid: 200 + counter,
      ppid: 100,
      startTicks: String(2000 + counter),
    };
    worker = child;
    live.push({
      pid: child.pid,
      ppid: 100,
      startTicks: child.startTicks,
      rssBytes: 123456,
    });
    const active = {
      requestId: "request-" + fence.attempts,
      attemptId: "attempt-" + fence.attempts,
      mode,
      turn: 1,
      reused: false,
      identity: child,
      accountId: runner.scope.account,
      providerGeneration: config.providerRuntime.providerGeneration,
    };
    const emit = (phase, sequence) =>
      events.push({
        ...active,
        phase,
        sequence,
        observedMonotonicNs: (BigInt(++clock - NOW) * 1000000n).toString(),
        ...(phase === "terminal" ? { outcome: "reaped" } : {}),
      });
    emit("dispatch", 1);
    if (mode === "warm") emit("fetch-invoked", 2);
    activeGeneration = { active, emit };
    fence.openRequests = 1;
    pulse();
    if (!hold) completeGeneration();
  }
  function completeGeneration() {
    if (!activeGeneration) return;
    const { active, emit } = activeGeneration;
    if (active.mode === "warm") emit("native-complete", 3);
    emit("terminal", active.mode === "warm" ? 4 : 2);
    trace.push({
      action: "native-terminal",
      generation: active.providerGeneration,
    });
    activeGeneration = null;
    fence.openRequests = 0;
    reap(active.identity);
    settled.push(active.requestId);
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
        : { command: "pwd" },
    );
    const p = {
      id,
      session_id: session.id,
      generation: 1,
      state: "pending",
      delivery: "pending",
      expires_at: new Date(clock + 30000).toISOString(),
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
    const counting = fence.activeRowId === "D1";
    generation(false, counting && !earlyComplete);
    if (counting && !earlyComplete) heldCommand = cmd;
    else completeCommand(cmd);
  }
  function completeCommand(cmd) {
    completeGeneration();
    cmd.state = "completed";
    cmd.receipt = {
      status: "completed",
      result: { stopReason: cancelled ? "cancelled" : "end_turn" },
    };
    const row = c.ROWS.find((r) => r.text === cmd.text);
    timeline.push({
      sequence: String(++seq),
      event: {
        kind: "update",
        type: "agent_message_chunk",
        text:
          row.id === "D1"
            ? "1\n2\n3"
            : wrongColdText
              ? "wrong"
              : row.expectedText,
      },
    });
    heldCommand = null;
    pulse();
  }
  const env = {
    origin: a.ORIGIN,
    executionDeadline: NOW + 900000,
    crypto: webcrypto,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
      pulse();
      await flush();
    },
    load: () => null,
    save: (s) => snapshots.push(clone(s)),
    ask: async (_title, _instructions, validate) =>
      validate ? validate(true) : true,
    fetch: async (url, init) => {
      clock += 2;
      if (releaseAfterWitness && heldCommand && !neverComplete) {
        releaseAfterWitness = false;
        if (postCasRead) {
          // A permission can arrive after the active native request completes.
          // It must not be approved or dispatch a generation in the new registry.
          const cmd = heldCommand;
          completeGeneration();
          const id = ID(300),
            toolId = "post-cas-read";
          permissions.set(id, {
            id,
            session_id: session.id,
            generation: 1,
            state: "pending",
            delivery: "pending",
            expires_at: new Date(clock + 30000).toISOString(),
            cmd: cmd.operation_id,
            read: true,
            context: {
              title: "Allow mcp__relay__relay_messages_read?",
              description: JSON.stringify({
                channelId: a.COMMUNICATION_CHANNEL,
                limit: 10,
              }),
              toolCallId: toolId,
              options: [
                { optionId: "yes", kind: "allow_once", name: "Allow once" },
                { optionId: "no", kind: "reject_once", name: "Deny once" },
              ],
            },
          });
          toolEvents("mcp__relay__relay_messages_read", "in_progress", toolId);
        } else completeCommand(heldCommand);
      }
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
          if (withRead) requestPermission(cmd, true);
          else finish(cmd);
        } else {
          cmd.state = "completed";
          cmd.receipt = { status: "completed", result: { cancelSent: true } };
          if (body.kind === "cancel" && heldCommand) {
            completeGeneration();
            heldCommand.state = "completed";
            heldCommand.receipt = {
              status: "completed",
              result: { stopReason: "cancelled" },
            };
            heldCommand = null;
          }
          if (body.kind === "close") {
            session.state = "closed";
            lease = null;
          }
        }
        out = cmd;
      } else if (u.pathname.includes("/operations/"))
        out = { command: commands.get(suffix) };
      else if (suffix === "timeline") {
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
            finish(cmd);
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
        if (observerFailure)
          throw Error("Synthetic observer transport failure");
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
      read: async () => {
        if (
          completeDuringRead &&
          runner.disableStarted &&
          config.values.warmSessions &&
          heldCommand
        )
          completeCommand(heldCommand);
        if (!config.values.warmSessions && heldCommand && lateWitness)
          completeCommand(heldCommand);
        const out = {
          ...clone(config),
          active: clone(config.values),
          providerRuntime: {
            ...config.providerRuntime,
            observedAt: clock,
            observedMonotonicNs: missingWitness
              ? null
              : (BigInt(clock - NOW) * 1000000n).toString(),
          },
        };
        if (!config.values.warmSessions && heldCommand)
          releaseAfterWitness = true;
        return out;
      },
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
          assert.equal(
            fence.state,
            "active",
            "Old fence survives settings CAS",
          );
          assert.equal(
            fence.openRequests,
            1,
            "CAS occurs during admitted request",
          );
          trace.push({ action: "disable-active", fenceId: fence.fenceId });
        }
        if (!v.values.warmSessions && coldCounterDrift) fence.attempts++;
        if (foreignSettings) config.values.maxRunning++;
        if (lostSettings && settingsCalls.length === 2)
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
          kind: v.rows[0].id === "D1" ? "warm" : "cold",
          body: clone(plan),
        });
        trace.push({
          action: "arm",
          generation: v.selection.providerGeneration,
          cap: v.phaseLimit,
        });
        if (coldArmLost && v.rows[0].id === "D2")
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
        if (closeFailure) throw Error("Close response unavailable");
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
  const runner = c.createClaudeActiveDrainRunner(env, input(), ports);
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
  async function twoRows() {
    await begin();
    const warm = await runner.counting();
    const cold = await runner.cold(warm);
    await runner.finishRows(cold);
    return cold;
  }
  return {
    runner,
    ports,
    begin,
    twoRows,
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
    abortWork: () => signal.abort(),
  };
}

test("public source gate refuses before any I/O", async () => {
  const f = fixture();
  await assert.rejects(f.runner.start(), /not qualified/);
  assert.equal(
    f.calls.length + f.settingsCalls.length + f.fenceCalls.length,
    0,
  );
});

test("fixed historical C source remains separate from current drain source", async (t) => {
  const config = {
    source: "2".repeat(40),
    qualificationSha256: SHA,
    prerequisiteCSource: SOURCE,
    prerequisiteCQualificationSha256: SHA,
    timing: true,
    attemptFence: true,
    monotonicWitness: true,
  };
  const { core, proof } = releaseFixture(config),
    f = fixture(),
    raw = input(),
    meta = credentialFixture(),
    calls = [];
  t.diagnostic(JSON.stringify(proof));
  const report = JSON.parse(raw.phaseCReportRaw);
  report.claudeCredential = b.credential(meta.inventory, meta.detail, NOW);
  raw.phaseCReportRaw = JSON.stringify(report);
  raw.phaseCReportSha256 = createHash("sha256")
    .update(raw.phaseCReportRaw)
    .digest("hex");
  const env = {
    ...f.env,
    fetch: async (url, init) => {
      assert.equal(init.method, "GET");
      const pathname = new URL(url, a.ORIGIN).pathname;
      calls.push(pathname);
      if (pathname === "/api/agent-profiles")
        throw Error("READONLY_PROFILE_BOUNDARY");
      const value =
        pathname === "/api/router-credentials" ? meta.inventory : meta.detail;
      return {
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        text: async () => JSON.stringify(value),
      };
    },
  };
  const ports = {
    ...f.ports,
    readiness: {
      read: async () => ({
        version: 1,
        hubSource: b.PINS.hub,
        routerSource: config.source,
        holderSource: b.PINS.holder,
        holderBinarySha256: b.PINS.binary,
        piManifestSha256: b.PINS.piManifest,
        profilesSha256: b.PINS.profiles,
        projectionSha256: SHA,
        holderId: ID(80),
        journalId: ID(81),
        nativeRecovery: 2,
        nativeStateReady: true,
        activeSessions: 0,
        pendingCommands: 0,
        admissionPaused: false,
        observedUtc: new Date(NOW).toISOString(),
        account: raw.account,
        model: raw.model,
        route: "canary-pi-claude",
        maxAttempts: 1,
        fallbacks: [],
        warmSessions: false,
        transport: "sdk-resume",
        trustVerified: true,
        cliVersion: "2.1.283",
        exclusiveWindowSha256: SHA,
        phaseBReportSha256: SHA,
        phaseCReportSha256: raw.phaseCReportSha256,
        coldReportSha256: SHA,
        pairedReportSha256: SHA,
        runtimeInstanceId: ID(3),
        deploymentId: ID(4),
        targetDeploymentId: "target-fixture",
        providerGeneration: 44,
        keyCreatedAt: NOW - 1000,
        attempts: 0,
        tokenUsageUnknown: 0,
        expectedFenceRevision: 0,
        root: scope().root,
      }),
    },
  };
  await assert.rejects(
    core.createClaudeActiveDrainRunner(env, raw, ports).start(),
    /READONLY_PROFILE_BOUNDARY/,
  );
  assert.deepEqual(calls, [
    "/api/router-credentials",
    "/api/router-credentials/keys/" + ID(5),
    "/api/agent-profiles",
  ]);
  assert.equal(JSON.parse(raw.phaseCReportRaw).router, SOURCE);
  assert.notEqual(proof.rawSha256, proof.transformedSha256);
  assert.equal(proof.qualifiedLiveBundle, false);
});

for (const missing of [
  "prerequisiteCSource",
  "prerequisiteCQualificationSha256",
])
  test(
    "release missing fixed " + missing + " refuses before I/O",
    async (t) => {
      const release = {
        source: "2".repeat(40),
        qualificationSha256: SHA,
        prerequisiteCSource: SOURCE,
        prerequisiteCQualificationSha256: SHA,
        timing: true,
        attemptFence: true,
        monotonicWitness: true,
      };
      delete release[missing];
      const { core, proof } = releaseFixture(release),
        f = fixture();
      t.diagnostic(JSON.stringify(proof));
      await assert.rejects(
        core.createClaudeActiveDrainRunner(f.env, input(), f.ports).start(),
        /not qualified/,
      );
      assert.equal(
        f.calls.length + f.settingsCalls.length + f.fenceCalls.length,
        0,
      );
    },
  );

test("caller C source cannot override compiled historical source pin", async (t) => {
  const release = {
    source: "2".repeat(40),
    qualificationSha256: SHA,
    prerequisiteCSource: "3".repeat(40),
    prerequisiteCQualificationSha256: SHA,
    timing: true,
    attemptFence: true,
    monotonicWitness: true,
  };
  const { core, proof } = releaseFixture(release),
    f = fixture();
  t.diagnostic(JSON.stringify(proof));
  await assert.rejects(
    core.createClaudeActiveDrainRunner(f.env, input(), f.ports).start(),
    /historical C source/,
  );
  assert.equal(
    f.calls.length + f.settingsCalls.length + f.fenceCalls.length,
    0,
  );
});

for (const [name, options, reason] of [
  ["already complete", { earlyComplete: true }, /completed before active CAS/],
  [
    "native completion during pre-CAS read",
    { completeDuringRead: true },
    /active generation|completed before|overlap unexercised/,
  ],
  ["post-CAS witness too late", { lateWitness: true }, /overlap unexercised/],
  ["lost disable receipt", { lostSettings: true }, /Lost mutation/],
  [
    "foreign settings",
    { foreignSettings: true },
    /Settings mutation not confirmed/,
  ],
  [
    "same generation",
    { sameGeneration: true },
    /Settings mutation not confirmed/,
  ],
  ["different runtime", { runtimeChanged: true }, /runtime changed/],
  ["post-CAS permission", { postCasRead: true }, /prompt failed/],
  ["native cancelled", { cancelled: true }, /prompt failed/],
  ["unattributed admission", { coldCounterDrift: true }, /counts differ/],
])
  test(name + " cannot become successful drain or start cold row", async () => {
    const f = fixture(options);
    try {
      await assert.rejects(async () => {
        await f.begin();
        await f.runner.counting();
      }, reason);
      if (options.earlyComplete || options.completeDuringRead)
        assert.equal(
          f.settingsCalls.length,
          1,
          "No disable CAS after observed completion",
        );
      assert.notEqual(f.runner.state.activeDrain, "observed");
      assert(!f.runner.state.rows.some((r) => r.state === "passed"));
      assert.equal(
        f.calls.filter((v) => v.body?.text === c.ROWS[1].text).length,
        0,
      );
      assert.equal(
        f.fenceCalls.filter((v) => v.action === "arm").length,
        options.foreignSettings ||
          options.sameGeneration ||
          options.runtimeChanged
          ? 0
          : 1,
      );
      if (options.postCasRead)
        assert.equal(
          f.calls.filter((v) => v.path.endsWith("/decision")).length,
          0,
        );
    } finally {
      f.runner.stopped = true;
      await f.runner.cleanup();
    }
  });

test("missing trusted clock refuses before warm settings or fence effects", async () => {
  const f = fixture({ missingWitness: true });
  await assert.rejects(f.begin(), /witness missing/);
  assert.equal(
    f.calls.length + f.settingsCalls.length + f.fenceCalls.length,
    0,
  );
});

for (const [name, options] of [
  ["lost cold arm", { coldArmLost: true }],
  ["failed warm close", { closeFailure: true }],
  ["old observer join failed", { stopFailure: true }],
])
  test(name + " never dispatches second prompt", async () => {
    const f = fixture(options);
    try {
      await assert.rejects(f.twoRows());
      assert.equal(
        f.calls.filter((v) => v.body?.text === c.ROWS[1].text).length,
        0,
      );
      assert(f.fenceCalls.filter((v) => v.action === "arm").length <= 2);
    } finally {
      f.runner.stopped = true;
      await f.runner.cleanup();
    }
  });

test("cold output mismatch remains failed without repeat", async () => {
  const f = fixture({ wrongColdText: true });
  try {
    await assert.rejects(f.twoRows(), /prompt failed/);
    assert.equal(f.calls.filter((v) => v.body?.kind === "prompt").length, 2);
  } finally {
    f.runner.stopped = true;
    await f.runner.cleanup();
  }
});

test("settled token uncertainty carries through exact closed predecessor without refunds", async () => {
  const f = fixture({ unknownTokens: true, withRead: true });
  try {
    await f.twoRows();
    assert.equal(f.runner.state.drainUsage.tokenUsageUnknown, 4);
    assert.equal(f.fence().tokenUsageUnknown, 11);
  } finally {
    await f.runner.stopMonitor();
  }
});

for (const field of ["attempts", "tokenUsageUnknown"])
  test("extra settled " + field + " prevents successor", async () => {
    const f = fixture();
    try {
      await f.begin();
      const first = await f.runner.counting();
      f.fence()[field]++;
      await assert.rejects(f.runner.cold(first), /Terminal evidence/);
      assert.equal(f.fenceCalls.filter((v) => v.action === "arm").length, 1);
    } finally {
      f.runner.stopped = true;
      await f.runner.cleanup();
    }
  });

test("cancelled host cannot send another prompt and cleanup still joins observer", async () => {
  const f = fixture();
  await f.begin();
  f.abortWork();
  await assert.rejects(f.runner.counting());
  await f.runner.cleanup();
  assert.equal(f.calls.filter((v) => v.body?.kind === "prompt").length, 0);
  assert.equal(f.runner.monitorStopped, true);
});

test("actual original window and independent exact C hash are required", () => {
  const good = input();
  assert.equal(c.inputs(good, NOW).phaseCReportSha256, good.phaseCReportSha256);
  for (const change of [
    (v) => delete v.phaseCReportSha256,
    (v) => (v.phaseCReportRaw += " "),
    (v) => (v.window.expiresAt = NOW),
    (v) => (v.window.startsAt = NOW + 1),
    (v) => (v.extraPlan = []),
    (v) => (v.runId = v.phaseId),
  ]) {
    const v = input();
    change(v);
    assert.throws(() => c.inputs(v, NOW));
  }
});

test("settings witness normalization preserves the strict selected C validator", async () => {
  const f = fixture(),
    raw = await f.ports.settings.read(),
    saved = clone(raw);
  const normalized = c.settings(raw, SOURCE);
  assert.equal(normalized.providerRuntime.observedMonotonicNs, "0");
  assert.deepEqual(raw, saved);
  for (const change of [
    (v) => delete v.providerRuntime.observedMonotonicNs,
    (v) => (v.providerRuntime.observedMonotonicNs = null),
    (v) => (v.providerRuntime.observedMonotonicNs = 0),
    (v) => (v.providerRuntime.observedMonotonicNs = "01"),
    (v) => (v.providerRuntime.observedMonotonicNs = "-1"),
    (v) => (v.providerRuntime.foreign = true),
    (v) => (v.source = "0".repeat(40)),
    (v) => (v.values.extra = true),
    (v) => (v.pending = true),
  ]) {
    const bad = clone(raw);
    change(bad);
    assert.throws(() => c.settings(bad, SOURCE));
  }
});

test("overlap requires causal ordering inside the same Router runtime and actual generations", () => {
  const before = {
      providerRuntime: {
        runtimeInstanceId: ID(3),
        providerGeneration: 2,
        observedMonotonicNs: "20",
      },
    },
    after = {
      providerRuntime: {
        runtimeInstanceId: ID(3),
        providerGeneration: 44,
        observedMonotonicNs: "30",
      },
    },
    fetch = { observedMonotonicNs: "10" },
    native = { observedMonotonicNs: "40" };
  assert.deepEqual(c.overlap(fetch, native, before, after, scope()), {
    fetchNs: "10",
    beforeNs: "20",
    afterNs: "30",
    nativeCompleteNs: "40",
  });
  for (const change of [
    (x) => (x.before.providerRuntime.runtimeInstanceId = ID(8)),
    (x) => (x.after.providerRuntime.runtimeInstanceId = ID(8)),
    (x) => (x.before.providerRuntime.providerGeneration = 3),
    (x) => (x.after.providerRuntime.providerGeneration = 2),
    (x) => (x.before.providerRuntime.observedMonotonicNs = "9"),
    (x) => (x.after.providerRuntime.observedMonotonicNs = "19"),
    (x) => (x.after.providerRuntime.observedMonotonicNs = "40"),
    (x) => (x.native.observedMonotonicNs = null),
  ]) {
    const x = clone({ before, after, fetch, native });
    change(x);
    assert.throws(() =>
      c.overlap(x.fetch, x.native, x.before, x.after, scope()),
    );
  }
});

test("Router monotonic regression cannot be repaired with a fresh wall-clock time", async () => {
  const f = fixture(),
    raw = await f.ports.settings.read();
  raw.providerRuntime.observedMonotonicNs = "200";
  f.runner.settingsObservation(raw);
  raw.providerRuntime.observedMonotonicNs = "199";
  raw.providerRuntime.observedAt++;
  assert.throws(
    () => f.runner.settingsObservation(raw),
    /clock observation regressed/,
  );
});

test("the supplied deadline cannot extend during a row", async () => {
  const f = fixture();
  await f.begin();
  f.setTime(input().window.expiresAt);
  await assert.rejects(f.runner.counting(), /expired|deadline|Stopped/i);
  assert.equal(f.calls.filter((v) => v.body?.kind === "prompt").length, 0);
  await f.runner.stopMonitor();
});

test("active request timeout cannot reset the row or acquire a successor allowance", async () => {
  const f = fixture({ neverComplete: true });
  try {
    await f.begin();
    await assert.rejects(f.runner.counting(), /prompt failed/);
    assert.equal(f.fenceCalls.filter((v) => v.action === "arm").length, 1);
    assert.equal(f.calls.filter((v) => v.body?.kind === "prompt").length, 1);
  } finally {
    f.runner.stopped = true;
    await f.runner.cleanup();
  }
});

test("observer failure remains a failure while host is active", async () => {
  const f = fixture({ observerFailure: true });
  await assert.rejects(f.begin(), /observer|stopped/i);
  assert.equal(f.runner.monitorFault, true);
  assert.equal(f.calls.filter((v) => v.body?.kind === "prompt").length, 0);
  f.runner.stopped = true;
  await f.runner.cleanup();
});

for (const [name, change] of [
  ["source", (p) => (p.relay = "0".repeat(40))],
  ["profile", (p) => (p.scope.profileId = ID(88))],
  ["A link", (p) => (p.inputs.phaseAReportSha256 = "0".repeat(64))],
  ["missing prior run UUID", (p) => delete p.inputs.runId],
  ["missing prior phase UUID", (p) => delete p.inputs.phaseId],
  ["scope run mismatch", (p) => (p.scope.runId = ID(90))],
  ["warm fence phase mismatch", (p) => (p.fences[0].phaseId = ID(90))],
  ["cold fence phase mismatch", (p) => (p.fences[1].phaseId = ID(90))],
  ["fence run mismatch", (p) => (p.fences[1].runId = ID(90))],
  ["C row", (p) => (p.rows[2].state = "intent")],
  ["open fence", (p) => (p.fences[0].openRequests = 1)],
  ["observer not joined", (p) => (p.processEpochs = [])],
  ["retained process", (p) => (p.process.processes = [{ pid: 20 }])],
  ["lease", (p) => (p.control = {})],
  ["missing close", (p) => p.evidence.pop()],
  [
    "unresolved cleanup",
    (p) => p.evidence.push({ outcomes: [{ state: "unresolved" }] }),
  ],
])
  test(
    "C prerequisite refuses " + name + " even with matching raw hash",
    () => {
      const v = input(),
        p = JSON.parse(v.phaseCReportRaw);
      change(p);
      v.phaseCReportRaw = JSON.stringify(p);
      v.phaseCReportSha256 = createHash("sha256")
        .update(v.phaseCReportRaw)
        .digest("hex");
      assert.throws(() => c.inputs(v, NOW));
    },
  );
for (const withRead of [false, true])
  test(
    "fixed 2+2 drain with actual row/HTTP methods" +
      (withRead ? " and one read per prompt" : ""),
    async () => {
      const f = fixture({ withRead });
      try {
        await f.twoRows();
        assert.deepEqual(
          f.runner.state.rows.map((r) => [r.rowId, r.state, r.generations]),
          [
            ["D1", "passed", withRead ? 2 : 1],
            ["D2", "passed", withRead ? 2 : 1],
          ],
        );
        assert.equal(f.runner.state.activeDrain, "observed");
        assert.equal(f.runner.state.drainUsage.attempts, withRead ? 4 : 2);
        assert.deepEqual(
          f.settingsCalls.map((v) => v.values.warmSessions),
          [true, false],
        );
        assert.deepEqual(
          f.fenceCalls.map((v) => v.action),
          ["arm", "close", "arm", "close"],
        );
        assert.deepEqual(
          f.fenceCalls
            .filter((v) => v.action === "arm")
            .map((v) => v.body.phaseLimit),
          [2, 2],
        );
        const off = f.trace.findIndex((v) => v.action === "disable-active"),
          end = f.trace.findIndex(
            (v, i) => i > off && v.action === "native-terminal",
          ),
          close = f.trace.findIndex((v) => v.action === "close");
        assert(off >= 0 && end > off && close > end);
        assert.equal(
          f.calls.filter((v) => v.body?.kind === "prompt").length,
          2,
        );
        assert.equal(f.runner.state.processEpochs.length, 3);
      } finally {
        await f.runner.stopMonitor();
      }
    },
  );
