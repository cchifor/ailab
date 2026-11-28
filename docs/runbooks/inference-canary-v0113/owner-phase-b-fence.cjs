"use strict";

// Ordinary managed-key protocol 79316b29. No signing, key issuance or HTTP here.
const { createHash } = require("node:crypto");
const { canonical } = require("./owner-phase-a.js");
const PATH = "/register/relay-keys";
const ROWS = Object.freeze({
  lifecycle: Object.freeze(
    [
      ["ready", 2],
      ["denied", 3],
      ["approved", 3],
      ["branch", 2],
      ["compaction", 2],
      ["compacted", 2],
      ["cancel", 2],
      ["recovered", 2],
    ].map(Object.freeze),
  ),
  effort: Object.freeze(
    [
      ["low", 2],
      ["high", 2],
    ].map(Object.freeze),
  ),
});
const METHODS = ["inspect", "arm", "advance", "close", "readOperation"];
const hash = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const uuid = (v) =>
  typeof v === "string" &&
  /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const id = (v) =>
  typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$/.test(v);
const count = (v) => Number.isSafeInteger(v) && v >= 0;
const sha = (v) => createHash("sha256").update(v).digest("hex");
const digest = (v) => sha(canonical(v));
const binding = (raw) => sha("POST\n" + PATH + "\n" + sha(raw));
function need(ok, message) {
  if (!ok) throw Error("Attempt fence: " + message);
}
function exact(v, required, optional = []) {
  need(
    v &&
      Object.getPrototypeOf(v) === Object.prototype &&
      required.every((k) => Object.hasOwn(v, k)) &&
      Object.keys(v).every((k) => required.includes(k) || optional.includes(k)),
    "unexpected response shape",
  );
}
function freeze(v) {
  if (v && typeof v === "object") {
    Object.values(v).forEach(freeze);
    Object.freeze(v);
  }
  return v;
}
function port(value, executionDeadline, now) {
  need(
    value && METHODS.every((k) => typeof value[k] === "function"),
    "required signed management port missing",
  );
  need(
    count(executionDeadline) && executionDeadline > now,
    "host execution deadline missing or expired",
  );
  return value;
}
function counters(v, createdAt, drained) {
  for (const k of [
    "attempts",
    "openRequests",
    "staleOpenRequests",
    "tokenUsageUnknown",
    "unattributed",
  ])
    need(count(v[k]), "invalid durable counters");
  need(
    v.coveredFrom === createdAt &&
      v.unattributed === 0 &&
      v.staleOpenRequests === 0 &&
      v.tokenUsageUnknown <= v.attempts &&
      (!drained || v.openRequests === 0),
    "coverage/open-attempt proof missing",
  );
}
function drain(value, expected, now) {
  exact(value, [
    "version",
    "observedAt",
    "scope",
    "completedOperationIds",
    "terminalPrompts",
    "pendingPermissions",
    "pendingTools",
    "pendingControls",
    "nativeRequests",
    "clientRequests",
    "provenance",
  ]);
  need(
    value.version === 1 &&
      count(value.observedAt) &&
      value.observedAt >= expected.notBefore &&
      value.observedAt <= now + 5000 &&
      now - value.observedAt < 120000,
    "stale native/client drain",
  );
  need(
    canonical(value.scope) === canonical(expected.scope) &&
      canonical(value.completedOperationIds) ===
        canonical(expected.completedOperationIds) &&
      canonical(value.terminalPrompts) === canonical(expected.terminalPrompts),
    "native/client terminal identity differs",
  );
  for (const k of [
    "pendingPermissions",
    "pendingTools",
    "pendingControls",
    "nativeRequests",
    "clientRequests",
  ])
    need(
      Array.isArray(value[k]) && value[k].length === 0,
      "native/client work remains or is unknown",
    );
  exact(value.provenance, ["nativeSha256", "clientSha256", "operationsSha256"]);
  need(
    Object.values(value.provenance).every(hash),
    "native/client drain provenance missing",
  );
  return structuredClone(value);
}

