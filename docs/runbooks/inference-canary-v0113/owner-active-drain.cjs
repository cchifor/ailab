"use strict";

// Fixed active-generation drain. Selected C and its source gate are unchanged.
const a = require("./owner-phase-a.js");
const b = require("./owner-phase-b.js");
const c = require("./owner-phase-c.cjs");
const { createHash } = require("node:crypto");
const QUALIFIED_ROUTER_RELEASE = null;
const ROWS = Object.freeze([
  Object.freeze({
    id: "D1",
    limit: 2,
    text: "Without using tools or modifying files, count upward from 1, one integer per line, until 500.",
  }),
  Object.freeze({
    id: "D2",
    limit: 2,
    text: "Reply exactly COLD_ROLLBACK_OK. Do not use tools or modify files.",
    expectedText: "COLD_ROLLBACK_OK",
  }),
]);
const PLAN = Object.freeze({
  phaseLimit: 4,
  promptLimit: 2,
  rows: Object.freeze(
    ROWS.map(({ id, limit }) => Object.freeze({ id, limit })),
  ),
});
const SUBPLANS = Object.freeze(
  ROWS.map((r, i) =>
    Object.freeze({
      kind: i ? "cold" : "warm",
      phaseLimit: 2,
      rows: Object.freeze([PLAN.rows[i]]),
    }),
  ),
);
const SHA = /^[a-f0-9]{64}$/,
  UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/,
  LABEL = /^[A-Za-z0-9._:-]{1,200}$/,
  NS = /^(?:0|[1-9][0-9]{0,24})$/;
const hash = (v) => typeof v === "string" && SHA.test(v);
const uuid = (v) => typeof v === "string" && UUID.test(v);
const label = (v) => typeof v === "string" && LABEL.test(v);
const integer = (v) => Number.isSafeInteger(v) && v >= 0;
const clone = (v) => JSON.parse(JSON.stringify(v));
const digest = (v) => createHash("sha256").update(a.canonical(v)).digest("hex");
const rawDigest = (v) => createHash("sha256").update(v).digest("hex");
const eq = (x, y) => a.canonical(x) === a.canonical(y);
const planDigest = digest({ plan: PLAN, prompts: ROWS.map((r) => r.text) });
const observation = c.observation,
  attempts = c.attempts;
