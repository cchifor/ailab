"use strict";

// Fixed selected warm-C core. No network/authentication or arbitrary plan here.
const a = require("./owner-phase-a.js");
const b = require("./owner-phase-b.js");
const { createHash } = require("node:crypto");

// Deliberately unavailable until timing + attempt-fence artifacts are published,
// qualified and reviewed together. An input/port cannot override this gate.
const QUALIFIED_ROUTER_RELEASE = null;
const ROWS = Object.freeze([
  Object.freeze({
    id: "P1",
    limit: 2,
    text: "Reply exactly WARM_FIRST_OK. Do not use tools or modify files.",
    expectedText: "WARM_FIRST_OK",
  }),
  Object.freeze({
    id: "P2",
    limit: 2,
    text: "Reply exactly WARM_SECOND_OK. Do not use tools or modify files.",
    expectedText: "WARM_SECOND_OK",
  }),
  Object.freeze({
    id: "P3",
    limit: 3,
    text: "Request permission to run the shell command pwd once. Do not run any other command or modify files.",
    tool: "pwd",
    decision: "reject_once",
  }),
  Object.freeze({
    id: "P4",
    limit: 2,
    text: "Reply exactly COLD_ROLLBACK_OK. Do not use tools or modify files.",
    expectedText: "COLD_ROLLBACK_OK",
  }),
]);
const PLAN = Object.freeze({
  phaseLimit: 9,
  rows: ROWS.map(({ id, limit }) => Object.freeze({ id, limit })),
  promptLimit: 4,
  durationMs: 900000,
});
Object.freeze(PLAN.rows);
const SHA = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const LABEL = /^[A-Za-z0-9._:-]{1,200}$/;
const NS = /^(?:0|[1-9][0-9]{0,24})$/;
const hash = (v) => typeof v === "string" && SHA.test(v);
const uuid = (v) => typeof v === "string" && UUID.test(v);
const label = (v) => typeof v === "string" && LABEL.test(v);
const integer = (v) => Number.isSafeInteger(v) && v >= 0;
const clone = (v) => JSON.parse(JSON.stringify(v));
const digest = (v) => createHash("sha256").update(a.canonical(v)).digest("hex");
const eq = (x, y) => a.canonical(x) === a.canonical(y);
const planDigest = digest({ plan: PLAN, prompts: ROWS.map((r) => r.text) });
const SUBPLANS = Object.freeze([
  Object.freeze({
    kind: "warm",
    phaseLimit: 7,
    rows: Object.freeze(PLAN.rows.slice(0, 3)),
  }),
  Object.freeze({
    kind: "cold",
    phaseLimit: 2,
    rows: Object.freeze(PLAN.rows.slice(3)),
  }),
]);
function need(ok, reason) {
  if (!ok) throw Error(reason);
}
function ticks(v) {
  need(typeof v === "string" && NS.test(v), "Monotonic observation missing");
  return BigInt(v);
}
function privateKeys(v, allowed) {
  need(
    v &&
      typeof v === "object" &&
      !Array.isArray(v) &&
      Object.keys(v).every((k) => allowed.includes(k)),
    "Unexpected projection fields",
  );
}
function release() {
  const p = QUALIFIED_ROUTER_RELEASE;
  need(
    p &&
      /^[a-f0-9]{40}$/.test(p.source) &&
      hash(p.qualificationSha256) &&
      p.timing === true &&
      p.attemptFence === true,
    "Final Router timing/fence release is not qualified",
  );
  return p;
}
function inputs(value, now) {
  privateKeys(value, [
    "version",
    "runId",
    "phaseId",
    "account",
    "model",
    "window",
    "phaseAReportSha256",
    "phaseBReportRaw",
    "coldReportSha256",
    "pairedReportSha256",
    "exclusiveWindowSha256",
  ]);
  need(
    value.version === 1 &&
      uuid(value.runId) &&
      uuid(value.phaseId) &&
      value.runId !== value.phaseId &&
      label(value.account) &&
      label(value.model),
    "Fixed Claude C inputs invalid",
  );
  privateKeys(value.window, ["startsAt", "expiresAt"]);
  need(
    integer(value.window.startsAt) &&
      integer(value.window.expiresAt) &&
      value.window.startsAt <= now &&
      value.window.expiresAt > now &&
      value.window.expiresAt - value.window.startsAt <= PLAN.durationMs,
    "C window outside fifteen-minute bound",
  );
  for (const k of [
    "phaseAReportSha256",
    "coldReportSha256",
    "pairedReportSha256",
    "exclusiveWindowSha256",
  ])
    need(hash(value[k]), "C prerequisite hash missing");
  need(
    typeof value.phaseBReportRaw === "string" &&
      Buffer.byteLength(value.phaseBReportRaw) <= 1048576,
    "Actual completed B report required",
  );
  const previous = JSON.parse(value.phaseBReportRaw);
  need(
    previous.version === 1 &&
      previous.kind === "claude-phase-b" &&
      previous.relay === b.PINS.hub &&
      previous.router === b.PINS.router &&
      previous.status === "phase-b-passed" &&
      previous.phaseBCompleted === true &&
      previous.inputs?.account === value.account &&
      previous.inputs.model === value.model &&
      previous.runs?.length === 2 &&
      previous.runs.every(
        (r, i) =>
          r.provider === "claude" &&
          r.effort === Boolean(i) &&
          r.status === "passed-with-live-wire-unobserved",
      ),
    "B lifecycle/effort prerequisite incomplete",
  );
  need(
    !previous.runs.some((r) => r.runId === value.runId),
    "C cannot replay a prior run",
  );
  const { phaseBReportRaw, ...out } = value;
  return Object.freeze({
    ...clone(out),
    window: Object.freeze(clone(value.window)),
    phaseBReportSha256: createHash("sha256")
      .update(phaseBReportRaw)
      .digest("hex"),
  });
}
function identity(v) {
  privateKeys(v, ["id", "pid", "ppid", "startTicks"]);
  need(
    uuid(v.id) &&
      integer(v.pid) &&
      v.pid > 0 &&
      v.pid <= 4194304 &&
      integer(v.ppid) &&
      /^[1-9][0-9]{0,19}$/.test(v.startTicks),
    "Process identity unobserved",
  );
  return clone(v);
}
function eventKey(e) {
  return e.requestId + ":" + e.attemptId + ":" + e.phase;
}
function observation(v, scope, now) {
  privateKeys(v, [
    "version",
    "source",
    "scopeDigest",
    "runtimeInstanceId",
    "observedUtc",
    "projectionSha256",
    "cursor",
    "root",
    "coverageComplete",
    "gaps",
    "events",
    "processes",
    "absent",
    "settledRequestIds",
    "openRequests",
  ]);
  need(
    v.version === 1 &&
      v.source === scope.routerSource &&
      v.scopeDigest === digest(scope) &&
      v.runtimeInstanceId === scope.runtimeInstanceId &&
      hash(v.projectionSha256),
    "Process projection scope changed",
  );
  need(
    Number.isFinite(Date.parse(v.observedUtc)) &&
      Math.abs(now - Date.parse(v.observedUtc)) <= 5000 &&
      integer(v.cursor) &&
      v.coverageComplete === true &&
      Array.isArray(v.gaps) &&
      v.gaps.length === 0 &&
      eq(v.root, scope.root),
    "Process coverage/clock/root gap",
  );
  need(
    Array.isArray(v.events) &&
      v.events.length <= 128 &&
      Array.isArray(v.processes) &&
      v.processes.length <= 128 &&
      Array.isArray(v.absent) &&
      v.absent.length <= 128 &&
      Array.isArray(v.settledRequestIds) &&
      v.settledRequestIds.length <= 16 &&
      v.settledRequestIds.every(label) &&
      integer(v.openRequests),
    "Process projection bounds invalid",
  );
  const seen = new Set();
  for (const e of v.events) {
    privateKeys(e, [
      "requestId",
      "attemptId",
      "sequence",
      "phase",
      "mode",
      "turn",
      "reused",
      "identity",
      "outcome",
      "observedMonotonicNs",
      "accountId",
      "providerGeneration",
    ]);
    need(
      label(e.requestId) &&
        label(e.attemptId) &&
        integer(e.sequence) &&
        ["dispatch", "fetch-invoked", "native-complete", "terminal"].includes(
          e.phase,
        ) &&
        ["cold", "warm"].includes(e.mode) &&
        integer(e.turn) &&
        e.turn > 0 &&
        e.reused === e.turn > 1 &&
        e.accountId === scope.account &&
        e.providerGeneration === scope.providerGeneration,
      "Invalid process event",
    );
    identity(e.identity);
    ticks(e.observedMonotonicNs);
    need(
      e.mode !== "cold" ||
        (e.turn === 1 &&
          !e.reused &&
          ["dispatch", "terminal"].includes(e.phase)),
      "Unsupported cold timing",
    );
    need(
      e.phase === "terminal"
        ? ["retained", "reaped"].includes(e.outcome) &&
            !(e.mode === "cold" && e.outcome === "retained")
        : e.outcome === undefined,
      "Unconfirmed process disposition",
    );
    need(!seen.has(eventKey(e)), "Duplicate process phase");
    seen.add(eventKey(e));
  }
  for (const p of v.processes) {
    privateKeys(p, ["pid", "ppid", "startTicks", "rssBytes"]);
    need(
      integer(p.pid) &&
        p.pid > 0 &&
        integer(p.ppid) &&
        /^[1-9][0-9]{0,19}$/.test(p.startTicks) &&
        integer(p.rssBytes),
      "Invalid process sample",
    );
  }
  for (const p of v.absent) {
    privateKeys(p, ["pid", "startTicks", "status"]);
    need(
      integer(p.pid) &&
        p.pid > 0 &&
        /^[1-9][0-9]{0,19}$/.test(p.startTicks) &&
        p.status === "absent-at-read",
      "Invalid absence sample",
    );
  }
  need(
    new Set(v.processes.map((p) => p.pid)).size === v.processes.length &&
      new Set(v.absent.map((p) => p.pid + ":" + p.startTicks)).size ===
        v.absent.length,
    "Ambiguous process samples",
  );
  return clone(v);
}
function attempts(events, previous = new Set(), requireTerminal = true) {
  const groups = new Map();
  for (const e of events) {
    if (previous.has(e.requestId + ":" + e.attemptId)) continue;
    const key = e.requestId + ":" + e.attemptId;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const out = [];
  for (const group of groups.values()) {
    group.sort((x, y) => x.sequence - y.sequence);
    const d = group.find((e) => e.phase === "dispatch"),
      f = group.find((e) => e.phase === "fetch-invoked"),
      n = group.find((e) => e.phase === "native-complete"),
      t = group.find((e) => e.phase === "terminal");
    need(d && (!requireTerminal || t), "Incomplete process attempt");
    const ordered = [d, f, n, t].filter(Boolean);
    need(
      group.every(
        (e) =>
          eq(e.identity, d.identity) &&
          e.turn === d.turn &&
          e.mode === d.mode &&
          e.reused === d.reused,
      ) &&
        ordered.every(
          (e, i) =>
            i === 0 ||
            (e.sequence > ordered[i - 1].sequence &&
              ticks(e.observedMonotonicNs) >=
                ticks(ordered[i - 1].observedMonotonicNs)),
        ),
      "Process event order/identity changed",
    );
    if (requireTerminal && d.mode === "warm")
      need(f && n, "Native timing incomplete");
    out.push({ dispatch: d, fetch: f, native: n, terminal: t });
  }
  out.sort((x, y) =>
    ticks(x.dispatch.observedMonotonicNs) <
    ticks(y.dispatch.observedMonotonicNs)
      ? -1
      : 1,
  );
  for (let i = 1; i < out.length; i++)
    need(
      out[i - 1].terminal &&
        ticks(out[i - 1].terminal.observedMonotonicNs) <
          ticks(out[i].dispatch.observedMonotonicNs),
      "Overlapping or ambiguous generation order",
    );
  return out;
}
function sameChild(x, y) {
  return eq(x.identity, y.identity);
}
function exactGap(previous, next, seconds) {
  need(previous.native && next.fetch, "Required native/fetch pair missing");
  const delta =
    ticks(next.fetch.observedMonotonicNs) -
    ticks(previous.native.observedMonotonicNs);
  need(
    delta >= 0n && delta <= BigInt(seconds) * 1000000000n,
    "Native timing window missed",
  );
  return delta.toString();
}
function settings(v, source) {
  privateKeys(v, [
    "source",
    "revision",
    "values",
    "active",
    "pending",
    "projectionSha256",
    "providerRuntime",
  ]);
  need(
    v.source === source &&
      hash(v.revision) &&
      hash(v.projectionSha256) &&
      v.pending === false &&
      v.values &&
      eq(v.values, v.active),
    "Settings not active/observed",
  );
  privateKeys(v.providerRuntime, [
    "runtimeInstanceId",
    "providerGeneration",
    "observedAt",
  ]);
  need(
    label(v.providerRuntime.runtimeInstanceId) &&
      integer(v.providerRuntime.providerGeneration) &&
      integer(v.providerRuntime.observedAt),
    "Actual provider generation unobserved",
  );
  privateKeys(v.values, [
    "transport",
    "warmSessions",
    "warmIdleMs",
    "maxRunning",
  ]);
  need(
    v.values.transport === "sdk-resume" &&
      typeof v.values.warmSessions === "boolean" &&
      integer(v.values.warmIdleMs) &&
      v.values.warmIdleMs >= 1000 &&
      v.values.warmIdleMs <= 300000 &&
      integer(v.values.maxRunning) &&
      v.values.maxRunning > 0 &&
      v.values.maxRunning <= 32,
    "Claude settings differ",
  );
  // Full values must remain JSON data, bounded; never print credentials/errors.
  need(
    Buffer.byteLength(JSON.stringify(v.values)) <= 16384,
    "Settings size bound",
  );
  return clone(v);
}
function protectedSettings(v) {
  const { warmSessions, warmIdleMs, ...rest } = v;
  return rest;
}

class WarmRunner extends a.Runner {
  constructor(env, value, ports) {
    super(env);
    this.inputs = inputs(value, this.now());
    this.ports = ports;
    for (const [name, methods] of Object.entries({
      readiness: ["read"],
      process: ["start", "next", "stop"],
      settings: ["read", "replace"],
      fence: ["arm", "inspect", "advance", "close"],
    }))
      for (const method of methods)
        need(
          typeof ports?.[name]?.[method] === "function",
          "Required typed C port missing",
        );
    need(
      typeof ports.monotonicNs === "function" &&
        ports.signal instanceof AbortSignal &&
        ports.cleanupSignal instanceof AbortSignal,
      "Host-owned C lifetime/clock missing",
    );
    this.effectiveExpiry = this.inputs.window.expiresAt;
    this.state.kind = "claude-phase-c";
    this.state.router = null; // Never relabel B's historical Router proof.
    this.state.inputs = this.inputs;
    this.state.plan = PLAN;
    this.state.planDigest = planDigest;
    this.state.capacity = this.state.activeDrain = "unexercised";
    this.state.b0 = "preserved; never executed by this entry";
    this.state.rows = [];
    this.state.settingsOperations = [];
    this.state.fenceOperations = [];
    this.state.processEpochs = [];
    this.state.fences = [];
    this.events = new Map();
    this.seenAlive = new Set();
    this.monitorAbort = new AbortController();
    this.previousLocalTime = -1n;
  }
  mono() {
    const n = this.ports.monotonicNs();
    need(
      typeof n === "bigint" && n >= this.previousLocalTime,
      "Controller monotonic clock changed",
    );
    this.previousLocalTime = n;
    return n;
  }
  check() {
    super.check();
    need(!this.monitorFault, "Process observer failed");
    this.ports.signal.throwIfAborted();
  }
  start() {
    return this.runExclusive(() => this.startPhaseC());
  }
  replaceStopped() {
    throw Error("C cannot replace/reset a prior phase");
  }
  revokeFinal() {
    throw Error("Grant cleanup is separate from C");
  }
  credentialScopeReviewed() {
    return Boolean(this.state.claudeCredential);
  }
  async currentCredential() {
    const inv = await this.api("/api/router-credentials");
    const rows = inv?.credentials?.filter(
      (c) => c.binding_id === b.PROFILE.binding,
    );
    need(rows?.length, "Claude credential missing");
    const last = [...rows].sort((x, y) => x.generation - y.generation).at(-1);
    need(uuid(last.id), "Credential identity invalid");
    return b.credential(
      inv,
      await this.api("/api/router-credentials/keys/" + last.id),
      this.now(),
    );
  }
  async refreshBeforeLaunch() {
    this.check();
    need(
      eq(await this.currentCredential(), this.state.claudeCredential),
      "Credential changed before launch",
    );
  }
  async call(port, method, data = {}, cleaning = false) {
    if (!cleaning) this.check();
    const signal = cleaning ? this.ports.cleanupSignal : this.ports.signal;
    signal.throwIfAborted();
    const result = await this.ports[port][method]({
      ...data,
      signal,
      scope: data.scope || this.scope,
    });
    signal.throwIfAborted();
    return result;
  }
  acceptSample(raw) {
    const v = observation(raw, this.scope, this.now());
    need(
      !this.sample || v.cursor > this.sample.cursor,
      "Process sample cursor replay",
    );
    const current = new Map(v.events.map((e) => [eventKey(e), e]));
    for (const [key, old] of this.events)
      need(
        current.has(key) && eq(current.get(key), old),
        "Process evidence changed/disappeared",
      );
    this.events = current;
    for (const p of v.processes) this.seenAlive.add(p.pid + ":" + p.startTicks);
    this.sample = v;
    this.state.process = {
      cursor: v.cursor,
      projectionSha256: v.projectionSha256,
      events: clone(v.events),
      processes: clone(v.processes),
      absent: clone(v.absent),
      observedUtc: v.observedUtc,
    };
    this.save();
  }
  absent(child) {
    const id = child.pid + ":" + child.startTicks;
    return (
      this.seenAlive.has(id) &&
      this.sample?.absent.some(
        (p) => p.pid === child.pid && p.startTicks === child.startTicks,
      ) &&
      !this.sample.processes.some(
        (p) => p.pid === child.pid && p.startTicks === child.startTicks,
      )
    );
  }
  async startMonitor() {
    need(
      !this.monitorStarted || this.monitorStopped,
      "Previous observer not joined",
    );
    this.monitorAbort = new AbortController();
    this.monitorStopped = false;
    this.monitorStarted = true;
    this.acceptSample(await this.call("process", "start"));
    this.monitorTask = (async () => {
      while (!this.monitorAbort.signal.aborted) {
        const signal = AbortSignal.any([
          this.ports.signal,
          this.monitorAbort.signal,
        ]);
        const value = await this.ports.process.next({
          scope: this.scope,
          afterCursor: this.sample.cursor,
          signal,
        });
        if (signal.aborted) break;
        this.acceptSample(value);
      }
    })().catch(() => {
      if (!this.monitorAbort.signal.aborted) {
        this.monitorFault = true;
        this.stopped = true;
        this.state.status = "stopped";
        this.state.reason = "Process observer failed";
        this.save();
      }
    });
  }
  async waitFor(predicate, milliseconds, reason) {
    const end = this.mono() + BigInt(milliseconds) * 1000000n;
    for (;;) {
      this.check();
      if (predicate()) return;
      need(this.mono() < end, reason);
      await this.sleep(100);
    }
  }
  conservativeWindow(previous, seconds) {
    need(
      this.mono() - ticks(previous.startedLocalNs) <=
        BigInt(seconds) * 1000000000n,
      "Conservative pre-dispatch native bound unavailable; no next prompt",
    );
  }
  planSegments() {
    need(!this.segments, "C subplans already persisted");
    const coldPhaseId = this.crypto.randomUUID();
    this.segments = SUBPLANS.map((plan, index) =>
      Object.freeze({
        ...plan,
        phaseId: index === 0 ? this.inputs.phaseId : coldPhaseId,
        fenceId: this.crypto.randomUUID(),
        planDigest: digest({ parent: planDigest, plan }),
      }),
    );
    const ids = [
      this.inputs.runId,
      ...this.segments.flatMap((p) => [p.phaseId, p.fenceId]),
    ];
    need(
      ids.every(uuid) && new Set(ids).size === ids.length,
      "C subplan identities collide",
    );
    this.state.subplans = clone(this.segments);
    this.save(); // Both immutable plans/IDs precede any settings/fence effect.
  }
  settingsObservation(raw) {
    const v = settings(raw, this.scope.routerSource);
    need(
      v.providerRuntime.runtimeInstanceId === this.scope.runtimeInstanceId &&
        Math.abs(this.now() - v.providerRuntime.observedAt) <= 5000,
      "Provider runtime changed/stale",
    );
    return v;
  }
  async switchEpoch(segment) {
    need(
      this.sample &&
        this.sample.openRequests === 0 &&
        this.sample.processes.length === 0 &&
        !this.pending,
      "Observer epoch not drained",
    );
    const oldScope = this.scope;
    await this.stopMonitor();
    need(!this.monitorFault, "Old observer failed before epoch transition");
    this.state.processEpochs.push({
      scope: clone(oldScope),
      process: clone(this.state.process),
      settledRequestIds: clone(this.sample.settledRequestIds),
      transition: "drained-and-joined",
    });
    this.scope = Object.freeze({
      ...oldScope,
      phaseId: segment.phaseId,
      providerGeneration: this.lastSettings.providerRuntime.providerGeneration,
    });
    this.state.scope = this.scope;
    this.events = new Map();
    this.seenAlive = new Set();
    this.sample = null;
    this.save();
    await this.startMonitor();
    need(
      this.events.size === 0 &&
        this.sample.openRequests === 0 &&
        this.sample.processes.length === 0,
      "New observer epoch not empty",
    );
  }
  async armSegment(segment, predecessor) {
    need(
      !this.pending &&
        this.sample.openRequests === 0 &&
        this.scope.phaseId === segment.phaseId &&
        this.scope.providerGeneration ===
          this.lastSettings.providerRuntime.providerGeneration,
      "Subplan generation/drain not observed",
    );
    if (segment.kind === "cold") {
      const warm = this.state.fences.find(
        (f) => f.fenceId === this.segments[0].fenceId,
      );
      need(
        warm?.state === "closed" &&
          predecessor.predecessorFenceId === warm.fenceId &&
          predecessor.expectedFenceRevision === warm.revision &&
          predecessor.expectedAttempts === warm.attempts &&
          predecessor.expectedUnknownUsage === warm.tokenUsageUnknown,
        "Cold predecessor continuity changed",
      );
    }
    this.fenceContext = {
      scope: this.scope,
      segment,
      predecessorFenceId: predecessor.predecessorFenceId,
    };
    this.fenceId = segment.fenceId;
    this.fence = null;
    this.row = null;
    this.state.fenceContext = clone(this.fenceContext);
    this.save();
    return this.fenceMutation(
      "arm",
      {
        keyId: this.scope.keyId,
        keyCreatedAt: this.scope.keyCreatedAt,
        ...predecessor,
        fenceId: segment.fenceId,
        runId: this.inputs.runId,
        phaseId: segment.phaseId,
        scopeDigest: digest(this.scope),
        planDigest: segment.planDigest,
        namespace: this.scope.namespace,
        principalId: this.scope.principalId,
        deploymentId: this.scope.deploymentId,
        originalDeadline: this.effectiveExpiry,
        phaseLimit: segment.phaseLimit,
        rows: segment.rows,
        selection: {
          route: "canary-pi-claude",
          accountId: this.scope.account,
          deploymentId: this.scope.targetDeploymentId,
          model: this.scope.model,
          provider: "claude",
          providerGeneration: this.scope.providerGeneration,
        },
      },
      segment.rows[0].id,
    );
  }
  async beginWarm(predecessor) {
    need(
      this.segments && !this.state.cBaseline && this.events.size === 0,
      "C baseline already used/nonempty",
    );
    this.state.cBaseline = {
      attempts: predecessor.expectedAttempts,
      tokenUsageUnknown: predecessor.expectedUnknownUsage,
    };
    this.save();
    await this.changeSettings("enable", true, 10000);
    await this.switchEpoch(this.segments[0]);
    await this.armSegment(this.segments[0], predecessor);
  }
  async closeCurrentFence(record) {
    need(
      !this.pending &&
        this.sample.openRequests === 0 &&
        record.state === "passed",
      "Native/client drain missing before fence close",
    );
    const f = await this.inspectFence(record.rowId);
    need(
      record.fenceId === this.fenceId &&
        record.scopeDigest === digest(this.fenceContext.scope) &&
        f.attempts === record.admittedBefore + record.generations &&
        f.tokenUsageUnknown === record.settledTokenUsageUnknown,
      "Terminal evidence no longer matches fence counters",
    );
    const closed = await this.fenceMutation(
      "close",
      {
        keyId: this.fenceContext.scope.keyId,
        fenceId: this.fenceId,
        expectedFenceRevision: f.revision,
        expectedAttempts: f.attempts,
        expectedUnknownUsage: f.tokenUsageUnknown,
        terminalEvidenceSha256: record.terminalEvidenceSha256,
      },
      record.rowId,
    );
    need(
      closed.openRequests === 0 &&
        closed.staleOpenRequests === 0 &&
        closed.unattributed === 0,
      "Closed fence still unsettled",
    );
    return closed;
  }
  async beginCold(previous) {
    need(
      previous.rowId === "P3" && this.fenceContext.segment.kind === "warm",
      "Cold transition out of order",
    );
    const warm = await this.closeCurrentFence(previous);
    // Restore BOTH values now, before the cold scope; no third provider refresh.
    await this.changeSettings(
      "disable",
      false,
      this.originalSettings.values.warmIdleMs,
    );
    await this.waitFor(
      () =>
        this.sample.processes.length === 0 &&
        [...this.events.values()]
          .filter((e) => e.phase === "dispatch")
          .every((e) => this.absent(e.identity)),
      5000,
      "Old children remain after disable",
    );
    await this.switchEpoch(this.segments[1]);
    await this.armSegment(this.segments[1], {
      predecessorFenceId: warm.fenceId,
      expectedFenceRevision: warm.revision,
      expectedAttempts: warm.attempts,
      expectedUnknownUsage: warm.tokenUsageUnknown,
    });
  }
  async finishRows(previous) {
    need(
      previous.rowId === "P4" && this.fenceContext.segment.kind === "cold",
      "C completion out of order",
    );
    await this.closeCurrentFence(previous);
    await this.close();
    const final = this.settingsObservation(await this.call("settings", "read"));
    need(
      eq(final.values, this.originalSettings.values) &&
        final.revision === this.lastSettings.revision &&
        final.providerRuntime.providerGeneration ===
          this.scope.providerGeneration,
      "Final cold settings/generation changed",
    );
    await this.stopMonitor();
    need(!this.monitorFault, "Observer failed before completion");
    this.state.processEpochs.push({
      scope: clone(this.scope),
      process: clone(this.state.process),
      settledRequestIds: clone(this.sample.settledRequestIds),
      transition: "final-drained-and-joined",
    });
    this.state.cUsage = {
      attempts: this.fence.attempts - this.state.cBaseline.attempts,
      tokenUsageUnknown:
        this.fence.tokenUsageUnknown - this.state.cBaseline.tokenUsageUnknown,
    };
    this.save();
  }
  fenceProjection(
    raw,
    rowId,
    {
      closed = false,
      zeroOpen = true,
      mutation = false,
      reconciliation = false,
    } = {},
  ) {
    const context = this.fenceContext;
    need(context && this.state.cBaseline, "No persisted C fence context");
    const { scope, segment } = context;
    const f = raw?.fence;
    privateKeys(raw, [
      "version",
      "source",
      "scopeDigest",
      "projectionSha256",
      "operationId",
      "fence",
    ]);
    privateKeys(f, [
      "fenceId",
      "keyId",
      "keyCreatedAt",
      "coveredFrom",
      "namespace",
      "principalId",
      "deploymentId",
      "selection",
      "runId",
      "phaseId",
      "predecessorFenceId",
      "planDigest",
      "originalDeadline",
      "phaseLimit",
      "rows",
      "activeRowId",
      "state",
      "revision",
      "attempts",
      "baselineAttempts",
      "rowStartAttempts",
      "openRequests",
      "staleOpenRequests",
      "unattributed",
      "tokenUsageUnknown",
      "baselineTokenUsageUnknown",
      "rowStartTokenUsageUnknown",
    ]);
    need(
      raw?.version === 1 &&
        raw.source === scope.routerSource &&
        raw.scopeDigest === digest(scope) &&
        hash(raw.projectionSha256) &&
        f,
      "Fence projection unavailable",
    );
    need(
      f.fenceId === segment.fenceId &&
        f.keyId === scope.keyId &&
        f.keyCreatedAt === scope.keyCreatedAt &&
        f.coveredFrom === scope.keyCreatedAt &&
        f.namespace === scope.namespace &&
        f.principalId === scope.principalId &&
        f.deploymentId === scope.deploymentId &&
        eq(f.selection, {
          route: "canary-pi-claude",
          accountId: scope.account,
          deploymentId: scope.targetDeploymentId,
          model: scope.model,
          provider: "claude",
          providerGeneration: scope.providerGeneration,
        }) &&
        f.runId === this.inputs.runId &&
        f.phaseId === segment.phaseId &&
        f.predecessorFenceId === context.predecessorFenceId &&
        f.planDigest === segment.planDigest &&
        f.originalDeadline === this.effectiveExpiry &&
        f.phaseLimit === segment.phaseLimit &&
        eq(f.rows, segment.rows) &&
        f.activeRowId === rowId &&
        f.state === (closed ? "closed" : "active") &&
        integer(f.revision) &&
        f.revision > 0 &&
        integer(f.attempts) &&
        integer(f.baselineAttempts) &&
        integer(f.rowStartAttempts) &&
        f.baselineAttempts <= f.rowStartAttempts &&
        f.rowStartAttempts <= f.attempts &&
        f.attempts - f.baselineAttempts <= segment.phaseLimit &&
        f.attempts >= this.state.cBaseline.attempts &&
        f.attempts - this.state.cBaseline.attempts <= PLAN.phaseLimit &&
        f.tokenUsageUnknown >= this.state.cBaseline.tokenUsageUnknown &&
        integer(f.openRequests) &&
        integer(f.staleOpenRequests) &&
        integer(f.unattributed) &&
        integer(f.tokenUsageUnknown) &&
        integer(f.baselineTokenUsageUnknown) &&
        integer(f.rowStartTokenUsageUnknown) &&
        f.baselineTokenUsageUnknown <= f.rowStartTokenUsageUnknown &&
        f.rowStartTokenUsageUnknown <= f.tokenUsageUnknown &&
        f.tokenUsageUnknown <= f.attempts &&
        f.rowStartTokenUsageUnknown <= f.rowStartAttempts &&
        f.baselineTokenUsageUnknown <= f.baselineAttempts,
      "Fence identity/state/counters changed",
    );
    if (zeroOpen)
      need(
        f.openRequests === 0 &&
          f.staleOpenRequests === 0 &&
          f.unattributed === 0,
        "Fence has unsettled/unattributed attempts",
      );
    const row = segment.rows.find((r) => r.id === rowId);
    need(
      row && f.attempts - f.rowStartAttempts <= row.limit,
      "Row admission cap exceeded",
    );
    if (this.fence)
      need(
        f.revision >= this.fence.revision &&
          f.attempts >= this.fence.attempts &&
          f.baselineAttempts === this.fence.baselineAttempts &&
          f.tokenUsageUnknown >= this.fence.tokenUsageUnknown &&
          f.baselineTokenUsageUnknown === this.fence.baselineTokenUsageUnknown,
        "Fence counters regressed",
      );
    if (this.fence && !mutation && !reconciliation)
      need(
        f.revision === this.fence.revision &&
          f.activeRowId === this.fence.activeRowId &&
          f.rowStartAttempts === this.fence.rowStartAttempts &&
          f.rowStartTokenUsageUnknown ===
            this.fence.rowStartTokenUsageUnknown &&
          f.state === this.fence.state,
        "Fence control changed outside this runner",
      );
    this.fence = clone(f);
    this.state.fence = clone(f);
    const at = this.state.fences.findIndex((old) => old.fenceId === f.fenceId);
    if (at < 0) this.state.fences.push(clone(f));
    else this.state.fences[at] = clone(f);
    this.save();
    return f;
  }
  async fenceMutation(method, body, row, cleaning = false) {
    const operationId = this.crypto.randomUUID();
    const item = { operationId, method, body: clone(body), state: "intent" };
    this.state.fenceOperations.push(item);
    this.save();
    try {
      const response = await this.call(
        "fence",
        method,
        { ...body, operationId, scope: this.fenceContext.scope },
        cleaning,
      );
      need(
        response.operationId === operationId,
        "Fence operation receipt mismatch",
      );
      item.state = "received";
      item.receiptSha256 = digest(response);
      this.save();
      const f = this.fenceProjection(response, row, {
        closed: method === "close",
        zeroOpen: method !== "close",
        mutation: true,
      });
      need(
        f.attempts === body.expectedAttempts &&
          f.tokenUsageUnknown === body.expectedUnknownUsage &&
          f.revision === body.expectedFenceRevision + 1,
        "Fence receipt does not match mutation compare",
      );
      if (method === "arm")
        need(
          f.baselineAttempts === body.expectedAttempts &&
            f.rowStartAttempts === body.expectedAttempts &&
            f.baselineTokenUsageUnknown === body.expectedUnknownUsage &&
            f.rowStartTokenUsageUnknown === body.expectedUnknownUsage,
          "Arm reset existing accounting",
        );
      if (method === "advance")
        need(
          f.rowStartAttempts === body.expectedAttempts &&
            f.rowStartTokenUsageUnknown === body.expectedUnknownUsage,
          "Advance borrowed allowance",
        );
      return f;
    } catch (error) {
      item.state = "uncertain";
      this.save();
      throw error;
    }
  }
  async inspectFence(
    row = this.row?.id || this.fenceContext?.segment.rows[0].id,
    cleaning = false,
  ) {
    return this.fenceProjection(
      await this.call(
        "fence",
        "inspect",
        {
          keyId: this.fenceContext.scope.keyId,
          scope: this.fenceContext.scope,
        },
        cleaning,
      ),
      row,
    );
  }
  async changeSettings(kind, warm, idle, cleaning = false) {
    const before = this.settingsObservation(
      await this.call("settings", "read", {}, cleaning),
    );
    need(
      eq(
        protectedSettings(before.values),
        protectedSettings(this.originalSettings.values),
      ),
      "Foreign settings change; no overwrite",
    );
    if (this.lastSettings)
      need(
        eq(before.values, this.lastSettings.values) &&
          before.revision === this.lastSettings.revision &&
          before.providerRuntime.providerGeneration ===
            this.lastSettings.providerRuntime.providerGeneration,
        "Settings ownership lost",
      );
    const values = {
      ...clone(before.values),
      warmSessions: warm,
      warmIdleMs: idle,
    };
    if (eq(values, before.values)) {
      this.lastSettings = before;
      return;
    }
    const intentId = this.crypto.randomUUID();
    const operation = {
      intentId,
      kind,
      expectedRevision: before.revision,
      before: before.values,
      values,
      state: "intent",
    };
    this.state.settingsOperations.push(operation);
    this.save();
    try {
      const receipt = await this.call(
        "settings",
        "replace",
        { intentId, expectedRevision: before.revision, values },
        cleaning,
      );
      need(
        receipt?.intentId === intentId && hash(receipt.projectionSha256),
        "Settings mutation receipt missing",
      );
      const after = this.settingsObservation(
        await this.call("settings", "read", {}, cleaning),
      );
      need(
        eq(after.values, values) &&
          after.revision !== before.revision &&
          after.providerRuntime.providerGeneration !==
            before.providerRuntime.providerGeneration,
        "Settings mutation not confirmed",
      );
      operation.state = "confirmed";
      operation.afterRevision = after.revision;
      operation.readbackSha256 = after.projectionSha256;
      operation.providerRuntime = clone(after.providerRuntime);
      this.lastSettings = after;
      this.save();
    } catch (error) {
      operation.state = "uncertain";
      this.settingsUncertain = true;
      this.save();
      // No replay or CAS loop. Cleanup may only inspect this exact last intent.
      throw error;
    }
  }
  async ask(title, instructions, validate, timeout, context) {
    if (context?.kind === "permission") {
      need(this.row && this.pending, "Permission outside an active C row");
      const f = await this.inspectFence();
      need(
        f.attempts - f.rowStartAttempts < this.row.limit &&
          f.attempts - f.baselineAttempts < 9,
        "No allowance for permission continuation",
      );
      if (context.purpose !== "relay-message-read") {
        need(
          this.row.id === "P3" &&
            context.expectedCommand === "pwd" &&
            context.decisionKind === "reject_once",
          "Only exact pwd denial is allowed",
        );
        const end = Math.min(
          Date.parse(context.permission.expiresAt),
          this.now() + 30000,
        );
        need(
          end - this.now() > 15000,
          "Insufficient permission lifetime for pwd hold",
        );
        await this.waitFor(
          () => {
            try {
              const rows = attempts([...this.events.values()], this.rowBefore);
              return (
                rows.length > 0 &&
                rows.every(
                  (r) =>
                    r.terminal.outcome === "reaped" &&
                    this.absent(r.terminal.identity),
                )
              );
            } catch {
              return false;
            }
          },
          Math.min(5000, end - this.now() - 15000),
          "Permission-producing child disappearance unobserved",
        );
        const start = this.mono();
        await this.waitFor(
          () => this.mono() - start >= 15000000000n,
          15000,
          "Pwd hold deadline",
        );
        need(
          this.now() < end &&
            this.now() < Date.parse(context.permission.expiresAt),
          "Permission expired during hold",
        );
        this.record("pwd-denial-hold", {
          durationNs: (this.mono() - start).toString(),
          requestId: context.permission.id,
        });
        context = { ...context, purpose: "warm-pwd-denial" };
        timeout = Math.min(timeout ?? 30000, end - this.now());
      }
    }
    return super.ask(title, instructions, validate, timeout, context);
  }
  async prompt(text, options = {}) {
    need(
      this.row && text === this.row.text && this.run.prompts < 4,
      "Only the next fixed C prompt is allowed",
    );
    return super.prompt(text, options);
  }
  async mutate(path, body) {
    if (body?.kind === "prompt") {
      need(
        this.row && body.text === this.row.text,
        "C prompt dispatch differs",
      );
      if (this.dispatchBound)
        this.conservativeWindow(
          this.dispatchBound.previous,
          this.dispatchBound.seconds,
        );
    }
    // No await separates the final bound from A's durable-intent/API dispatch.
    return super.mutate(path, body);
  }
  async runRow(row, previous) {
    this.row = row;
    this.dispatchBound =
      previous && ["P2", "P3"].includes(row.id)
        ? { previous, seconds: row.id === "P2" ? 10 : 20 }
        : null;
    if (previous) {
      if (row.id === "P3") {
        await this.waitFor(
          () =>
            this.absent(previous.final.terminal.identity) &&
            this.mono() - ticks(previous.settledLocalNs) >= 10000000000n,
          20000,
          "Idle disappearance unobserved",
        );
        this.conservativeWindow(previous, 20);
      } else if (row.id === "P2") this.conservativeWindow(previous, 10);
      need(
        !this.pending && this.sample.openRequests === 0,
        "Native/client drain missing before row advance",
      );
      const f = await this.inspectFence(previous.rowId);
      await this.fenceMutation(
        "advance",
        {
          keyId: this.scope.keyId,
          fenceId: this.fenceId,
          expectedFenceRevision: f.revision,
          expectedAttempts: f.attempts,
          expectedUnknownUsage: f.tokenUsageUnknown,
          completedRowId: previous.rowId,
          nextRowId: row.id,
          terminalEvidenceSha256: previous.terminalEvidenceSha256,
        },
        row.id,
      );
    }
    await this.inspectFence(row.id);
    this.rowBefore = new Set(
      [...this.events.values()].map((e) => e.requestId + ":" + e.attemptId),
    );
    const record = {
      rowId: row.id,
      fenceId: this.fenceId,
      phaseId: this.fenceContext.segment.phaseId,
      scopeDigest: digest(this.scope),
      cap: row.limit,
      state: "intent",
      startedLocalNs: this.mono().toString(),
      admittedBefore: this.fence.attempts,
    };
    this.state.rows.push(record);
    this.save();
    // Recheck the conservative bound AFTER awaited fence recording, immediately
    // before entering the guarded prompt path; later awaits remain charged.
    if (previous && ["P2", "P3"].includes(row.id))
      this.conservativeWindow(previous, row.id === "P2" ? 10 : 20);
    await this.prompt(row.text, row);
    need(!this.pending, "Native command not settled");
    await this.waitFor(
      () => {
        try {
          const rows = attempts([...this.events.values()], this.rowBefore);
          return (
            rows.length > 0 &&
            rows.every((r) =>
              this.sample.settledRequestIds.includes(r.dispatch.requestId),
            ) &&
            this.sample.openRequests === 0
          );
        } catch {
          return false;
        }
      },
      5000,
      "Scoped process/trajectory settlement missing",
    );
    const rows = attempts([...this.events.values()], this.rowBefore);
    const f = await this.inspectFence(row.id);
    need(
      rows.length >= 1 &&
        rows.length <= row.limit &&
        f.attempts - record.admittedBefore === rows.length,
      "Admitted and observed generations differ",
    );
    const first = rows[0],
      final = rows.at(-1);
    need(
      rows.every(
        (r) => r.dispatch.mode === (row.id === "P4" ? "cold" : "warm"),
      ),
      "Unexpected transport mode",
    );
    if (["P1", "P2"].includes(row.id)) {
      need(
        final.terminal.outcome === "retained" &&
          this.sample.processes.some(
            (p) =>
              p.pid === final.terminal.identity.pid &&
              p.startTicks === final.terminal.identity.startTicks,
          ),
        "Final warm worker retention unobserved",
      );
    }
    if (row.id === "P2") {
      need(
        sameChild(previous.final.terminal, first.dispatch) &&
          first.dispatch.reused === true &&
          first.dispatch.turn === previous.final.terminal.turn + 1,
        "P2 first generation did not reuse P1 final worker",
      );
      record.nativeGapNs = exactGap(previous.final, first, 10);
    }
    if (row.id === "P3")
      record.nativeGapNs = exactGap(previous.final, first, 20);
    if (row.id === "P4")
      need(
        rows.every(
          (r) =>
            r.terminal.outcome === "reaped" && this.absent(r.terminal.identity),
        ),
        "Cold child disappearance unobserved",
      );
    Object.assign(record, {
      state: "passed",
      generations: rows.length,
      settledTokenUsageUnknown: f.tokenUsageUnknown,
      final,
      settledLocalNs: this.mono().toString(),
      terminalEvidenceSha256: digest({
        prompt: this.state.evidence.filter((e) => e.kind === "prompt").at(-1),
        processProjection: this.sample.projectionSha256,
        fence: f,
      }),
    });
    this.save();
    return record;
  }
  async prepare() {
    const published = release();
    const p = await this.currentCredential();
    const ready = await this.call("readiness", "read", {
      inputs: this.inputs,
      credential: p,
      release: published,
    });
    need(
      ready?.version === 1 &&
        ready.hubSource === b.PINS.hub &&
        ready.routerSource === published.source &&
        ready.holderSource === b.PINS.holder &&
        ready.holderBinarySha256 === b.PINS.binary &&
        ready.piManifestSha256 === b.PINS.piManifest &&
        ready.profilesSha256 === b.PINS.profiles &&
        hash(ready.projectionSha256),
      "C source qualification/readiness differs",
    );
    need(
      uuid(ready.holderId) &&
        uuid(ready.journalId) &&
        ready.nativeRecovery === 2 &&
        ready.nativeStateReady === true &&
        ready.activeSessions === 0 &&
        ready.pendingCommands === 0 &&
        ready.admissionPaused === false &&
        Math.abs(this.now() - Date.parse(ready.observedUtc)) <= 5000,
      "Living holder is not ready/idle",
    );
    need(
      ready.account === this.inputs.account &&
        ready.model === this.inputs.model &&
        ready.route === "canary-pi-claude" &&
        ready.maxAttempts === 1 &&
        eq(ready.fallbacks, []) &&
        ready.warmSessions === false &&
        ready.transport === "sdk-resume" &&
        ready.trustVerified === true &&
        ready.cliVersion === "2.1.283" &&
        ready.exclusiveWindowSha256 === this.inputs.exclusiveWindowSha256 &&
        ready.phaseBReportSha256 === this.inputs.phaseBReportSha256 &&
        ready.coldReportSha256 === this.inputs.coldReportSha256 &&
        ready.pairedReportSha256 === this.inputs.pairedReportSha256,
      "Selected C prerequisite evidence missing",
    );
    need(
      label(ready.runtimeInstanceId) &&
        label(ready.deploymentId) &&
        label(ready.targetDeploymentId) &&
        integer(ready.providerGeneration) &&
        integer(ready.keyCreatedAt) &&
        ready.keyCreatedAt > 0 &&
        integer(ready.attempts) &&
        integer(ready.tokenUsageUnknown) &&
        integer(ready.expectedFenceRevision) &&
        integer(ready.root?.pid) &&
        ready.root.pid > 0 &&
        /^[1-9][0-9]{0,19}$/.test(ready.root.startTicks),
      "Router process/meter identity missing",
    );
    privateKeys(ready.root, ["pid", "startTicks"]);
    need(
      ready.expectedFenceRevision === 0
        ? ready.predecessorFenceId === undefined
        : uuid(ready.predecessorFenceId),
      "Prior fence identity missing/ambiguous",
    );
    need(
      ready.expectedFenceRevision !== 0 || ready.tokenUsageUnknown === 0,
      "Initial arm cannot inherit unknown token usage",
    );
    this.effectiveExpiry = Math.min(this.inputs.window.expiresAt, p.expiresAt);
    this.scope = Object.freeze({
      version: 1,
      runId: this.inputs.runId,
      phaseId: this.inputs.phaseId,
      hubSource: b.PINS.hub,
      routerSource: published.source,
      runtimeInstanceId: ready.runtimeInstanceId,
      deploymentId: ready.deploymentId,
      targetDeploymentId: ready.targetDeploymentId,
      account: ready.account,
      model: ready.model,
      providerGeneration: ready.providerGeneration,
      namespace: "default",
      principalId: "key:" + p.key,
      keyId: p.key,
      keyCreatedAt: ready.keyCreatedAt,
      bindingId: p.binding,
      credentialId: p.credential,
      credentialGeneration: p.generation,
      profileId: p.id,
      profileRevision: 1,
      root: Object.freeze(clone(ready.root)),
      deadline: this.effectiveExpiry,
    });
    this.state.scope = this.scope;
    this.state.router = published.source;
    this.state.claudeCredential = p;
    this.profiles = Object.freeze({
      claude: Object.freeze({ ...p, run: this.inputs.runId }),
    });
    this.holderId = ready.holderId;
    this.journalId = ready.journalId;
    this.run = {
      provider: "claude",
      runId: this.inputs.runId,
      effort: false,
      prompts: 0,
      deadline: this.effectiveExpiry,
    };
    this.state.runs.push(this.run);
    this.save();
    const profiles = await this.api("/api/agent-profiles");
    const match = profiles.filter(
      (v) => v.id === p.id && v.revision === 1 && v.enabled === true,
    );
    need(
      match.length === 1 &&
        (await a.digest(this.crypto, match[0].definition)) === p.definitionHash,
      "Approved profile drifted",
    );
    await this.startMonitor();
    need(
      this.events.size === 0 && this.sample.openRequests === 0,
      "C observation window not empty",
    );
    const pre = await this.ask(
      "C prelaunch marker baseline",
      "Read exact fixed-run markers without writes.",
      (v) =>
        a.marker(
          v,
          "claude",
          this.inputs.runId,
          "baseline",
          this.now(),
          this.profiles,
        ),
      undefined,
      {
        kind: "warm-marker-baseline",
        runId: this.inputs.runId,
        stage: "before-launch",
      },
    );
    await this.launch();
    const boundary = this.collectionBoundary("C post-Open baseline");
    const post = await this.ask(
      "C delivery/Open marker baseline",
      "Read exact same run markers after actual delivery/Open.",
      (v) => {
        const result = a.marker(
          v,
          "claude",
          this.inputs.runId,
          "baseline",
          this.now(),
          this.profiles,
        );
        this.collectedAfter(boundary, null, result);
        return result;
      },
      undefined,
      {
        kind: "warm-marker-baseline",
        runId: this.inputs.runId,
        stage: "after-open",
        sessionId: this.session.id,
        notBefore: boundary.notBefore,
      },
    );
    a.unchangedBaseline(pre, post);
    this.record("warm-launch-baselines", { pre, post });
    this.originalSettings = this.settingsObservation(
      await this.call("settings", "read"),
    );
    need(
      this.originalSettings.values.warmSessions === false &&
        this.originalSettings.providerRuntime.providerGeneration ===
          this.scope.providerGeneration,
      "C must begin at observed cold generation",
    );
    this.state.originalSettings = this.originalSettings;
    this.lastSettings = this.originalSettings;
    await this.beginWarm({
      expectedAttempts: ready.attempts,
      expectedUnknownUsage: ready.tokenUsageUnknown,
      expectedFenceRevision: ready.expectedFenceRevision,
      ...(ready.predecessorFenceId
        ? { predecessorFenceId: ready.predecessorFenceId }
        : {}),
    });
  }
  async startPhaseC() {
    need(
      this.env.origin === a.ORIGIN &&
        this.env.load() === null &&
        !this.started &&
        !this.stopped,
      "Fresh exclusive C report required",
    );
    release(); // BEFORE any API/port use. No caller-selected pin or bypass.
    this.started = true;
    this.state.status = "running";
    this.planSegments();
    this.save();
    try {
      await this.prepare();
      let previous;
      for (const row of ROWS) {
        if (row.id === "P4") {
          await this.beginCold(previous);
          previous = undefined; // First row of its own fixed cold plan.
        }
        previous = await this.runRow(row, previous);
      }
      await this.finishRows(previous);
      this.run.status = "passed-local-process-observations";
      this.state.status = "phase-c-passed";
      this.state.phaseCCompleted = true;
      this.state.completed = false;
      this.save();
    } catch (error) {
      this.stopped = true;
      this.state.status = "stopped";
      this.state.reason =
        "C stopped; preserve scoped observations and unresolved intents";
      this.save();
      await this.cleanup();
      throw error;
    }
  }
  async stopMonitor() {
    if (!this.monitorStarted || this.monitorStopped) return;
    this.monitorAbort.abort();
    await this.call("process", "stop", {}, true);
    await this.monitorTask;
    this.monitorStopped = true;
  }
  async cleanup() {
    if (this.cCleanup) return this.cCleanup;
    this.cCleanup = (async () => {
      const outcomes = [];
      if (this.fenceContext) {
        try {
          const r = await this.call(
            "fence",
            "inspect",
            {
              keyId: this.fenceContext.scope.keyId,
              scope: this.fenceContext.scope,
            },
            true,
          );
          const f = this.fenceProjection(r, r?.fence?.activeRowId, {
            closed: r?.fence?.state === "closed",
            zeroOpen: false,
            reconciliation: true,
          });
          if (f.state !== "closed")
            await this.fenceMutation(
              "close",
              {
                keyId: this.fenceContext.scope.keyId,
                fenceId: this.fenceId,
                expectedFenceRevision: f.revision,
                expectedAttempts: f.attempts,
                expectedUnknownUsage: f.tokenUsageUnknown,
                terminalEvidenceSha256: digest({
                  cleanup: true,
                  operations: this.state.operations,
                }),
              },
              f.activeRowId,
              true,
            );
          outcomes.push({
            resource: "fence",
            state: "blocked",
            openRequests: this.fence.openRequests,
            staleOpenRequests: this.fence.staleOpenRequests,
            unattributed: this.fence.unattributed,
            tokenUsageUnknown: this.fence.tokenUsageUnknown,
          });
        } catch {
          outcomes.push({ resource: "fence", state: "unresolved" });
        }
      }
      // Block further admission first. A cleanup retains original operation IDs
      // and does not grant new authority; its failure cannot skip cold rollback.
      try {
        await super.cleanup();
      } catch {
        outcomes.push({ resource: "session", state: "unresolved" });
      }
      if (this.originalSettings) {
        try {
          if (this.settingsUncertain) {
            const actual = this.settingsObservation(
              await this.call("settings", "read", {}, true),
            );
            const last = this.state.settingsOperations.at(-1);
            need(
              eq(actual.values, last.values) ||
                (eq(actual.values, last.before) &&
                  actual.revision === last.expectedRevision),
              "Uncertain/foreign settings outcome",
            );
            this.lastSettings = actual;
            this.settingsUncertain = false;
            this.record("settings-reconciliation", {
              intentId: last.intentId,
              revision: actual.revision,
              projectionSha256: actual.projectionSha256,
            });
          }
          await this.changeSettings(
            "cleanup-cold",
            false,
            this.originalSettings.values.warmIdleMs,
            true,
          );
          outcomes.push({ resource: "settings", state: "cold-original-idle" });
        } catch {
          outcomes.push({ resource: "settings", state: "unresolved" });
        }
      }
      try {
        await this.stopMonitor();
        outcomes.push({ resource: "observer", state: "stopped" });
      } catch {
        outcomes.push({ resource: "observer", state: "unresolved" });
      }
      this.record("warm-cleanup", { outcomes });
    })();
    return this.cCleanup;
  }
}

function createClaudePhaseCRunner(env, value, ports) {
  return new WarmRunner(env, value, ports);
}
module.exports = {
  createClaudePhaseCRunner,
  ROWS,
  PLAN,
  planDigest,
  inputs,
  observation,
  attempts,
  exactGap,
  settings,
};
