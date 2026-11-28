"use strict";

// Cleanup only. Publishing this file grants neither inference nor admin authority.
const { createHash, randomUUID } = require("node:crypto");
const a = require("./owner-phase-a.js");
const b = require("./owner-phase-b.js");
const QUALIFIED_FINALIZATION = null;
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
const CONNECTION = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8";
const TENANT = "00000000-0000-4000-8000-000000000001";
const FIXED = Object.freeze({
  codex: a.PROFILES.codex,
  qwen: a.PROFILES.qwen,
  claude: b.PROFILE,
});
const BINDINGS = Object.freeze(Object.values(FIXED).map((p) => p.binding));
const sha = (raw) => createHash("sha256").update(raw).digest("hex");
const eq = (x, y) => a.canonical(x) === a.canonical(y);
const uuid = (v) =>
  typeof v === "string" &&
  /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const hash = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const int = (v) => Number.isSafeInteger(v) && v >= 0;
const REASONS = Object.freeze({
  "external-failure": "External callback or input failed",
  "callback-aborted": "Finalization callback aborted",
  "callback-timeout": "Finalization callback timed out",
  "dispatch-blocked": "Stopped before revoke dispatch",
  "http-refused": "Finalization API refused",
  "metadata-invalid": "Finalization metadata is invalid",
  "persistence-failed": "Finalization evidence persistence failed",
  "settlement-unresolved": "Revocation unresolved; retain original operation",
  "callbacks-unresolved":
    "Finalization callback cleanup unresolved; preserve evidence",
  "host-stopped": "Finalization host stopped; preserve evidence",
  "cli-input-invalid": "Finalization CLI input invalid",
  "output-failed": "Finalization diagnostic output failed",
});
class KnownFailure extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}
function failure(code) {
  const key = Object.hasOwn(REASONS, code) ? code : "external-failure";
  return new KnownFailure(key, REASONS[key]);
}
function redact(error) {
  if (error instanceof KnownFailure) return error;
  return failure(
    error?.name === "AbortError"
      ? "callback-aborted"
      : error?.name === "TimeoutError"
        ? "callback-timeout"
        : "external-failure",
  );
}
function diagnostic(error) {
  const safe = redact(error);
  return {
    code: safe.code,
    reason: safe.message,
    ...(typeof safe.failureEvidencePersisted === "boolean"
      ? { failureEvidencePersisted: safe.failureEvidencePersisted }
      : {}),
  };
}
function need(v, message) {
  if (!v) throw new KnownFailure("validation-refused", message);
}
function list(v, max, label) {
  need(Array.isArray(v) && v.length < max, label + " incomplete");
  return v;
}
function unique(rows, key, label) {
  need(
    new Set(rows.map((x) => x[key])).size === rows.length,
    label + " ambiguous",
  );
}
function exactly(rows, predicate, label) {
  const found = rows.filter(predicate);
  need(found.length === 1, label + " missing/ambiguous");
  return found[0];
}
function fresh(value, now, notBefore) {
  const time = Date.parse(value);
  need(
    Number.isFinite(time) &&
      time >= notBefore &&
      time >= now - 120000 &&
      time <= now + 5000,
    "Stale observation",
  );
}
function qualification() {
  const q = QUALIFIED_FINALIZATION;
  const source = (s) => typeof s === "string" && /^[a-f0-9]{40}$/.test(s);
  const reportSources = (pins) =>
    pins &&
    eq(Object.keys(pins).sort(), ["a", "b", "c"]) &&
    Object.values(pins).every(source);
  need(
    q &&
      [q.hubSource, q.routerSource].every(source) &&
      reportSources(q.reportHubSources) &&
      reportSources(q.reportRouterSources) &&
      hash(q.observerQualificationSha256),
    "Finalization source/adapters are not qualified",
  );
  return q;
}
function qualified(observer) {
  const q = qualification();
  need(
    observer &&
      observer.qualificationSha256 === q.observerQualificationSha256 &&
      typeof observer.observe === "function",
    "Qualified finalization observer missing",
  );
  return q;
}
function finalizationScope() {
  return {
    tenantId: TENANT,
    hostId: HOST,
    connectionId: CONNECTION,
    executionIdentity: "c4",
    bindingIds: BINDINGS,
    profileIds: Object.values(FIXED).map((p) => p.id),
  };
}
function reportArtifactNames(manifest) {
  need(
    manifest &&
      (!Object.hasOwn(manifest, "activeDrain") ||
        manifest.activeDrain === true),
    "Unknown finalization report selection",
  );
  return [
    "a",
    "b",
    "bInputs",
    "c",
    ...(manifest.activeDrain === true ? ["d"] : []),
  ];
}
function activeDrainReport(d, c, manifest, q) {
  const pins = q.activeDrainReportSources;
  need(
    pins &&
      eq(Object.keys(pins).sort(), ["hubSource", "routerSource"]) &&
      Object.values(pins).every(
        (v) => typeof v === "string" && /^[a-f0-9]{40}$/.test(v),
      ),
    "Active drain historical source pins missing",
  );
  need(
    d?.version === 1 &&
      d.kind === "claude-active-drain" &&
      d.status === "active-drain-passed" &&
      d.activeDrainCompleted === true &&
      d.completed === false &&
      d.activeDrain === "observed" &&
      d.capacity === "unexercised" &&
      d.relay === pins.hubSource &&
      d.router === pins.routerSource,
    "Active drain incomplete/source differs",
  );
  need(
    d.inputs?.phaseAReportSha256 === manifest.pins.a &&
      d.inputs.phaseBReportSha256 === manifest.pins.b &&
      d.inputs.phaseCReportSha256 === manifest.pins.c &&
      d.inputs.phaseCRouterSource === c.router &&
      uuid(d.inputs.runId) &&
      uuid(d.inputs.phaseId) &&
      d.inputs.runId !== c.inputs.runId &&
      d.inputs.phaseId !== c.inputs.phaseId &&
      ["account", "model", "coldReportSha256", "pairedReportSha256"].every(
        (k) => d.inputs[k] === c.inputs[k] && typeof d.inputs[k] === "string",
      ) &&
      hash(d.inputs.exclusiveWindowSha256) &&
      eq(d.claudeCredential, c.claudeCredential),
    "Active drain prerequisite linkage differs",
  );
  need(
    d.scope?.runId === d.inputs.runId &&
      d.scope.hubSource === d.relay &&
      d.scope.routerSource === d.router &&
      d.scope.profileId === b.PROFILE.id &&
      d.scope.profileRevision === 1 &&
      d.scope.bindingId === b.PROFILE.binding &&
      d.scope.credentialId === d.claudeCredential.credential &&
      d.scope.credentialGeneration === d.claudeCredential.generation &&
      d.scope.keyId === d.claudeCredential.key &&
      d.scope.principalId === "key:" + d.claudeCredential.key &&
      d.scope.account === d.inputs.account &&
      d.scope.model === d.inputs.model &&
      d.scope.namespace === "default" &&
      uuid(d.scope.deploymentId) &&
      uuid(d.scope.phaseId) &&
      uuid(d.scope.runtimeInstanceId),
    "Active drain scope differs",
  );
  const original = d.originalSettings,
    final = d.finalSettings;
  need(
    original?.source === d.router &&
      final?.source === d.router &&
      original.values?.warmSessions === false &&
      original.values.warmIdleMs === c.originalSettings.values.warmIdleMs &&
      eq(original.values, final.values) &&
      hash(original.revision) &&
      hash(final.revision) &&
      d.process?.processes?.length === 0 &&
      !Object.hasOwn(d, "control") &&
      d.processEpochs?.length === 3 &&
      d.processEpochs.every(
        (e, i) =>
          e.transition ===
            (i === 2 ? "final-drained-and-joined" : "drained-and-joined") &&
          e.process?.processes?.length === 0 &&
          e.scope?.runtimeInstanceId === d.scope.runtimeInstanceId &&
          e.scope.routerSource === d.router,
      ),
    "Active drain settings/process closure incomplete",
  );
  const settings = d.activeSettings,
    before = settings?.before,
    after = settings?.after;
  need(
    before?.source === d.router &&
      after?.source === d.router &&
      before.values?.warmSessions === true &&
      after.values?.warmSessions === false &&
      eq({ ...before.values, warmSessions: false }, original.values) &&
      eq(after.values, final.values) &&
      after.revision === final.revision &&
      before.revision !== after.revision &&
      hash(before.revision) &&
      [before, after, final].every(
        (v) =>
          v.providerRuntime?.runtimeInstanceId === d.scope.runtimeInstanceId,
      ) &&
      int(before.providerRuntime.providerGeneration) &&
      int(after.providerRuntime.providerGeneration) &&
      before.providerRuntime.providerGeneration !==
        after.providerRuntime.providerGeneration &&
      after.providerRuntime.providerGeneration === d.scope.providerGeneration &&
      final.providerRuntime.providerGeneration === d.scope.providerGeneration,
    "Active drain settings transition differs",
  );
  need(
    d.settingsOperations?.length === 2,
    "Active drain settings intents differ",
  );
  d.settingsOperations.forEach((op, i) =>
    need(
      uuid(op.intentId) &&
        op.state === "confirmed" &&
        hash(op.readbackSha256) &&
        op.kind === (i ? "active-disable" : "enable") &&
        (i
          ? op.intentId === settings.intentId &&
            op.expectedRevision === before.revision &&
            op.afterRevision === after.revision &&
            eq(op.before, before.values) &&
            eq(op.values, after.values)
          : op.expectedRevision === original.revision &&
            op.afterRevision === before.revision &&
            eq(op.before, original.values) &&
            eq(op.values, before.values)),
      "Active drain settings intent not confirmed",
    ),
  );
  need(
    d.rows?.length === 2 && d.fences?.length === 2 && d.subplans?.length === 2,
    "Active drain row/fence inventory differs",
  );
  const baseline = d.drainBaseline;
  need(
    int(baseline?.attempts) && int(baseline.tokenUsageUnknown),
    "Active drain baseline missing",
  );
  d.rows.forEach((row, i) => {
    const f = d.fences[i],
      p = d.subplans[i],
      prev = d.fences[i - 1];
    need(
      row.rowId === "D" + (i + 1) &&
        row.state === "passed" &&
        row.cap === 2 &&
        int(row.generations) &&
        row.generations >= 1 &&
        row.generations <= 2 &&
        hash(row.terminalEvidenceSha256) &&
        hash(row.scopeDigest) &&
        uuid(row.phaseId) &&
        uuid(row.fenceId) &&
        p.kind === (i ? "cold" : "warm") &&
        p.phaseId === row.phaseId &&
        p.fenceId === row.fenceId &&
        p.phaseLimit === 2 &&
        eq(p.rows, [{ id: row.rowId, limit: 2 }]) &&
        hash(p.planDigest) &&
        f.fenceId === row.fenceId &&
        f.phaseId === row.phaseId &&
        f.runId === d.inputs.runId &&
        f.planDigest === p.planDigest &&
        f.phaseLimit === 2 &&
        eq(f.rows, p.rows) &&
        f.activeRowId === row.rowId &&
        f.state === "closed" &&
        f.keyId === d.scope.keyId &&
        f.namespace === d.scope.namespace &&
        f.principalId === d.scope.principalId &&
        f.deploymentId === d.scope.deploymentId &&
        f.selection?.providerGeneration ===
          (i ? after : before).providerRuntime.providerGeneration &&
        f.openRequests === 0 &&
        f.staleOpenRequests === 0 &&
        f.unattributed === 0 &&
        int(f.revision) &&
        f.revision > 0 &&
        int(f.attempts) &&
        int(f.tokenUsageUnknown) &&
        f.tokenUsageUnknown <= f.attempts &&
        f.baselineAttempts === (i ? prev.attempts : baseline.attempts) &&
        f.rowStartAttempts === f.baselineAttempts &&
        row.admittedBefore === f.baselineAttempts &&
        f.attempts === row.admittedBefore + row.generations &&
        f.baselineTokenUsageUnknown ===
          (i ? prev.tokenUsageUnknown : baseline.tokenUsageUnknown) &&
        f.rowStartTokenUsageUnknown === f.baselineTokenUsageUnknown &&
        f.tokenUsageUnknown >= f.baselineTokenUsageUnknown &&
        row.settledTokenUsageUnknown === f.tokenUsageUnknown &&
        (!i || f.predecessorFenceId === prev.fenceId) &&
        row.final?.dispatch?.mode === (i ? "cold" : "warm") &&
        row.final.terminal?.phase === "terminal" &&
        row.final.terminal.requestId === row.final.dispatch.requestId &&
        row.final.terminal.attemptId === row.final.dispatch.attemptId,
      "Active drain admission/terminal continuity differs",
    );
  });
  need(
    d.rows[0].fenceId !== d.rows[1].fenceId &&
      d.rows[0].phaseId !== d.rows[1].phaseId &&
      d.rows[0].phaseId === d.inputs.phaseId &&
      d.rows[1].phaseId === d.scope.phaseId &&
      d.drainUsage?.attempts ===
        d.rows[0].generations + d.rows[1].generations &&
      d.drainUsage.attempts === d.fences[1].attempts - baseline.attempts &&
      d.drainUsage.attempts <= 4 &&
      d.drainUsage.tokenUsageUnknown ===
        d.fences[1].tokenUsageUnknown - baseline.tokenUsageUnknown,
    "Active drain accounting differs",
  );
  need(
    d.fenceOperations?.length === 4 &&
      d.fenceOperations.every(
        (op, i) =>
          uuid(op.operationId) &&
          op.state === "received" &&
          hash(op.receiptSha256) &&
          op.method === (i % 2 ? "close" : "arm") &&
          op.body?.fenceId === d.fences[Math.floor(i / 2)].fenceId,
      ),
    "Active drain fence intents unresolved",
  );
  activeDrainWitness(d);
}
function activeDrainWitness(d) {
  const row = d.rows[0],
    warm = d.processEpochs[1],
    before = d.activeSettings.before,
    after = d.activeSettings.after,
    dispatch = row.final?.dispatch,
    label = (v) => typeof v === "string" && /^[A-Za-z0-9._:-]{1,200}$/.test(v),
    expectedScope = {
      ...d.scope,
      phaseId: row.phaseId,
      providerGeneration: before.providerRuntime.providerGeneration,
    };
  need(
    eq(warm.scope, expectedScope) &&
      row.scopeDigest === sha(a.canonical(warm.scope)) &&
      label(dispatch?.requestId) &&
      label(dispatch.attemptId) &&
      uuid(dispatch.identity?.id) &&
      int(dispatch.identity.pid) &&
      dispatch.identity.pid > 0 &&
      dispatch.identity.pid <= 4194304 &&
      int(dispatch.identity.ppid) &&
      /^[1-9][0-9]{0,19}$/.test(dispatch.identity.startTicks) &&
      int(dispatch.turn) &&
      dispatch.turn > 0 &&
      dispatch.reused === dispatch.turn > 1 &&
      eq(d.activeAttempt?.dispatch, dispatch) &&
      eq(d.activeAttempt?.fetch, row.final.fetch),
    "Active drain selected attempt/scope differs",
  );
  const phases = [
    ["dispatch", "dispatch"],
    ["fetch", "fetch-invoked"],
    ["native", "native-complete"],
    ["terminal", "terminal"],
  ];
  let prior;
  const ns = (v) => typeof v === "string" && /^(?:0|[1-9][0-9]{0,24})$/.test(v);
  for (const [field, phase] of phases) {
    const event = row.final[field];
    need(
      event?.phase === phase &&
        event.requestId === dispatch.requestId &&
        event.attemptId === dispatch.attemptId &&
        event.mode === "warm" &&
        event.accountId === warm.scope.account &&
        event.providerGeneration === warm.scope.providerGeneration &&
        eq(event.identity, dispatch.identity) &&
        event.turn === dispatch.turn &&
        event.reused === dispatch.reused &&
        int(event.sequence) &&
        ns(event.observedMonotonicNs) &&
        (!prior ||
          (event.sequence > prior.sequence &&
            BigInt(event.observedMonotonicNs) >=
              BigInt(prior.observedMonotonicNs))),
      "Active drain warm event identity/order differs",
    );
    const recorded = exactly(
      list(warm.process.events, 129, "Warm process events"),
      (e) =>
        e.phase === phase &&
        e.requestId === dispatch.requestId &&
        e.attemptId === dispatch.attemptId,
      "Active drain warm witness",
    );
    need(eq(recorded, event), "Active drain recorded witness differs");
    prior = event;
  }
  const overlap = row.overlap;
  need(
    overlap &&
      ["fetchNs", "beforeNs", "afterNs", "nativeCompleteNs"].every((k) =>
        ns(overlap[k]),
      ) &&
      overlap.beforeNs === before.providerRuntime.observedMonotonicNs &&
      overlap.afterNs === after.providerRuntime.observedMonotonicNs &&
      overlap.fetchNs === d.activeAttempt?.fetch?.observedMonotonicNs &&
      BigInt(overlap.fetchNs) <= BigInt(overlap.beforeNs) &&
      BigInt(overlap.beforeNs) <= BigInt(overlap.afterNs) &&
      BigInt(overlap.afterNs) < BigInt(overlap.nativeCompleteNs),
    "Active drain overlap unobserved",
  );
  need(
    row.final.native.observedMonotonicNs === overlap.nativeCompleteNs,
    "Active drain native witness differs",
  );
}
function parseReports(raws, manifest, now, q) {
  need(
    manifest?.version === 1 &&
      int(manifest.cleanupDeadline) &&
      manifest.cleanupDeadline > now &&
      manifest.cleanupDeadline <= now + 900000 &&
      hash(manifest.decisionSha256),
    "Cleanup-only deadline/scope invalid",
  );
  need(
    eq(Object.keys(raws).sort(), reportArtifactNames(manifest).sort()) &&
      eq(Object.keys(manifest.pins || {}).sort(), Object.keys(raws).sort()),
    "Exact selected artifacts required",
  );
  const r = {};
  for (const name of Object.keys(raws)) {
    need(
      typeof raws[name] === "string" &&
        Buffer.byteLength(raws[name]) <= 8 * 1024 * 1024 &&
        hash(manifest.pins[name]) &&
        sha(raws[name]) === manifest.pins[name],
      "Artifact hash/bound mismatch",
    );
    r[name] = JSON.parse(raws[name]);
  }
  for (const name of ["a", "b", "c"])
    need(
      r[name]?.version === 1 &&
        r[name].relay === q.reportHubSources[name] &&
        r[name].router === q.reportRouterSources[name] &&
        r[name].completed === false,
      "Report source/shape differs",
    );
  need(
    r.a.phaseACompleted === true &&
      ["phase-a-passed", "phase-a-and-grants-closed"].includes(r.a.status),
    "A incomplete",
  );
  need(
    r.b.kind === "claude-phase-b" &&
      r.b.status === "phase-b-passed" &&
      r.b.phaseBCompleted === true,
    "B incomplete",
  );
  need(
    r.c.kind === "claude-phase-c" &&
      r.c.status === "phase-c-passed" &&
      r.c.phaseCCompleted === true,
    "C incomplete",
  );
  need(
    r.bInputs.kind === "claude-phase-b-inputs" &&
      r.bInputs.version === 1 &&
      r.bInputs.phaseAReportSha256 === manifest.pins.a &&
      eq(r.bInputs.inputs, r.b.inputs),
    "B retained input linkage differs",
  );
  need(
    r.c.inputs?.phaseAReportSha256 === manifest.pins.a &&
      r.c.inputs.phaseBReportSha256 === manifest.pins.b &&
      r.c.inputs.account === r.b.inputs.account &&
      r.c.inputs.model === r.b.inputs.model,
    "C report linkage differs",
  );
  need(
    r.a.replacement?.carriedPrompts === 1 &&
      r.a.replacement.carriedAttempts === 2,
    "Original accounting missing",
  );
  const prior = a.stoppedPredecessor(r.a.predecessor?.raw);
  const plans = {
    a: [
      ["codex", false],
      ["qwen", false],
      ["codex", true],
    ],
    b: [
      ["claude", false],
      ["claude", true],
    ],
    c: [["claude", false]],
  };
  for (const name of Object.keys(plans)) {
    need(r[name].runs?.length === plans[name].length, "Run set differs");
    r[name].runs.forEach((run, i) =>
      need(
        run.provider === plans[name][i][0] &&
          run.effort === plans[name][i][1] &&
          uuid(run.runId) &&
          run.status ===
            {
              a: "passed-with-live-wire-unobserved",
              b: "passed-with-admission-fence-live-wire-unobserved",
              c: "passed-local-process-observations",
            }[name],
        "Run not qualified",
      ),
    );
  }
  need(
    r.b.attemptFence?.phases?.length === 2 &&
      r.b.attemptFence.phases.every(
        (p) =>
          p.closed === true &&
          p.closeReceipt?.fence?.closedAt > 0 &&
          p.closeReceipt.openRequests === 0 &&
          p.closeReceipt.staleOpenRequests === 0 &&
          p.closeReceipt.unattributed === 0,
      ),
    "B fence cleanup incomplete",
  );
  need(
    r.c.rows?.length === 4 &&
      r.c.rows.every(
        (row, i) =>
          row.rowId === "P" + (i + 1) &&
          row.state === "passed" &&
          int(row.generations) &&
          row.generations >= 1 &&
          row.generations <= [2, 2, 3, 2][i],
      ) &&
      r.c.fences?.length === 2 &&
      r.c.fences.every(
        (f) =>
          f.state === "closed" &&
          f.openRequests === 0 &&
          f.staleOpenRequests === 0 &&
          f.unattributed === 0,
      ),
    "C rows/fences incomplete",
  );
  need(
    r.c.originalSettings?.values?.warmSessions === false &&
      int(r.c.originalSettings.values.warmIdleMs),
    "Original cold settings missing",
  );
  if (manifest.activeDrain === true) activeDrainReport(r.d, r.c, manifest, q);
  const selected = {
    ...r.a.successorCredentials?.profiles,
    claude: r.b.claudeCredential,
  };
  need(
    eq(Object.keys(selected).sort(), Object.keys(FIXED).sort()),
    "Fixed credential scopes missing",
  );
  for (const [provider, p] of Object.entries(selected)) {
    need(
      p?.id === FIXED[provider].id &&
        p.binding === FIXED[provider].binding &&
        uuid(p.credential) &&
        /^key_[a-f0-9]{12,32}$/.test(p.key) &&
        int(p.generation) &&
        p.generation >= 1 &&
        p.generation < 128 &&
        int(p.revision) &&
        p.revision > 0,
      "Selected credential differs",
    );
  }
  for (const key of ["credential", "key", "generation", "binding"])
    need(
      r.c.claudeCredential?.[key] === selected.claude[key],
      "Claude credential changed between reports",
    );
  const launches = [];
  for (const [name, count] of [
    ["a", 5],
    ["b", 3],
    ["c", 1],
    ...(manifest.activeDrain === true ? [["d", 1]] : []),
  ]) {
    const evidence = list(r[name].evidence, 10000, "Report evidence"),
      rows = evidence.filter((e) => e.kind === "launch");
    need(rows.length === count, "Launch inventory differs");
    for (const l of rows) {
      const provider = Object.keys(FIXED).find(
          (p) => FIXED[p].id === l.profileId,
        ),
        p = selected[provider];
      need(
        p &&
          uuid(l.id) &&
          uuid(l.sessionId) &&
          l.revision === 1 &&
          Number.isFinite(Date.parse(l.at)) &&
          evidence.some(
            (e) => e.kind === "closed" && e.sessionId === l.sessionId,
          ),
        "Launch not closed in report",
      );
      const d = evidence.filter(
        (e) => e.kind === "delivery" && e.sessionId === l.sessionId,
      );
      need(
        d.length > 0 &&
          d.every(
            (e) =>
              e.credentialId === p.credential &&
              e.credentialGeneration === p.generation &&
              e.principal === "key:" + p.key,
          ),
        "Launch credential linkage differs",
      );
      launches.push({
        launchId: l.id,
        sessionId: l.sessionId,
        profileId: l.profileId,
        bindingId: p.binding,
        credentialId: p.credential,
        generation: p.generation,
        launchedAt: l.at,
      });
    }
  }
  launches.push({
    launchId: prior.launch.id,
    sessionId: prior.launch.sessionId,
    profileId: a.PROFILES.codex.id,
    bindingId: a.PROFILES.codex.binding,
    credentialId: a.PROFILES.codex.credential,
    generation: 1,
    launchedAt: prior.launch.at,
  });
  unique(launches, "launchId", "Launches");
  unique(launches, "sessionId", "Sessions");
  const markers = [];
  for (const [name, provider] of [
    ["a", "codex"],
    ["a", "qwen"],
    ["b", "claude"],
  ]) {
    const run = r[name].runs.find((v) => v.provider === provider && !v.effort);
    const m = r[name].evidence
      .filter(
        (e) =>
          e.kind === "operator-observation" &&
          e.marker?.provider === provider &&
          e.marker.runId === run.runId &&
          e.marker.phase === "recovered",
      )
      .at(-1)?.marker;
    need(m, "Recovered marker missing");
    need(
      int(m.files?.effects?.bytes) && m.files.effects.bytes > 0,
      "Recovered marker byte count missing",
    );
    markers.push(
      a.marker(
        m,
        provider,
        run.runId,
        "recovered",
        Date.parse(m.observedUtc),
        selected,
      ),
    );
  }
  const cm = exactly(
    r.c.evidence,
    (e) => e.kind === "warm-launch-baselines",
    "C marker baseline",
  ).post;
  markers.push(
    a.marker(
      cm,
      "claude",
      r.c.inputs.runId,
      "baseline",
      Date.parse(cm?.observedUtc),
      selected,
    ),
  );
  if (manifest.activeDrain === true) {
    need(
      r.d.runs?.length === 1 &&
        r.d.runs[0].provider === "claude" &&
        r.d.runs[0].effort === false &&
        r.d.runs[0].runId === r.d.inputs.runId &&
        r.d.runs[0].status === "passed-local-process-observations",
      "Active drain run incomplete",
    );
    const baseline = exactly(
      r.d.evidence,
      (e) => e.kind === "warm-launch-baselines",
      "D marker baseline",
    );
    for (const key of ["pre", "post"])
      a.marker(
        baseline[key],
        "claude",
        r.d.inputs.runId,
        "baseline",
        Date.parse(baseline[key]?.observedUtc),
        selected,
      );
    markers.push(baseline.post);
  }
  return {
    reports: r,
    selected,
    launches,
    markers,
    originalIdle: r.c.originalSettings.values.warmIdleMs,
  };
}

