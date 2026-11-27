"use strict";

// Named Claude entry; no browser mounting, credentials, issuance or B0 execution.
const a = require("./owner-phase-a.js");
const { createHash } = require("node:crypto");
const PROFILE = Object.freeze({
  id: "fdb56ca5-908b-40ee-94b3-aac735dd8622",
  binding: "a8f6211c-d7e2-4ffd-824d-b158e349a8d4",
  protocol: "anthropic-messages",
  definitionHash:
    "b1fdad80cf111a6aa1123139ee9dbcb8bd060a8b423ea427d1d43205915ac01d",
});
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
const CONNECTION = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8";
const TENANT = "00000000-0000-4000-8000-000000000001";
const PINS = Object.freeze({
  hub: "5f90d03a6ed291e6c62dfde75938ba07c20d8902",
  router: "4e4bb34e2f968f9450aabe9cf228d3e70c8bab71",
  holder: "64dc50f47a4867a6d9a7afc2e7d5ae24068c59af",
  binary: "d406b1f363f590b386ab3eaea4a8cb350c5ac9c49a3dcb26acc34034dfcf6926",
  piManifest:
    "d0bf7f9d44a2ae992bb1fbd87f7f2043c7d582ad563078bd13d713ebcd211a0d",
  profiles: "928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4",
});
// Exact frozen packet text. Only marker commands and completed branch IDs vary.
const TEXT = Object.freeze({
  ready: "Reply exactly READY. Do not use tools or modify files.",
  markerPrefix:
    "Request permission to run exactly the following shell command once. Do not run any other command, change the command, retry after denial, or modify any other file. After the decision, report the outcome and stop. COMMAND: ",
  branch:
    "Reply exactly BRANCH_OK. Do not use tools, repeat earlier commands, or modify files.",
  compact:
    "Reply exactly COMPACT_OK. Do not use tools, repeat earlier commands, or modify files.",
  cancel:
    "Without using tools or modifying files, count upward from 1, one integer per line, until 500.",
  recovered:
    "Reply exactly RECOVERED_OK. Do not use tools, repeat earlier commands, or modify files.",
  low: "Reply exactly EFFORT_LOW_OK. Do not use tools, access files or modify anything.",
  highProbe:
    "Reply exactly EFFORT_HIGH_PROBE. Do not use tools, access files or modify anything.",
  high: "Reply exactly EFFORT_HIGH_OK. Do not use tools, access files or modify anything.",
});
const uuid = (v) =>
  typeof v === "string" &&
  /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const hash = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const label = (v) =>
  typeof v === "string" && /^[A-Za-z0-9._:-]{1,200}$/.test(v);
const integer = (v) => Number.isSafeInteger(v) && v >= 0;
function need(ok, message) {
  if (!ok) throw Error(message);
}
function fresh(value, now, notBefore = now - 120000) {
  const time = Date.parse(value);
  need(
    Number.isFinite(time) &&
      time >= notBefore &&
      time <= now + 5000 &&
      now - time < 120000,
    "Claude observation stale or predates boundary",
  );
  return time;
}
function inputs(value, now) {
  need(
    value?.version === 1 &&
      uuid(value.lifecycleRunId) &&
      uuid(value.effortRunId) &&
      value.lifecycleRunId !== value.effortRunId,
    "Distinct frozen Claude run IDs required",
  );
  need(
    label(value.account) &&
      label(value.model) &&
      integer(value.window?.startsAt) &&
      integer(value.window?.expiresAt) &&
      value.window.startsAt <= now &&
      now < value.window.expiresAt,
    "Actual Claude selection and execution window required",
  );
  need(
    a.RUN_SOURCE === PINS.hub &&
      a.ROUTER === PINS.router &&
      a.HOLDER_CONTROLS_QUALIFIED === true,
    "Qualified source bridge changed",
  );
  return Object.freeze({
    version: 1,
    lifecycleRunId: value.lifecycleRunId,
    effortRunId: value.effortRunId,
    account: value.account,
    model: value.model,
    window: Object.freeze({
      startsAt: value.window.startsAt,
      expiresAt: value.window.expiresAt,
    }),
  });
}