const eventKey = (e) => e.requestId + ":" + e.attemptId + ":" + e.phase;
function need(ok, reason) {
  if (!ok) throw Error(reason);
}
function ticks(v) {
  need(
    typeof v === "string" && NS.test(v),
    "Same-runtime monotonic witness missing",
  );
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
function protectedSettings(v) {
  const { warmSessions, warmIdleMs, ...rest } = v;
  return rest;
}
function release(input) {
  const p = QUALIFIED_ROUTER_RELEASE;
  need(
    p &&
      /^[a-f0-9]{40}$/.test(p.source) &&
      hash(p.qualificationSha256) &&
      /^[a-f0-9]{40}$/.test(p.prerequisiteCSource) &&
      hash(p.prerequisiteCQualificationSha256) &&
      p.timing === true &&
      p.attemptFence === true &&
      p.monotonicWitness === true,
    "Final active-drain Router release is not qualified",
  );
  need(
    input.phaseCRouterSource === p.prerequisiteCSource,
    "Fixed historical C source differs",
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
    "phaseBReportSha256",
    "phaseCReportSha256",
    "phaseCReportRaw",
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
    "Fixed active-drain inputs invalid",
  );
  privateKeys(value.window, ["startsAt", "expiresAt"]);
  need(
    integer(value.window.startsAt) &&
      integer(value.window.expiresAt) &&
      value.window.startsAt <= now &&
      value.window.expiresAt > now,
    "Actual unexpired active-drain window required",
  );
  for (const k of [
    "phaseAReportSha256",
    "phaseBReportSha256",
    "phaseCReportSha256",
    "coldReportSha256",
    "pairedReportSha256",
    "exclusiveWindowSha256",
  ])
    need(hash(value[k]), "Independent prerequisite hash missing");
  need(
    typeof value.phaseCReportRaw === "string" &&
      Buffer.byteLength(value.phaseCReportRaw) <= 1048576 &&
      rawDigest(value.phaseCReportRaw) === value.phaseCReportSha256,
    "Exact independently pinned C report required",
  );
  const p = JSON.parse(value.phaseCReportRaw);
  need(
    p.version === 1 &&
      p.kind === "claude-phase-c" &&
      p.status === "phase-c-passed" &&
      p.phaseCCompleted === true &&
      p.relay === b.PINS.hub &&
      /^[a-f0-9]{40}$/.test(p.router),
    "Completed C prerequisite missing",
  );
  for (const k of [
    "account",
    "model",
    "phaseAReportSha256",
    "phaseBReportSha256",
    "coldReportSha256",
    "pairedReportSha256",
  ])
    need(p.inputs?.[k] === value[k], "C prerequisite context/hash differs");
  need(
    uuid(p.inputs.runId) &&
      uuid(p.inputs.phaseId) &&
      p.inputs.runId !== p.inputs.phaseId &&
      p.inputs.runId !== value.runId &&
      p.inputs.phaseId !== value.phaseId &&
      p.rows?.length === 4 &&
      p.rows.every((r, i) => r.rowId === "P" + (i + 1) && r.state === "passed"),
    "C rows incomplete or replayed",
  );
  need(
    p.scope?.routerSource === p.router &&
      p.scope.runId === p.inputs.runId &&
      uuid(p.scope.phaseId) &&
      p.scope.phaseId !== p.inputs.phaseId &&
      p.scope.hubSource === b.PINS.hub &&
      p.scope.account === value.account &&
      p.scope.model === value.model &&
      p.scope.profileId === b.PROFILE.id &&
      p.scope.profileRevision === 1 &&
      p.scope.bindingId === b.PROFILE.binding &&
      p.claudeCredential?.definitionHash === b.PROFILE.definitionHash,
    "C source/profile context differs",
  );
  need(
    p.fences?.length === 2 &&
      p.fences[0].phaseId === p.inputs.phaseId &&
      p.fences[1].phaseId === p.scope.phaseId &&
      p.fences.every(
        (f) =>
          f.runId === p.inputs.runId &&
          f.state === "closed" &&
          f.openRequests === 0 &&
          f.staleOpenRequests === 0 &&
          f.unattributed === 0,
      ) &&
      p.process?.processes?.length === 0 &&
      p.processEpochs?.at(-1)?.transition === "final-drained-and-joined" &&
      !Object.hasOwn(p, "control"),
    "C cleanup/observer closure unresolved",
  );
  const launch = p.evidence?.filter((e) => e.kind === "launch").at(-1);
  need(
    launch &&
      launch.profileId === b.PROFILE.id &&
      uuid(launch.sessionId) &&
      uuid(launch.nativeSessionId) &&
      p.evidence.some(
        (e) => e.kind === "closed" && e.sessionId === launch.sessionId,
      ) &&
      !p.evidence.some((e) =>
        e.outcomes?.some((o) => o.state === "unresolved"),
      ),
    "C session cleanup/context missing",
  );
  const { phaseCReportRaw, ...out } = value;
  return Object.freeze({
    ...clone(out),
    window: Object.freeze(clone(value.window)),
    phaseCRouterSource: p.router,
  });
}
function settings(v, source) {
  // Validate the extended clock contract before delegating the unchanged C validator.
  privateKeys(v?.providerRuntime, [
    "runtimeInstanceId",
    "providerGeneration",
    "observedAt",
    "observedMonotonicNs",
  ]);
  const n = v.providerRuntime.observedMonotonicNs;
  ticks(n);
  const copy = clone(v);
  delete copy.providerRuntime.observedMonotonicNs;
  const checked = c.settings(copy, source);
  checked.providerRuntime.observedMonotonicNs = n;
  return checked;
}
function overlap(fetch, native, before, after, scope) {
  need(
    before.providerRuntime.runtimeInstanceId === scope.runtimeInstanceId &&
      after.providerRuntime.runtimeInstanceId === scope.runtimeInstanceId &&
      before.providerRuntime.providerGeneration === scope.providerGeneration &&
      after.providerRuntime.providerGeneration !== scope.providerGeneration,
    "Drain clock/generation domain changed",
  );
  const f = ticks(fetch.observedMonotonicNs),
    n = ticks(native.observedMonotonicNs),
    lo = ticks(before.providerRuntime.observedMonotonicNs),
    hi = ticks(after.providerRuntime.observedMonotonicNs);
  need(f <= lo && lo <= hi && hi < n, "Active native overlap unexercised");
  return {
    fetchNs: f.toString(),
    beforeNs: lo.toString(),
    afterNs: hi.toString(),
    nativeCompleteNs: n.toString(),
  };
}

class DrainRunner extends a.Runner {
  constructor(env, value, ports) {
    super(env);
    this.inputs = inputs(value, this.now());
    this.priorC = JSON.parse(value.phaseCReportRaw);
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
    need(
      integer(env.executionDeadline) &&
        env.executionDeadline > this.now() &&
        env.executionDeadline <= this.inputs.window.expiresAt,
      "Explicit bounded execution deadline required",
    );
    this.effectiveExpiry = env.executionDeadline;
    this.state.kind = "claude-active-drain";
    this.state.router = null; // Never relabel B's historical Router proof.
    this.state.inputs = this.inputs;
    this.state.plan = PLAN;
    this.state.planDigest = planDigest;
    this.state.capacity = "unexercised";
    this.state.activeDrain = "unexercised";
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
    return this.runExclusive(() => this.startActiveDrain());
  }
  replaceStopped() {
    throw Error("Drain cannot replace/reset a prior phase");
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
      // The host aborts active work after a primary failure or Stop. That also
      // cancels next(); it must not replace the saved failure with an observer fault.
      if (!this.monitorAbort.signal.aborted && !this.ports.signal.aborted) {
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
  planSegments() {
    need(!this.segments, "Drain subplans already persisted");
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
      "Drain subplan identities collide",
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
    if (this.lastRuntimeTick !== undefined)
      need(
        ticks(v.providerRuntime.observedMonotonicNs) >= this.lastRuntimeTick,
        "Router clock observation regressed",
      );
    this.lastRuntimeTick = ticks(v.providerRuntime.observedMonotonicNs);
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
      this.segments && !this.state.drainBaseline && this.events.size === 0,
      "Drain baseline already used/nonempty",
    );
    this.state.drainBaseline = {
      attempts: predecessor.expectedAttempts,
      tokenUsageUnknown: predecessor.expectedUnknownUsage,
    };
    this.save();
    await this.changeSettings(
      "enable",
      true,
      this.originalSettings.values.warmIdleMs,
    );
    await this.switchEpoch(this.segments[0]);
    await this.armSegment(this.segments[0], predecessor);
  }
  requireTerminalFence(record, f) {
    need(
      record.state === "passed" &&
        record.fenceId === this.fenceId &&
        record.phaseId === this.fenceContext.segment.phaseId &&
        record.scopeDigest === digest(this.fenceContext.scope) &&
        record.rowId === f.activeRowId &&
        f.attempts === record.admittedBefore + record.generations &&
        f.tokenUsageUnknown === record.settledTokenUsageUnknown,
      "Terminal evidence no longer matches fence counters",
    );
  }
  async closeCurrentFence(record) {
    need(
      !this.pending &&
        this.sample.openRequests === 0 &&
        record.state === "passed",
      "Native/client drain missing before fence close",
    );
    const f = await this.inspectFence(record.rowId);
    this.requireTerminalFence(record, f);
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
  async finishRows(previous) {
    need(
      previous.rowId === "D2" && this.fenceContext.segment.kind === "cold",
      "Drain completion out of order",
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
    this.state.finalSettings = final;
    await this.stopMonitor();
    need(!this.monitorFault, "Observer failed before completion");
    this.state.processEpochs.push({
      scope: clone(this.scope),
      process: clone(this.state.process),
      settledRequestIds: clone(this.sample.settledRequestIds),
      transition: "final-drained-and-joined",
    });
    this.state.drainUsage = {
      attempts: this.fence.attempts - this.state.drainBaseline.attempts,
      tokenUsageUnknown:
        this.fence.tokenUsageUnknown -
        this.state.drainBaseline.tokenUsageUnknown,
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
    need(context && this.state.drainBaseline, "No persisted C fence context");
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
        f.attempts >= this.state.drainBaseline.attempts &&
        f.attempts - this.state.drainBaseline.attempts <= PLAN.phaseLimit &&
        f.tokenUsageUnknown >= this.state.drainBaseline.tokenUsageUnknown &&
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
      return { before, after: before, operation: null };
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
      if (kind === "active-disable") {
        const selected = this.state.activeAttempt;
        const current = attempts(
          [...this.events.values()],
          this.rowBefore,
          false,
        ).find(
          (r) =>
            r.dispatch.requestId === selected?.dispatch.requestId &&
            r.dispatch.attemptId === selected?.dispatch.attemptId,
        );
        need(
          !this.promptResult &&
            this.pending &&
            !this.deciding &&
            this.sample.openRequests > 0 &&
            current?.fetch &&
            !current.native &&
            !current.terminal,
          "Selected active generation completed before settings dispatch",
        );
      }
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
      return { before, after, operation };
    } catch (error) {
      operation.state = "uncertain";
      this.settingsUncertain = true;
      this.save();
      // No replay or CAS loop. Cleanup may only inspect this exact last intent.
      throw error;
    }
  }
  async prepare() {
    const published = release(this.inputs);
    this.state.routerRelease = clone(published);
    this.save();
    const p = await this.currentCredential();
    need(
      eq(p, this.priorC.claudeCredential),
      "Credential differs from independently pinned C prerequisite",
    );
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
      "Drain source qualification/readiness differs",
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
        ready.phaseCReportSha256 === this.inputs.phaseCReportSha256 &&
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
    this.effectiveExpiry = Math.min(
      this.env.executionDeadline,
      this.inputs.window.expiresAt,
      p.expiresAt,
    );
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
    this.originalSettings = this.settingsObservation(
      await this.call("settings", "read"),
    );
    need(
      this.originalSettings.values.warmSessions === false &&
        this.originalSettings.values.warmIdleMs ===
          this.priorC.originalSettings?.values?.warmIdleMs &&
        this.originalSettings.providerRuntime.providerGeneration ===
          this.scope.providerGeneration,
      "Drain must begin at observed cold generation",
    );
    this.state.originalSettings = this.originalSettings;
    this.lastSettings = this.originalSettings;
    await this.startMonitor();
    need(
      this.events.size === 0 && this.sample.openRequests === 0,
      "Drain observation window not empty",
    );
    const pre = await this.ask(
      "Drain prelaunch marker baseline",
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
        kind: "active-drain-marker-baseline",
        runId: this.inputs.runId,
        stage: "before-launch",
      },
    );
    await this.launch();
    const boundary = this.collectionBoundary("Drain post-Open baseline");
    const post = await this.ask(
      "Drain delivery/Open marker baseline",
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
        kind: "active-drain-marker-baseline",
        runId: this.inputs.runId,
        stage: "after-open",
        sessionId: this.session.id,
        notBefore: boundary.notBefore,
      },
    );
    a.unchangedBaseline(pre, post);
    this.record("warm-launch-baselines", { pre, post });
    await this.beginWarm({
      expectedAttempts: ready.attempts,
      expectedUnknownUsage: ready.tokenUsageUnknown,
      expectedFenceRevision: ready.expectedFenceRevision,
      ...(ready.predecessorFenceId
        ? { predecessorFenceId: ready.predecessorFenceId }
        : {}),
    });
  }
  async stopMonitor() {
    if (!this.monitorStarted || this.monitorStopped) return;
    this.monitorAbort.abort();
    await this.call("process", "stop", {}, true);
    await this.monitorTask;
    this.monitorStopped = true;
  }
  async cleanup() {
    if (this.drainCleanup) return this.drainCleanup;
    this.drainCleanup = (async () => {
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
      if (this.promptTask) await this.promptTask;
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
      this.record("active-drain-cleanup", { outcomes });
    })();
    return this.drainCleanup;
  }
  async decide(expected, kind, communication = []) {
    this.deciding = true;
    try {
      return await super.decide(expected, kind, communication);
    } finally {
      this.deciding = false;
    }
  }
  async ask(title, instructions, validate, timeout, context) {
    if (context?.kind === "permission") {
      need(
        this.row && this.pending && context.purpose === "relay-message-read",
        "Only the existing scoped read is allowed",
      );
      need(
        !(this.row.id === "D1" && this.disableStarted),
        "Post-CAS read continuation cannot migrate generation",
      );
      const f = await this.inspectFence();
      need(
        f.attempts - f.rowStartAttempts < 2 &&
          f.attempts - this.state.drainBaseline.attempts < 4,
        "Read continuation allowance exhausted",
      );
    }
    return super.ask(title, instructions, validate, timeout, context);
  }
  async mutate(path, body) {
    if (body?.kind === "prompt")
      need(
        this.row && body.text === this.row.text,
        "Only fixed drain prompt dispatch allowed",
      );
    if (/^\/api\/agent-permissions\/[^/]+\/decision$/.test(path))
      need(
        !(this.row?.id === "D1" && this.disableStarted),
        "No permission dispatch after drain CAS began",
      );
    return super.mutate(path, body);
  }
  async prompt(text, options = {}) {
    need(
      this.row && text === this.row.text && this.run.prompts < 2,
      "Only the next fixed drain prompt allowed",
    );
    return super.prompt(text, options);
  }
  async reserveRow(row) {
    need(
      !this.pending && !this.promptTask,
      "Prior native/client work not joined",
    );
    this.row = row;
    const f = await this.inspectFence(row.id);
    this.rowBefore = new Set(
      [...this.events.values()].map((e) => e.requestId + ":" + e.attemptId),
    );
    const record = {
      rowId: row.id,
      fenceId: this.fenceId,
      phaseId: this.fenceContext.segment.phaseId,
      scopeDigest: digest(this.scope),
      cap: 2,
      state: "intent",
      admittedBefore: f.attempts,
    };
    this.state.rows.push(record);
    this.save();
    return record;
  }
  startPrompt(row) {
    this.promptResult = null;
    // Always observe both promise outcomes; no detached rejection or hidden retry.
    this.promptTask = this.prompt(row.text, row).then(
      (text) => {
        this.promptResult = { text };
      },
      (error) => {
        this.promptResult = { error };
      },
    );
  }
  async joinPrompt() {
    await this.promptTask;
    this.promptTask = null;
    need(
      this.promptResult && !this.promptResult.error && !this.pending,
      "Drain prompt failed/cancelled/uncertain",
    );
  }
  async settleRow(record) {
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
      "Drain process/client settlement missing",
    );
    const rows = attempts([...this.events.values()], this.rowBefore);
    const f = await this.inspectFence(this.row.id);
    need(
      rows.length >= 1 &&
        rows.length <= 2 &&
        f.attempts - record.admittedBefore === rows.length,
      "Drain admitted/raw generation counts differ",
    );
    need(
      rows.every(
        (r) => r.dispatch.mode === (this.row.id === "D1" ? "warm" : "cold"),
      ),
      "Drain mode changed",
    );
    await this.waitFor(
      () =>
        rows.every((r) => this.absent(r.terminal.identity)) &&
        this.sample.processes.length === 0,
      5000,
      "Drained child disappearance unobserved",
    );
    Object.assign(record, {
      state: "settled",
      generations: rows.length,
      settledTokenUsageUnknown: f.tokenUsageUnknown,
      final: rows.at(-1),
      terminalEvidenceSha256: digest({
        prompt: this.state.evidence.filter((e) => e.kind === "prompt").at(-1),
        processProjection: this.sample.projectionSha256,
        fence: f,
      }),
    });
    this.save();
    return rows;
  }
  async counting() {
    const record = await this.reserveRow(ROWS[0]);
    this.startPrompt(ROWS[0]);
    let selected;
    await this.waitFor(
      () => {
        if (this.promptResult)
          throw Error("Counting completed before active CAS; unexercised");
        if (this.deciding || !this.pending || this.sample.openRequests === 0)
          return false;
        const open = attempts(
          [...this.events.values()],
          this.rowBefore,
          false,
        ).filter((r) => r.fetch && !r.native && !r.terminal);
        if (open.length !== 1) return false;
        need(
          open[0].dispatch.mode === "warm",
          "Counting generation is not warm",
        );
        selected = clone(open[0]);
        return true;
      },
      120000,
      "No active counting generation within prompt limit",
    );
    this.state.activeAttempt = selected;
    this.disableStarted = true;
    this.save();
    // The old fence and provider lease remain untouched during this real settings CAS.
    const transition = await this.changeSettings(
      "active-disable",
      false,
      this.originalSettings.values.warmIdleMs,
    );
    need(
      transition.operation,
      "Active disable did not commit a settings transition",
    );
    this.state.activeSettings = {
      before: transition.before,
      after: transition.after,
      intentId: transition.operation.intentId,
    };
    this.save();
    await this.joinPrompt();
    const rows = await this.settleRow(record);
    const active = rows.find(
      (r) =>
        r.dispatch.requestId === selected.dispatch.requestId &&
        r.dispatch.attemptId === selected.dispatch.attemptId,
    );
    need(
      active?.fetch && active.native,
      "Selected active native attempt missing",
    );
    record.overlap = overlap(
      active.fetch,
      active.native,
      transition.before,
      transition.after,
      this.scope,
    );
    record.state = "passed";
    this.state.activeDrain = "observed";
    this.save();
    return record;
  }
  async cold(previous) {
    need(
      this.state.activeDrain === "observed" &&
        this.lastSettings.values.warmSessions === false,
      "Cold continuation lacks successful active drain",
    );
    const warm = await this.closeCurrentFence(previous);
    await this.switchEpoch(this.segments[1]);
    await this.armSegment(this.segments[1], {
      predecessorFenceId: warm.fenceId,
      expectedFenceRevision: warm.revision,
      expectedAttempts: warm.attempts,
      expectedUnknownUsage: warm.tokenUsageUnknown,
    });
    const record = await this.reserveRow(ROWS[1]);
    this.startPrompt(ROWS[1]);
    await this.joinPrompt();
    await this.settleRow(record);
    record.state = "passed";
    this.save();
    return record;
  }
  async startActiveDrain() {
    need(
      this.env.origin === a.ORIGIN &&
        this.env.load() === null &&
        !this.started &&
        !this.stopped,
      "Fresh exclusive drain report required",
    );
    release(this.inputs); // Fixed null gate refuses before any I/O, with no caller override.
    this.started = true;
    this.state.status = "running";
    this.planSegments();
    this.save();
    try {
      await this.prepare();
      const warm = await this.counting();
      const cold = await this.cold(warm);
      await this.finishRows(cold);
      this.run.status = "passed-local-process-observations";
      this.state.status = "active-drain-passed";
      this.state.activeDrainCompleted = true;
      this.state.completed = false;
      this.save();
    } catch (error) {
      this.stopped = true;
      this.state.status = "stopped";
      this.state.reason =
        "Active drain stopped; preserve observations and unresolved intents";
      this.save();
      await this.cleanup();
      throw error;
    }
  }
}
function createClaudeActiveDrainRunner(env, value, ports) {
  return new DrainRunner(env, value, ports);
}
module.exports = {
  createClaudeActiveDrainRunner,
  ROWS,
  PLAN,
  planDigest,
  inputs,
  settings,
  overlap,
};