function validateFinalizationInputs({ reports, manifest }) {
  const q = qualification(),
    raw = structuredClone(reports),
    selected = structuredClone(manifest),
    data = parseReports(raw, selected, Date.now(), q);
  return structuredClone({
    version: 1,
    purpose: "canary-finalization",
    scope: finalizationScope(),
    reportPins: selected.pins,
    decisionSha256: selected.decisionSha256,
    cleanupDeadline: selected.cleanupDeadline,
    launches: data.launches,
    markers: data.markers,
    originalIdle: data.originalIdle,
    sources: q,
  });
}
function createFinalization(options) {
  const q = qualified(options.observer);
  need(
    typeof options.authenticatedFetch === "function" &&
      typeof options.save === "function" &&
      typeof options.observeFinalization !== "function",
    "Fixed finalization adapters required",
  );
  const now = options.now || Date.now,
    sleep = options.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const manifest = structuredClone(options.manifest),
    raw = structuredClone(options.reports);
  const data = parseReports(raw, manifest, now(), q);
  const fetch = options.authenticatedFetch,
    observe = options.observer.observe,
    save = options.save;
  const state = {
    version: 1,
    kind: "canary-finalization",
    completed: false,
    status: "prepared",
    stage: "preparation",
    sources: q,
    reportPins: manifest.pins,
    decisionSha256: manifest.decisionSha256,
    cleanupDeadline: manifest.cleanupDeadline,
    intents: [],
    evidence: [],
    capacity: data.reports.c.capacity,
    activeDrain: data.reports.c.activeDrain,
    ...(manifest.activeDrain === true
      ? {
          selectedActiveDrain: {
            reportSha256: manifest.pins.d,
            hubSource: data.reports.d.relay,
            routerSource: data.reports.d.router,
            status: data.reports.d.status,
            activeDrain: data.reports.d.activeDrain,
          },
        }
      : {}),
  };
  const controller = new AbortController();
  let started = false,
    stopping = false;
  const check = (readback = false) => {
    need(
      !controller.signal.aborted &&
        now() < manifest.cleanupDeadline &&
        (readback || !stopping),
      "Cleanup stopped/deadline elapsed",
    );
  };
  const persist = () => {
    let result;
    try {
      result = save(structuredClone(state));
    } catch {
      throw failure("persistence-failed");
    }
    need(
      !result || typeof result.then !== "function",
      "Durable save must be synchronous",
    );
  };
  const scope = Object.freeze(finalizationScope());
  async function api(path, method = "GET", body, readback = false, intent) {
    check(readback);
    const session = data.launches.find(
      (l) =>
        path === `/api/agent-sessions/${l.sessionId}` ||
        path === `/api/agent-sessions/${l.sessionId}/control`,
    );
    const credential =
      /^\/api\/router-credentials\/keys\/([a-f0-9-]{36})$/.exec(path);
    const permitted =
      (method === "GET" &&
        (path === "/api/router-credentials" ||
          session ||
          (credential && knownCredentials.has(credential[1])))) ||
      (method === "POST" &&
        BINDINGS.some((id) => path === `/api/router-credentials/${id}/revoke`));
    need(permitted, "Finalization API path forbidden");
    const signal = AbortSignal.any([
      controller.signal,
      AbortSignal.timeout(
        Math.max(1, Math.min(15000, manifest.cleanupDeadline - now())),
      ),
    ]);
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: { "content-type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal,
    });
    if (intent) {
      intent.dispatchDisposition = "http-response";
      intent.httpStatus =
        Number.isInteger(response.status) &&
        response.status >= 100 &&
        response.status <= 599
          ? response.status
          : null;
      persist();
    }
    if (!response.ok) throw failure("http-refused");
    if (!response.headers.get("content-type")?.includes("application/json"))
      throw failure("metadata-invalid");
    const text = await response.text();
    check(readback);
    need(
      Buffer.byteLength(text) <= 2 * 1024 * 1024,
      "Metadata response oversized",
    );
    try {
      return JSON.parse(text);
    } catch {
      throw failure("metadata-invalid");
    }
  }
  const knownCredentials = new Set();
  function noConsumers(rows, detail = false) {
    list(rows, detail ? 128 : 1000, "Consumers");
    need(
      rows.every(
        (c) =>
          c &&
          uuid(c.id) &&
          (detail || uuid(c.binding_id)) &&
          ["active", "released", "blocked"].includes(c.state),
      ),
      "Unknown consumer shape",
    );
    need(
      !rows.some(
        (c) =>
          (detail || BINDINGS.includes(c.binding_id)) && c.state === "active",
      ),
      "Active consumer remains",
    );
  }
  async function inventory(provider) {
    const all = await api("/api/router-credentials"),
      p = data.selected[provider];
    list(all.bindings, 500, "Bindings");
    list(all.credentials, 1000, "Credentials");
    noConsumers(all.consumers);
    const binding = exactly(all.bindings, (v) => v.id === p.binding, "Binding");
    need(
      binding.host_id === HOST &&
        binding.connection_id === CONNECTION &&
        binding.execution_identity === "c4" &&
        eq(binding.grant_spec, {
          routes: ["canary-pi-" + provider],
          access: [provider === "qwen" ? "local" : "subscription"],
          lifetimeSeconds: 86400,
        }) &&
        binding.generation === p.generation &&
        int(binding.revision) &&
        binding.revision > 0 &&
        binding.pending_credential_id === null,
      "Binding scope/generation changed",
    );
    const generations = all.credentials
      .filter((v) => v.binding_id === p.binding)
      .sort((x, y) => x.generation - y.generation);
    need(
      generations.length === p.generation &&
        generations.every(
          (c, i) =>
            uuid(c.id) &&
            /^key_[a-f0-9]{12,32}$/.test(c.router_key_id) &&
            c.generation === i + 1 &&
            c.namespace === "default",
        ),
      "Credential generations incomplete",
    );
    unique(generations, "id", "Credentials");
    unique(generations, "router_key_id", "Principals");
    need(
      generations.at(-1).id === p.credential &&
        generations.at(-1).router_key_id === p.key,
      "Last credential changed",
    );
    for (const c of generations) knownCredentials.add(c.id);
    const details = [];
    for (const c of generations) {
      const d = await api("/api/router-credentials/keys/" + c.id),
        k = d.credential,
        db = d.binding;
      noConsumers(d.consumers, true);
      list(d.operations, 32, "Credential operations");
      unique(d.operations, "id", "Credential operations");
      need(
        k?.id === c.id &&
          k.bindingId === p.binding &&
          k.connectionId === CONNECTION &&
          k.generation === c.generation &&
          k.routerKeyId === c.router_key_id &&
          k.namespace === "default" &&
          k.state === c.state &&
          k.revokeRequested === c.revoke_requested &&
          k.cleanupPending === c.cleanup_pending &&
          k.failureCode === c.failure_code &&
          db?.id === p.binding &&
          db.hostId === HOST &&
          db.executionIdentity === "c4" &&
          db.enabled === binding.enabled &&
          db.state === binding.state &&
          db.revision === binding.revision &&
          db.generation === p.generation &&
          db.currentCredentialId === binding.current_credential_id &&
          db.pendingCredentialId === null,
        "Detail/inventory disagreement",
      );
      need(
        d.operations.every(
          (o) => uuid(o.id) && ["ready", "revoked", "failed"].includes(o.state),
        ),
        "Outstanding/unknown credential operation",
      );
      details.push(d);
    }
    const allRevoked = details.every((d) => terminalCredential(d.credential));
    // Migration008 has no 'expired' state. Expiry reconciliation leaves an
    // enabled FAILED binding with null pointers after known keys are revoked.
    const ready =
      binding.enabled === true &&
      binding.state === "ready" &&
      binding.revision === p.revision &&
      binding.current_credential_id === p.credential &&
      details.at(-1).credential.state === "ready" &&
      details.at(-1).credential.revokeRequested === false &&
      details.at(-1).credential.cleanupPending === false &&
      details.at(-1).credential.failureCode === null &&
      details.slice(0, -1).every((d) => terminalCredential(d.credential));
    const retired =
      binding.enabled === true &&
      binding.state === "failed" &&
      binding.revision === p.revision &&
      binding.current_credential_id === null &&
      allRevoked;
    const already =
      binding.enabled === false &&
      binding.state === "revoked" &&
      binding.revision === p.revision + 1 &&
      binding.current_credential_id === null &&
      allRevoked;
    need(
      ready || retired || already,
      "Binding retirement/revocation unresolved",
    );
    return {
      provider,
      bindingId: p.binding,
      revision: binding.revision,
      enabled: binding.enabled,
      alreadyRevoked: already,
      generations: generations.map((c) => ({
        id: c.id,
        generation: c.generation,
        routerKeyId: c.router_key_id,
      })),
    };
  }
  function terminalCredential(c) {
    return (
      c.state === "revoked" &&
      c.revokeRequested === true &&
      c.cleanupPending === false &&
      c.failureCode === null
    );
  }
  async function closedSessions() {
    for (const l of data.launches) {
      const s = await api(`/api/agent-sessions/${l.sessionId}`),
        c = await api(`/api/agent-sessions/${l.sessionId}/control`);
      need(
        s?.id === l.sessionId &&
          s.state === "closed" &&
          int(s.generation) &&
          s.generation > 0 &&
          c?.lease === null,
        "Session/control not closed",
      );
    }
  }
  async function observation(stage, notBefore) {
    check();
    const deadline = Math.min(manifest.cleanupDeadline, now() + 120000);
    const context = {
      ...structuredClone({
        version: 1,
        stage,
        reportPins: manifest.pins,
        decisionSha256: manifest.decisionSha256,
        sources: q,
        scope,
        launches: data.launches,
        markers: data.markers,
        originalIdle: data.originalIdle,
        notBefore,
        deadline,
      }),
      signal: AbortSignal.any([
        controller.signal,
        AbortSignal.timeout(Math.max(1, deadline - now())),
      ]),
    };
    const v = structuredClone(await observe(context));
    check();
    fresh(v?.observedUtc, now(), notBefore);
    need(
      v.version === 1 &&
        v.stage === stage &&
        eq(v.reportPins, manifest.pins) &&
        eq(v.scope, scope) &&
        v.hubSource === q.hubSource &&
        v.routerSource === q.routerSource &&
        v.decisionSha256 === manifest.decisionSha256 &&
        v.noFurtherUse === true &&
        hash(v.provenance?.querySha256) &&
        hash(v.provenance?.projectionSha256) &&
        typeof v.provenance?.databaseIdentity === "string" &&
        v.provenance.databaseIdentity.length > 0 &&
        v.provenance.databaseIdentity.length <= 200,
      "Finalization observation scope/provenance differs",
    );
    need(
      v.unlistedLaunches === 0 &&
        v.unlistedSessions === 0 &&
        v.unresolvedOperations === 0,
      "Unreported or unresolved scoped work",
    );
    need(
      v.sessions?.length === data.launches.length,
      "Session observation incomplete",
    );
    unique(v.sessions, "sessionId", "Observed sessions");
    for (const l of data.launches) {
      const s = exactly(
        v.sessions,
        (s) => s.sessionId === l.sessionId,
        "Observed session",
      );
      need(
        s.state === "closed" &&
          s.pendingCommands === 0 &&
          s.pendingPermissions === 0,
        "Outstanding session work",
      );
    }
    need(v.bindings?.length === 3, "Binding observation incomplete");
    unique(v.bindings, "bindingId", "Observed bindings");
    for (const id of BINDINGS) {
      const row = exactly(
        v.bindings,
        (x) => x.bindingId === id,
        "Observed binding",
      );
      need(
        row.activeConsumers === 0 &&
          row.openRequests === 0 &&
          row.staleOpenRequests === 0 &&
          row.unattributed === 0 &&
          (id === b.PROFILE.binding
            ? row.fenceState === "closed"
            : ["closed", "not-applicable"].includes(row.fenceState)),
        "Outstanding/unknown binding work",
      );
    }
    need(
      v.router?.warmSessions === false &&
        v.router.warmIdleMs === data.originalIdle &&
        hash(v.router.settingsRevision) &&
        Array.isArray(v.router.processes) &&
        v.router.processes.length === 0,
      "Cold/process cleanup incomplete",
    );
    const projection = {
      version: 1,
      stage,
      reportPins: v.reportPins,
      scope: v.scope,
      hubSource: v.hubSource,
      routerSource: v.routerSource,
      decisionSha256: v.decisionSha256,
      noFurtherUse: true,
      observedUtc: v.observedUtc,
      unlistedLaunches: 0,
      unlistedSessions: 0,
      unresolvedOperations: 0,
      sessions: v.sessions.map((s) => ({
        sessionId: s.sessionId,
        state: "closed",
        pendingCommands: 0,
        pendingPermissions: 0,
      })),
      bindings: v.bindings.map((x) => ({
        bindingId: x.bindingId,
        activeConsumers: 0,
        openRequests: 0,
        staleOpenRequests: 0,
        unattributed: 0,
        fenceState: x.fenceState,
      })),
      router: {
        warmSessions: false,
        warmIdleMs: v.router.warmIdleMs,
        settingsRevision: v.router.settingsRevision,
        processes: [],
      },
    };
    if (stage === "renderer") {
      need(
        v.launches?.length === data.launches.length &&
          v.markers?.length === data.markers.length,
        "Renderer/marker inventory incomplete",
      );
      unique(v.launches, "launchId", "Renderer launches");
      projection.launches = data.launches.map((l) => {
        const row = exactly(
          v.launches,
          (x) => x.launchId === l.launchId,
          "Renderer launch",
        );
        need(
          ["sessionId", "bindingId", "credentialId", "generation"].every(
            (k) => row[k] === l[k],
          ) &&
            uuid(row.commandId) &&
            ["absent", "not-current"].includes(row.status) &&
            row.fileAbsent === true &&
            Date.parse(row.cleanedAt) >= Date.parse(l.launchedAt) - 5000 &&
            Date.parse(row.cleanedAt) <= Date.parse(v.observedUtc) + 5000,
          "Renderer receipt/absence differs",
        );
        // A historical removal can legitimately find a successor credential in
        // the shared binding file. Keep that immutable receipt; fileAbsent is
        // a separate fresh observation, never inferred from not-current.
        if (row.status === "not-current" || row.reference !== undefined) {
          const reference = row.reference,
            target = state.targets.find((t) => t.bindingId === l.bindingId),
            credential = target?.generations.find(
              (c) => c.id === l.credentialId,
            );
          need(
            reference &&
              eq(Object.keys(reference).sort(), [
                "creationOperationId",
                "credentialId",
                "generation",
                "keyId",
                "secretVersion",
              ]) &&
              reference.credentialId === l.credentialId &&
              reference.generation === l.generation &&
              credential?.generation === l.generation &&
              reference.keyId === credential.routerKeyId &&
              int(reference.secretVersion) &&
              reference.secretVersion > 0 &&
              uuid(reference.creationOperationId),
            "Renderer immutable reference differs",
          );
          need(
            row.status !== "not-current" ||
              target.generations.some((c) => c.generation > l.generation),
            "Renderer not-current lacks a recorded successor generation",
          );
        }
        return {
          ...l,
          commandId: row.commandId,
          status: row.status,
          ...(row.reference === undefined
            ? {}
            : { reference: structuredClone(row.reference) }),
          fileAbsent: true,
          cleanedAt: row.cleanedAt,
        };
      });
      projection.markers = data.markers.map((old) => {
        const m = exactly(
          v.markers,
          (m) => m.provider === old.provider && m.runId === old.runId,
          "Marker",
        );
        fresh(m.observedUtc, now(), notBefore);
        const current = a.marker(
          m,
          old.provider,
          old.runId,
          old.phase,
          now(),
          data.selected,
        );
        need(
          eq(current.files, old.files) && eq(current.commands, old.commands),
          "Preserved marker changed",
        );
        return current;
      });
    }
    // Provenance is operator-attributed, not independently authenticated DB access.
    state.evidence.push({
      ...projection,
      provenance: {
        querySha256: v.provenance.querySha256,
        projectionSha256: v.provenance.projectionSha256,
        databaseIdentity: v.provenance.databaseIdentity,
      },
      attribution:
        "operator-scoped observation; not independent host/DB measurement",
    });
    persist();
  }
  async function settle(intent) {
    const deadline = Math.min(manifest.cleanupDeadline, now() + 120000);
    while (now() < deadline) {
      check(true);
      let complete = true;
      const readback = [];
      for (const c of intent.target.generations) {
        const v = await api(
            "/api/router-credentials/keys/" + c.id,
            "GET",
            undefined,
            true,
          ),
          d = v?.credential,
          t = v?.binding;
        const states = [
          "requested",
          "issued",
          "escrowed",
          "installing",
          "ready",
          "rotation_pending",
          "revocation_pending",
          "revoked",
          "failed",
          "unknown",
        ];
        const known = (value, values) =>
          values.includes(value) ? value : "invalid";
        const identifier = (value) =>
          value === null || uuid(value) ? value : "invalid";
        const boolean = (value) =>
          typeof value === "boolean" ? value : "invalid";
        const operationsBounded =
          Array.isArray(v?.operations) && v.operations.length < 32;
        const matches = operationsBounded
          ? v.operations.filter((o) => o?.id === intent.body.operationId)
          : [];
        const observedOperation = matches[0];
        readback.push({
          requestedCredentialId: c.id,
          credentialId: identifier(d?.id),
          bindingId: identifier(d?.bindingId),
          generation: int(d?.generation) ? d.generation : null,
          routerKeyId:
            typeof d?.routerKeyId === "string" &&
            /^key_[a-f0-9]{12,32}$/.test(d.routerKeyId)
              ? d.routerKeyId
              : "invalid",
          state: known(d?.state, states),
          revokeRequested: boolean(d?.revokeRequested),
          cleanupPending: boolean(d?.cleanupPending),
          failureCodePresent: d?.failureCode !== null,
          binding: {
            id: identifier(t?.id),
            enabled: boolean(t?.enabled),
            state: known(t?.state, states),
            revision: int(t?.revision) ? t.revision : null,
            currentCredentialId: identifier(t?.currentCredentialId),
            pendingCredentialId: identifier(t?.pendingCredentialId),
          },
          operationsBounded,
          operationMatches: operationsBounded ? matches.length : null,
          operation: observedOperation
            ? {
                id: intent.body.operationId,
                kind: known(observedOperation.kind, [
                  "issue",
                  "rotate",
                  "revoke",
                  "revoke-binding",
                ]),
                state: known(observedOperation.state, states),
                failureCodePresent: observedOperation.failureCode !== null,
              }
            : null,
        });
        intent.lastReadback = {
          observedUtc: new Date(now()).toISOString(),
          generations: structuredClone(readback),
        };
        persist();
        noConsumers(v?.consumers, true);
        list(v?.operations, 32, "Operations");
        unique(v.operations, "id", "Operations");
        need(
          d?.id === c.id &&
            d.bindingId === intent.target.bindingId &&
            d.generation === c.generation &&
            d.routerKeyId === c.routerKeyId &&
            t?.id === intent.target.bindingId &&
            t.generation === intent.target.generations.length &&
            [intent.target.revision, intent.expectedRevision].includes(
              t.revision,
            ),
          "Revocation readback identity changed",
        );
        const op = v.operations.find((x) => x.id === intent.body.operationId);
        if (op)
          need(op.kind === "revoke-binding", "Revocation operation changed");
        if (c.generation === 1)
          complete &&= op?.state === "revoked" && op.failureCode === null;
        complete &&=
          terminalCredential(d) &&
          t.enabled === false &&
          t.state === "revoked" &&
          t.revision === intent.expectedRevision &&
          t.currentCredentialId === null &&
          t.pendingCredentialId === null;
      }
      if (complete) {
        intent.status = "confirmed";
        intent.readback = {
          observedUtc: new Date(now()).toISOString(),
          generations: readback,
        };
        persist();
        return;
      }
      // An HTTP refusal is not proof that the Hub performed no work. Keep the
      // original operation and one observed readback, without a retry/poll loop.
      if (intent.status === "http-refused") break;
      await sleep(Math.min(2000, Math.max(0, deadline - now())));
    }
    throw failure("settlement-unresolved");
  }
  return {
    stop() {
      stopping = true;
    },
    abort() {
      stopping = true;
      controller.abort();
    },
    async run() {
      need(!started, "Finalization is single-use");
      started = true;
      persist();
      try {
        check();
        state.stage = "preflight-sessions";
        await closedSessions();
        state.stage = "preflight-observation";
        await observation("preflight", now());
        state.stage = "credential-inventory";
        const targets = [];
        for (const provider of Object.keys(FIXED))
          targets.push(await inventory(provider));
        state.targets = targets;
        state.status = "revoking";
        persist();
        for (const target of targets) {
          state.stage = "binding-preflight";
          state.bindingId = target.bindingId;
          check();
          need(
            eq(await inventory(target.provider), target),
            "Reviewed binding changed before dispatch",
          );
          if (target.alreadyRevoked) {
            state.evidence.push({ kind: "already-revoked", target });
            persist();
            continue;
          }
          const body = { operationId: randomUUID(), revision: target.revision };
          const intent = {
            target,
            body,
            bodySha256: sha(JSON.stringify(body)),
            path: `/api/router-credentials/${target.bindingId}/revoke`,
            expectedRevision: target.revision + (target.enabled ? 1 : 0),
            status: "sending",
            at: now(),
          };
          state.intents.push(intent);
          state.intentIndex = state.intents.length - 1;
          state.stage = "revoke-dispatch";
          persist();
          try {
            check();
          } catch (error) {
            intent.status = "not-dispatched";
            intent.dispatchDisposition = "not-dispatched";
            intent.dispatchError = diagnostic(error);
            persist();
            throw error;
          }
          try {
            await api(intent.path, "POST", body, true, intent);
            intent.status = "received";
          } catch (error) {
            intent.dispatchError = diagnostic(error);
            intent.status =
              intent.dispatchError.code === "dispatch-blocked"
                ? "not-dispatched"
                : intent.dispatchError.code === "http-refused"
                  ? "http-refused"
                  : "dispatch-uncertain";
            intent.dispatchDisposition =
              intent.status === "not-dispatched"
                ? "not-dispatched"
                : intent.dispatchDisposition || "transport-uncertain";
            persist();
            if (intent.status === "not-dispatched") throw error;
          }
          persist();
          state.stage = "revocation-readback";
          await settle(intent);
        }
        check();
        state.stage = "final-sessions";
        await closedSessions();
        const notBefore = now() + 5000;
        state.rendererNotBefore = notBefore;
        persist();
        state.stage = "renderer-observation";
        await observation("renderer", notBefore);
        state.stage = "complete";
        state.status = "canary-finalization-passed";
        state.completed = false;
        persist();
        return structuredClone(state);
      } catch (error) {
        const safe = redact(error);
        state.status = "finalization-unresolved";
        state.completed = false;
        state.reason = diagnostic(error);
        try {
          persist();
          safe.failureEvidencePersisted = true;
        } catch {
          safe.failureEvidencePersisted = false;
        }
        throw safe;
      }
    },
  };
}
module.exports = {
  createFinalization,
  validateFinalizationInputs,
  reportArtifactNames,
  qualified,
  sha,
  BINDINGS,
  failure,
  diagnostic,
  redact,
};