function readiness(v, expected, now, notBefore) {
  fresh(v?.observedUtc, now, notBefore);
  need(
    v?.version === 1 &&
      v.hubSource === PINS.hub &&
      v.routerSource === PINS.router &&
      v.holderSource === PINS.holder &&
      v.holderBinarySha256 === PINS.binary &&
      v.piManifestSha256 === PINS.piManifest &&
      v.profilesSha256 === PINS.profiles,
    "Claude runtime pins differ",
  );
  need(
    uuid(v.holderId) &&
      uuid(v.journalId) &&
      v.nativeRecovery === 2 &&
      v.nativeStateReady === true &&
      v.activeSessions === 0 &&
      v.pendingCommands === 0 &&
      v.admissionPaused === false,
    "Claude living holder is not ready and idle",
  );
  const r = v.route;
  need(
    r?.alias === "canary-pi-claude" &&
      r.account === expected.account &&
      r.model === expected.model &&
      r.protocol === PROFILE.protocol &&
      r.maxAttempts === 1 &&
      Array.isArray(r.fallbacks) &&
      r.fallbacks.length === 0 &&
      r.transport === "sdk-resume" &&
      r.cliVersion === "2.1.283" &&
      r.warmSessions === false &&
      r.trustVerified === true &&
      integer(r.configRevision),
    "Claude route/cold configuration differs",
  );
  need(
    hash(v.cold?.reportSha256) &&
      v.cold.account === expected.account &&
      v.cold.source === PINS.router &&
      v.cold.passed === true &&
      hash(v.projectionSha256),
    "Selected account cold evidence/provenance missing; do not rerun B0 implicitly",
  );
  return {
    version: 1,
    observedUtc: v.observedUtc,
    holderId: v.holderId,
    journalId: v.journalId,
    route: {
      alias: r.alias,
      account: r.account,
      model: r.model,
      protocol: r.protocol,
      configRevision: r.configRevision,
      maxAttempts: 1,
      fallbacks: [],
      transport: "sdk-resume",
      cliVersion: "2.1.283",
      warmSessions: false,
    },
    cold: {
      reportSha256: v.cold.reportSha256,
      account: v.cold.account,
      source: v.cold.source,
    },
    projectionSha256: v.projectionSha256,
  };
}