class Controller {
  constructor({
    transport,
    source,
    now,
    randomUUID,
    save,
    state,
    check,
    deadline,
    scope,
    selection,
  }) {
    port(transport, deadline, now());
    need(
      typeof source === "string" && /^[a-f0-9]{40}$/.test(source),
      "qualified Router source missing",
    );
    need(
      /^key_[a-f0-9]{12,32}$/.test(scope.keyId) &&
        scope.principalId === "key:" + scope.keyId &&
        scope.namespace === "default" &&
        uuid(scope.tenantId) &&
        uuid(scope.holderId) &&
        uuid(scope.journalId),
      "invalid fixed scope",
    );
    exact(selection, [
      "route",
      "accountId",
      "deploymentId",
      "model",
      "provider",
      "providerGeneration",
    ]);
    need(
      selection.route === "canary-pi-claude" &&
        selection.provider === "claude" &&
        [selection.accountId, selection.deploymentId].every(id) &&
        typeof selection.model === "string" &&
        selection.model.length > 0 &&
        selection.model.length <= 256 &&
        count(selection.providerGeneration),
      "actual selected registration required",
    );
    Object.assign(this, {
      transport,
      source,
      now,
      randomUUID,
      save,
      state,
      check,
      deadline,
    });
    this.scope = freeze(structuredClone(scope));
    this.selection = freeze(structuredClone(selection));
    need(
      state.operations.length === 0 && state.phases.length === 0,
      "fence controller cannot replay saved state",
    );
  }
  request(action, fields) {
    return {
      version: 1,
      action,
      operationId: this.randomUUID(),
      keyId: this.scope.keyId,
      ...fields,
    };
  }
  envelope(value, notBefore) {
    // The port is trusted operator transport; hashes bind bytes, not DB authority.
    need(
      Buffer.byteLength(JSON.stringify(value)) <= 262144,
      "response too large",
    );
    exact(value, [
      "routerSource",
      "observedAt",
      "projectionSha256",
      "response",
    ]);
    need(
      value.routerSource === this.source &&
        count(value.observedAt) &&
        value.observedAt >= notBefore &&
        value.observedAt <= this.now() + 5000 &&
        this.now() - value.observedAt < 120000 &&
        hash(value.projectionSha256) &&
        value.projectionSha256 === digest(value.response),
      "source/freshness/projection differs",
    );
    return structuredClone(value.response);
  }
  async call(method, body, cleanup = false, limit = Infinity) {
    if (!cleanup) this.check();
    const now = this.now(),
      deadline = Math.min(
        limit,
        cleanup
          ? now + 15000
          : Math.min(
              this.deadline,
              now + 15000,
              this.current && this.current.closedAt === undefined
                ? this.current.plan.originalDeadline
                : Infinity,
            ),
      );
    need(deadline > now, "dispatch deadline expired");
    const raw = JSON.stringify(body);
    const request = Object.freeze({
      raw,
      bodySha256: sha(raw),
      requestDigest: binding(raw),
      notBefore: now,
      deadline,
      signal: AbortSignal.timeout(Math.max(1, deadline - now)),
      cleanup,
    });
    const value = await this.transport[method](request);
    need(this.now() < deadline, "port returned after deadline");
    if (!cleanup) this.check();
    return this.envelope(value, now);
  }
  validateInspect(value, body, drained) {
    exact(value, [
      "version",
      "action",
      "operationId",
      "observedAt",
      "deploymentId",
      "keyId",
      "keyCreatedAt",
      "marker",
      "fence",
      "attempts",
      "coveredFrom",
      "openRequests",
      "staleOpenRequests",
      "tokenUsageUnknown",
      "unattributed",
    ]);
    need(
      value.version === 1 &&
        value.action === body.action &&
        value.operationId === body.operationId &&
        value.keyId === body.keyId &&
        uuid(value.deploymentId) &&
        count(value.keyCreatedAt) &&
        value.keyCreatedAt > 0 &&
        count(value.observedAt) &&
        this.now() - value.observedAt < 120000 &&
        value.observedAt <= this.now() + 5000,
      "inspect identity differs",
    );
    if (this.initial)
      need(
        value.deploymentId === this.initial.deploymentId &&
          value.keyCreatedAt === this.initial.keyCreatedAt,
        "key/deployment replaced",
      );
    counters(value, value.keyCreatedAt, drained);
    if (this.current) {
      need(
        canonical(value.fence) === canonical(this.current) &&
          canonical(value.marker) ===
            canonical({
              fenceId: this.current.plan.fenceId,
              revision: this.current.revision,
            }),
        "fence changed outside this controller",
      );
      need(
        value.attempts >= this.last.attempts &&
          value.tokenUsageUnknown >= this.last.tokenUsageUnknown,
        "durable counters regressed",
      );
      need(
        value.attempts - this.current.baselineAttempts <=
          this.current.plan.phaseLimit &&
          value.attempts - this.current.rowStartAttempts <=
            this.current.plan.rows[this.current.activeRow].limit,
        "admitted row/phase ceiling exceeded",
      );
      if (this.current.closedAt !== undefined)
        need(
          value.attempts === this.current.closedAttempts,
          "closed fence count changed",
        );
    } else
      need(
        value.marker === null &&
          value.fence === null &&
          value.tokenUsageUnknown === 0,
        "unexpected predecessor or first-arm uncertainty",
      );
    return value;
  }
  async inspect(cleanup = false) {
    const body = this.request("attempt-fence-inspect", {});
    const reading = this.validateInspect(
      await this.call("inspect", body, cleanup),
      body,
      !cleanup,
    );
    this.last = reading;
    this.state.readings ??= [];
    this.state.readings.push(structuredClone(reading));
    this.save();
    return reading;
  }
  async initialize() {
    need(!this.initial, "already initialized");
    this.initial = await this.inspect();
    this.state.initial = structuredClone(this.initial);
    this.save();
  }
  expectedState(body, receipt) {
    const time = receipt.committedAt;
    if (body.action === "attempt-fence-arm") {
      const {
        version,
        action,
        operationId,
        keyId,
        expectedAttempts,
        expectedUnknownUsage,
        expectedFenceRevision,
        ...plan
      } = body;
      need(id(receipt.fence?.issuer), "issuer missing");
      if (this.current)
        need(receipt.fence.issuer === this.current.issuer, "issuer changed");
      return {
        v: 1,
        keyId,
        issuer: receipt.fence.issuer,
        tenant: this.scope.tenantId,
        plan,
        revision: expectedFenceRevision + 1,
        activeRow: 0,
        baselineAttempts: expectedAttempts,
        rowStartAttempts: expectedAttempts,
        baselineTokenUsageUnknown: expectedUnknownUsage,
        rowStartTokenUsageUnknown: expectedUnknownUsage,
        completed: [],
        createdAt: time,
      };
    }
    const f = structuredClone(this.current);
    need(
      f &&
        f.plan.fenceId === body.fenceId &&
        f.revision === body.expectedFenceRevision &&
        f.closedAt === undefined,
      "receipt has no current predecessor",
    );
    if (body.action === "attempt-fence-advance") {
      const row = f.plan.rows[f.activeRow],
        used = body.expectedAttempts - f.rowStartAttempts;
      need(
        row.id === body.completedRowId &&
          f.plan.rows[f.activeRow + 1]?.id === body.nextRowId &&
          used >= 0 &&
          used <= row.limit,
        "row transition differs",
      );
      f.completed.push({
        rowId: row.id,
        used,
        retiredUnused: row.limit - used,
        terminalEvidenceSha256: body.terminalEvidenceSha256,
      });
      f.activeRow++;
      f.rowStartAttempts = body.expectedAttempts;
      f.rowStartTokenUsageUnknown = body.expectedUnknownUsage;
    } else {
      f.closedAt = time;
      f.closedAttempts = body.expectedAttempts;
      f.closedTokenUsageUnknown = body.expectedUnknownUsage;
      f.terminalEvidenceSha256 = body.terminalEvidenceSha256;
    }
    f.revision++;
    return f;
  }
  accept(receipt, intent, cleanup) {
    exact(receipt, [
      "version",
      "action",
      "operationId",
      "requestDigest",
      "committedAt",
      "fence",
      "attempts",
      "coveredFrom",
      "openRequests",
      "staleOpenRequests",
      "tokenUsageUnknown",
      "unattributed",
    ]);
    const body = JSON.parse(intent.raw);
    need(
      receipt.version === 1 &&
        receipt.action === body.action &&
        receipt.operationId === body.operationId &&
        // The HTTP route stores SHA256(raw). The JWT assertion's requestDigest
        // binds method/path/body separately and is never a receipt body digest.
        receipt.requestDigest === intent.bodySha256 &&
        count(receipt.committedAt) &&
        receipt.committedAt >= intent.notBefore - 5000 &&
        receipt.committedAt <= this.now() + 5000 &&
        receipt.attempts === body.expectedAttempts &&
        receipt.tokenUsageUnknown === body.expectedUnknownUsage,
      "mutation receipt/digest/count differs",
    );
    counters(receipt, this.initial.keyCreatedAt, !cleanup);
    need(
      canonical(receipt.fence) === canonical(this.expectedState(body, receipt)),
      "full immutable fence receipt differs",
    );
    this.current = structuredClone(receipt.fence);
    this.last = { ...this.last, ...receipt };
    intent.status = "committed";
    intent.receipt = structuredClone(receipt);
    this.state.current = structuredClone(this.current);
    this.save();
  }
  async mutate(method, fields, cleanup = false) {
    need(!this.uncertain, "unresolved mutation prevents further fence writes");
    const body = this.request("attempt-fence-" + method, fields),
      raw = JSON.stringify(body);
    const intent = {
      raw,
      bodySha256: sha(raw),
      requestDigest: binding(raw),
      notBefore: this.now(),
      deadline: cleanup
        ? this.now() + 15000
        : Math.min(
            this.deadline,
            this.now() + 15000,
            body.originalDeadline ??
              this.current?.plan.originalDeadline ??
              Infinity,
          ),
      status: "sending",
      cleanup,
    };
    this.state.operations.push(intent);
    this.save(); // Must succeed before the trusted signer sees these bytes.
    let value;
    try {
      value = await this.call(method, body, cleanup, intent.deadline);
    } catch {
      intent.status = "uncertain";
      this.uncertain = true;
      this.save();
      // Read exactly this operation once. Never resend its mutation.
      try {
        const lookup = {
          version: 1,
          action: "operation",
          operationId: this.randomUUID(),
          targetOperationId: body.operationId,
        };
        value = await this.call(
          "readOperation",
          lookup,
          cleanup,
          intent.deadline,
        );
        this.accept(value, intent, cleanup);
        this.uncertain = false;
        return;
      } catch {
        throw Error(
          "Attempt fence: mutation unresolved; retained original operation, no resend",
        );
      }
    }
    try {
      this.accept(value, intent, cleanup);
    } catch (error) {
      intent.status = "invalid-receipt";
      this.uncertain = true;
      this.save();
      throw error;
    }
  }
  async row(run, rowId, terminal) {
    need(
      this.initial && !this.uncertain,
      "initial inspection or resolved operation missing",
    );
    const name = run.effort ? "effort" : "lifecycle",
      rows = ROWS[name].map(([id, limit]) => ({ id, limit }));
    const reading = await this.inspect();
    if (this.phase?.runId !== run.runId) {
      need(
        rowId === rows[0].id &&
          this.state.phases.length === (run.effort ? 1 : 0) &&
          (!this.current || this.current.closedAt !== undefined),
        "phase order/predecessor invalid",
      );
      need(
        this.current || reading.attempts === this.initial.attempts,
        "unobserved admissions before initial arm",
      );
      const deadline = Math.min(this.deadline, run.deadline);
      need(deadline > this.now(), "phase deadline expired");
      const plan = {
        keyCreatedAt: this.initial.keyCreatedAt,
        fenceId: this.randomUUID(),
        runId: run.runId,
        phaseId: run.runId,
        scopeDigest: digest(this.scope),
        namespace: this.scope.namespace,
        principalId: this.scope.principalId,
        deploymentId: this.initial.deploymentId,
        originalDeadline: deadline,
        phaseLimit: run.effort ? 6 : 20,
        rows,
        selection: this.selection,
        ...(this.current
          ? { predecessorFenceId: this.current.plan.fenceId }
          : {}),
      };
      plan.planDigest = digest(plan);
      await this.mutate("arm", {
        ...plan,
        expectedAttempts: reading.attempts,
        expectedUnknownUsage: reading.tokenUsageUnknown,
        expectedFenceRevision: this.current?.revision || 0,
      });
      this.phase = {
        runId: run.runId,
        name,
        rawBaseline: run.lastSnapshot.totalAttempts,
        admittedBaseline: reading.attempts,
        rows: [],
      };
      this.state.phases.push(this.phase);
    } else {
      need(
        terminal && this.phase.rows.at(-1)?.settled === true,
        "previous native/client drain missing",
      );
      need(
        this.now() - terminal.observedAt < 120000 &&
          terminal.observedAt <= this.now() + 5000 &&
          reading.attempts ===
            this.phase.rows.at(-1).before + this.phase.rows.at(-1).attempts,
        "attempts changed after terminal drain",
      );
      const f = this.current;
      need(
        f.plan.rows[f.activeRow + 1]?.id === rowId,
        "fixed row order differs",
      );
      await this.mutate("advance", {
        fenceId: f.plan.fenceId,
        expectedFenceRevision: f.revision,
        expectedAttempts: reading.attempts,
        expectedUnknownUsage: reading.tokenUsageUnknown,
        completedRowId: f.plan.rows[f.activeRow].id,
        nextRowId: rowId,
        terminalEvidenceSha256: digest(terminal),
      });
    }
    const index = this.current.activeRow;
    need(this.current.plan.rows[index].id === rowId, "row activation differs");
    this.phase.rows.push({
      id: rowId,
      before: reading.attempts,
      rawBefore: run.lastSnapshot.totalAttempts,
      maximum: Math.min(
        rows[index].limit,
        this.current.plan.phaseLimit -
          (reading.attempts - this.current.baselineAttempts),
      ),
      settled: false,
    });
    this.save();
  }
  async settle(run, rawTotal, terminal) {
    if (!this.current) return;
    const reading = await this.inspect();
    if (run.runId !== this.phase.runId) {
      need(
        this.current.closedAt !== undefined,
        "next phase observed before predecessor closed",
      );
      return;
    }
    need(
      count(rawTotal) &&
        rawTotal - this.phase.rawBaseline ===
          reading.attempts - this.phase.admittedBaseline,
      "raw trajectory/admission mismatch; denied N+1 remains a phase failure",
    );
    const row = this.phase.rows.at(-1);
    need(
      row &&
        rawTotal - row.rawBefore === reading.attempts - row.before &&
        reading.attempts - row.before <= row.maximum,
      "row attempt accounting differs",
    );
    Object.assign(row, {
      settled: true,
      attempts: reading.attempts - row.before,
      tokenUsageUnknown: reading.tokenUsageUnknown,
      terminal: structuredClone(terminal),
    });
    this.save();
  }
  async close(terminal, cleanup = false) {
    if (!this.current || this.current.closedAt !== undefined) {
      need(!this.uncertain, "unresolved arm cannot be represented as closed");
      return;
    }
    need(
      !this.uncertain,
      "unresolved mutation cannot be represented as closed",
    );
    const reading = await this.inspect(cleanup),
      f = this.current;
    if (!cleanup)
      need(
        terminal &&
          this.now() - terminal.observedAt < 120000 &&
          terminal.observedAt <= this.now() + 5000 &&
          this.phase.rows.length === f.plan.rows.length &&
          this.phase.rows.at(-1).settled &&
          reading.attempts ===
            this.phase.rows.at(-1).before + this.phase.rows.at(-1).attempts,
        "phase incomplete or not drained",
      );
    await this.mutate(
      "close",
      {
        fenceId: f.plan.fenceId,
        expectedFenceRevision: f.revision,
        expectedAttempts: reading.attempts,
        expectedUnknownUsage: reading.tokenUsageUnknown,
        terminalEvidenceSha256: digest(terminal),
      },
      cleanup,
    );
    this.phase.closed = true;
    this.phase.closeReceipt = structuredClone(
      this.state.operations.at(-1).receipt,
    );
    this.save();
  }
}
module.exports = { Controller, ROWS, port, drain, digest, binding };