// Actual normal metadata shape, distinct from A's mandatory two-binding rotation.
function credential(inventory, detail, now) {
  need(
    inventory &&
      ["bindings", "credentials", "consumers"].every((k) =>
        Array.isArray(inventory[k]),
      ) &&
      inventory.bindings.length < 200 &&
      inventory.credentials.length < 500 &&
      inventory.consumers.length < 1000,
    "Claude credential inventory incomplete",
  );
  need(
    inventory.consumers.every(
      (c) =>
        c &&
        uuid(c.binding_id) &&
        ["active", "released", "blocked"].includes(c.state),
    ),
    "Malformed consumer inventory",
  );
  const bindings = inventory.bindings.filter((b) => b.id === PROFILE.binding);
  need(bindings.length === 1, "Claude binding missing or ambiguous");
  const b = bindings[0];
  need(
    b.enabled === true &&
      b.canRenew === false &&
      ["ready", "installing"].includes(b.state) &&
      b.connection_id === CONNECTION &&
      b.host_id === HOST &&
      b.execution_identity === "c4" &&
      a.canonical(b.grant_spec) ===
        a.canonical({
          routes: ["canary-pi-claude"],
          access: ["subscription"],
          lifetimeSeconds: 86400,
        }) &&
      integer(b.revision) &&
      b.revision >= 1 &&
      integer(b.generation) &&
      b.generation >= 1 &&
      b.generation < 128,
    "Claude binding scope differs",
  );
  const rows = inventory.credentials
    .filter((c) => c.binding_id === b.id)
    .sort((x, y) => x.generation - y.generation);
  need(
    rows.length === b.generation &&
      new Set(rows.map((c) => c.id)).size === rows.length &&
      new Set(rows.map((c) => c.router_key_id)).size === rows.length &&
      rows.every(
        (c, i) =>
          c.generation === i + 1 &&
          uuid(c.id) &&
          /^key_[a-f0-9]{12,32}$/.test(c.router_key_id),
      ),
    "Claude credential history incomplete",
  );
  need(
    rows
      .slice(0, -1)
      .every(
        (c) =>
          c.state === "revoked" &&
          c.revoke_requested === true &&
          c.cleanup_pending === false &&
          c.failure_code === null,
      ) &&
      !inventory.consumers.some(
        (c) => c.binding_id === b.id && c.state === "active",
      ),
    "Claude prior work remains active",
  );
  const c = rows.at(-1),
    expiry = Date.parse(c.expires_at);
  need(
    c.namespace === "default" &&
      c.remote_acknowledged === true &&
      c.revoke_requested === false &&
      c.cleanup_pending === false &&
      c.failure_code === null &&
      c.retire_after === null &&
      expiry > now &&
      expiry <= now + 86405000 &&
      ((b.state === "ready" &&
        c.state === "ready" &&
        b.current_credential_id === c.id &&
        b.pending_credential_id === null) ||
        (b.state === "installing" &&
          c.state === "installing" &&
          b.current_credential_id === null &&
          b.pending_credential_id === c.id)),
    "Claude credential not fresh/acknowledged",
  );
  const d = detail?.credential,
    db = detail?.binding;
  need(
    d?.id === c.id &&
      d.bindingId === b.id &&
      d.connectionId === CONNECTION &&
      d.generation === c.generation &&
      d.routerKeyId === c.router_key_id &&
      d.namespace === "default" &&
      d.state === c.state &&
      d.remoteAcknowledged === true &&
      d.revokeRequested === false &&
      d.cleanupPending === false &&
      d.failureCode === null &&
      d.retireAfter === null &&
      Date.parse(d.expiresAt) === expiry &&
      db?.id === b.id &&
      db.hostId === HOST &&
      db.executionIdentity === "c4" &&
      db.enabled === true &&
      db.canRenew === false &&
      db.state === b.state &&
      db.revision === b.revision &&
      db.generation === b.generation &&
      db.currentCredentialId === b.current_credential_id &&
      db.pendingCredentialId === b.pending_credential_id &&
      Array.isArray(detail.consumers) &&
      detail.consumers.length < 128 &&
      detail.consumers.every((x) =>
        ["active", "released", "blocked"].includes(x.state),
      ) &&
      !detail.consumers.some((x) => x.state === "active"),
    "Claude credential detail changed",
  );
  return Object.freeze({
    ...PROFILE,
    credential: c.id,
    key: c.router_key_id,
    namespace: "default",
    connection: CONNECTION,
    generation: c.generation,
    revision: b.revision,
    expiresAt: expiry,
  });
}

// Operator-attested, source-bound projection, not an invented HTTP response.
// Native receipt: Pi2604 packages/pi-policy/session.ts. Router: ReplaySessions
// scoped KV record (4e4 packages/plugins/inference-sessions/service.ts).
function branchEvidence(v, context, now, previous) {
  fresh(v?.observedUtc, now, context.notBefore);
  need(
    v?.version === 1 &&
      v.hubSource === PINS.hub &&
      v.routerSource === PINS.router &&
      v.piSource === "2604c41b69c5d9c50a3324545a51f0bfa38435a0" &&
      hash(v.provenance?.projectionSha256) &&
      hash(v.provenance?.querySha256) &&
      label(v.provenance?.databaseIdentity),
    "Branch source/provenance missing",
  );
  for (const key of [
    "sessionId",
    "generation",
    "nativeSessionId",
    "holderId",
    "journalId",
    "principal",
    "namespace",
    "tenantId",
    "bindingId",
    "profileId",
  ])
    need(v[key] === context[key], "Branch scope mismatch");
  const n = v.native,
    r = v.router;
  need(
    n &&
      [n.conversation, n.branch, n.nativeSession].every(label) &&
      n.nativeSession === context.nativeSessionId &&
      [n.parent, n.pending, n.reconciledFrom].every(
        (x) => x === undefined || label(x),
      ),
    "Native coordination receipt invalid",
  );
  need(
    r &&
      label(r.branch) &&
      label(r.epoch) &&
      label(r.operation) &&
      ["complete", "uncertain"].includes(r.state) &&
      integer(r.updatedAt) &&
      r.updatedAt <= Date.parse(v.observedUtc) + 5000 &&
      (r.successor === undefined || label(r.successor)),
    "Router branch is active or unknown",
  );
  const key = createHash("sha256")
    .update(
      JSON.stringify([
        context.namespace,
        context.principal,
        n.conversation,
        r.branch,
      ]),
    )
    .digest("hex");
  need(
    v.routerStoreNamespace === "inference-session-v1" &&
      v.routerStoreKey === key &&
      hash(v.provenance.nativeHistorySha256),
    "Branch store/history provenance differs",
  );
  if (previous) {
    need(
      r.branch === previous.native.branch &&
        r.epoch === previous.router.epoch &&
        r.operation === previous.router.operation &&
        r.state === previous.router.state &&
        r.updatedAt >= previous.router.updatedAt &&
        r.successor === n.branch &&
        n.branch !== previous.native.branch &&
        n.reconciledFrom === previous.native.branch &&
        n.conversation === previous.native.conversation &&
        n.parent === undefined &&
        n.pending === undefined,
      "Reconstruction receipt/branch differs",
    );
  } else {
    need(
      r.branch === n.branch &&
        r.successor === undefined &&
        (n.pending
          ? r.operation === n.pending ||
            (r.operation === n.parent && r.state === "complete")
          : r.operation === n.parent && r.state === "complete"),
      "Native and router branch cannot safely continue/reconcile",
    );
  }
  return {
    version: 1,
    observedUtc: v.observedUtc,
    sessionId: v.sessionId,
    generation: v.generation,
    nativeSessionId: v.nativeSessionId,
    holderId: v.holderId,
    journalId: v.journalId,
    tenantId: v.tenantId,
    namespace: v.namespace,
    principal: v.principal,
    bindingId: v.bindingId,
    profileId: v.profileId,
    routerStoreNamespace: v.routerStoreNamespace,
    routerStoreKey: key,
    native: {
      conversation: n.conversation,
      branch: n.branch,
      nativeSession: n.nativeSession,
      ...(n.parent ? { parent: n.parent } : {}),
      ...(n.pending ? { pending: n.pending } : {}),
      ...(n.reconciledFrom ? { reconciledFrom: n.reconciledFrom } : {}),
    },
    router: {
      branch: r.branch,
      epoch: r.epoch,
      operation: r.operation,
      state: r.state,
      updatedAt: r.updatedAt,
      ...(r.successor ? { successor: r.successor } : {}),
    },
    provenance: {
      projectionSha256: v.provenance.projectionSha256,
      querySha256: v.provenance.querySha256,
      databaseIdentity: v.provenance.databaseIdentity,
      nativeHistorySha256: v.provenance.nativeHistorySha256,
    },
    requiresReconcile: !previous && n.pending !== undefined,
  };
}

class ClaudeRunner extends a.Runner {
  constructor(env, value) {
    super(env);
    this.inputs = inputs(value, this.now());
    this.effectiveExpiry = this.inputs.window.expiresAt;
    this.state.kind = "claude-phase-b";
    this.state.inputs = this.inputs;
    this.state.b0 = "preserved; not executed by this entry";
    this.state.accounting =
      "observed router attempts with conservative reservations; not a hard upstream dispatch fence";
  }
  phaseText(name) {
    if (name === "routeHandoff")
      return `Claude route alias canary-pi-claude, selected account ${this.inputs.account}, model ${this.inputs.model}.`;
    need(Object.hasOwn(TEXT, name), "Unknown Claude prompt");
    return TEXT[name];
  }
  effortRunId() {
    return this.inputs.effortRunId;
  }
  credentialScopeReviewed() {
    return Boolean(this.state.claudeCredential);
  }
  start() {
    return this.runExclusive(() => this.startPhaseB());
  }
  replaceStopped() {
    throw Error("Claude Phase B cannot replace or reset Phase A");
  }
  revokeFinal() {
    throw Error("Claude grant cleanup remains separate from Phase B/C");
  }
  async currentCredential() {
    const inventory = await this.api("/api/router-credentials");
    const rows = inventory?.credentials?.filter(
      (c) => c.binding_id === PROFILE.binding,
    );
    need(
      Array.isArray(rows) && rows.length > 0,
      "Claude credential is not issued",
    );
    const latest = [...rows].sort((x, y) => x.generation - y.generation).at(-1);
    need(uuid(latest.id), "Claude credential identity invalid");
    return credential(
      inventory,
      await this.api("/api/router-credentials/keys/" + latest.id),
      this.now(),
    );
  }
  async refreshBeforeLaunch() {
    this.check();
    const current = await this.currentCredential();
    this.check();
    need(
      a.canonical(current) === a.canonical(this.state.claudeCredential),
      "Claude credential changed since readiness",
    );
  }
  async inspectBranch(previous) {
    const boundary = this.collectionBoundary("Claude branch observation");
    const context = {
      kind: "claude-branch",
      sessionId: this.session.id,
      generation: this.session.generation,
      nativeSessionId: this.launchRow.receipt.result.nativeSessionId,
      holderId: this.holderId,
      journalId: this.journalId,
      principal: "key:" + this.profiles.claude.key,
      namespace: "default",
      tenantId: TENANT,
      bindingId: PROFILE.binding,
      profileId: PROFILE.id,
      notBefore: boundary.notBefore,
      ...(previous ? { retiredBranch: previous.native.branch } : {}),
    };
    const observed = await super.ask(
      "Observe exact Claude branch receipts",
      "Read only the scoped private native coordination receipt and router KV metadata. No credentials, model text or tools. Missing/ambiguous evidence stops.",
      (v) => branchEvidence(v, context, this.now(), previous),
      undefined,
      context,
    );
    this.record("claude-branch-observation", observed);
    return observed;
  }
  async ask(title, instructions, validate, timeout, context) {
    if (
      ["recover-history", "reconcile-choice", "retire-branch"].includes(
        context?.kind,
      )
    ) {
      const observed = await this.inspectBranch();
      if (context.kind === "recover-history") {
        this.retainedBranch = observed;
        return true;
      }
      if (context.kind === "reconcile-choice") {
        need(
          this.retainedBranch &&
            a.canonical(observed.native) ===
              a.canonical(this.retainedBranch.native),
          "Imported branch receipt changed",
        );
        const retained = this.retainedBranch.router;
        need(
          observed.router.branch === retained.branch &&
            observed.router.epoch === retained.epoch &&
            observed.router.operation === retained.operation &&
            observed.router.state === retained.state &&
            observed.router.updatedAt >= retained.updatedAt,
          "Imported Router branch changed",
        );
        this.beforeReconcile = observed.requiresReconcile ? observed : null;
        return observed.requiresReconcile;
      }
      this.beforeReconcile = observed;
      return true;
    }
    return super.ask(title, instructions, validate, timeout, context);
  }
  async afterReconciliation() {
    need(this.beforeReconcile, "Explicit inspected branch decision missing");
    await this.inspectBranch(this.beforeReconcile);
    this.beforeReconcile = null;
  }
  async prompt(text, options = {}) {
    this.check();
    need(!this.reservation, "Previous Claude attempt reservation unsettled");
    const maximum =
      options.backendRefusal ||
      text === "/relay-history" ||
      text === "/relay-reconcile confirmed" ||
      /^\/relay-branch [a-f0-9]{8}$/.test(text)
        ? 0
        : text === "/relay-compact"
          ? 2
          : options.tool
            ? 3
            : 2;
    const used =
      this.run.lastSnapshot.totalAttempts -
      this.run.firstSnapshot.totalAttempts;
    need(
      used + maximum <= (this.run.effort ? 6 : 20),
      "Claude continuation reservation exceeds remaining budget",
    );
    this.reservation = {
      before: this.run.lastSnapshot.totalAttempts,
      maximum,
      promptNumber: this.run.prompts + 1,
      state: "reserved",
    };
    this.run.reservations ??= [];
    this.run.reservations.push(this.reservation);
    this.save();
    return super.prompt(text, options);
  }
  async evidence(...args) {
    const value = await super.evidence(...args);
    if (this.reservation) {
      const attempts = value.trajectory.totalAttempts - this.reservation.before;
      need(
        attempts >= 0 && attempts <= this.reservation.maximum,
        "Claude observed attempts exceeded reservation; no further inference",
      );
      Object.assign(this.reservation, { state: "settled", attempts });
      this.reservation = null;
      this.save();
    }
    return value;
  }
  async startPhaseB() {
    need(
      this.env.origin === a.ORIGIN &&
        this.env.load() === null &&
        !this.stopped &&
        !this.started,
      "Fresh exclusive Claude report required; no replay/reset",
    );
    this.started = true;
    this.state.status = "running";
    this.save();
    try {
      const boundary = this.collectionBoundary("Claude readiness");
      const ready = await super.ask(
        "Current Claude cold/readiness",
        "Observe current source-pinned readiness for the selected account/window. Preserve completed B0; perform no probe, retarget, approval or grant mutation.",
        (v) => readiness(v, this.inputs, this.now(), boundary.notBefore),
        undefined,
        {
          kind: "claude-readiness",
          notBefore: boundary.notBefore,
          account: this.inputs.account,
          model: this.inputs.model,
          pins: PINS,
          profile: PROFILE,
          window: this.inputs.window,
        },
      );
      this.holderId = ready.holderId;
      this.journalId = ready.journalId;
      this.record("claude-readiness", ready);
      const current = await this.currentCredential();
      this.profiles = Object.freeze({
        claude: Object.freeze({ ...current, run: this.inputs.lifecycleRunId }),
      });
      this.effectiveExpiry = Math.min(
        this.inputs.window.expiresAt,
        current.expiresAt,
      );
      this.state.claudeCredential = current;
      this.save();
      await this.phase("claude");
      await this.phase("claude", true);
      this.state.status = "phase-b-passed";
      this.state.phaseBCompleted = true;
      this.state.completed = false;
      this.record("limits", {
        liveWire:
          "Unobserved; paired published Pi synthetic evidence remains separate",
        remaining: "Phase C, paired performance and final grant cleanup",
      });
    } catch (e) {
      this.stopped = true;
      this.state.status = "stopped";
      this.state.reason = String(e.message).slice(0, 300);
      this.save();
      await this.cleanup();
      throw e;
    } finally {
      this.save();
    }
  }
}
function createClaudePhaseBRunner(env, verifiedInputs) {
  return new ClaudeRunner(env, verifiedInputs);
}
module.exports = {
  createClaudePhaseBRunner,
  PROFILE,
  PINS,
  TEXT,
  inputs,
  readiness,
  credential,
  branchEvidence,
};
