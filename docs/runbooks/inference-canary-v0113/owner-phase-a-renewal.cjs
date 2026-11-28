"use strict";

// Normal expired-binding renewal only. The caller supplies the reviewed JIT
// transport and synchronous durable journal; this module creates no authority.
const { randomUUID } = require("node:crypto");
const { setTimeout: delay } = require("node:timers/promises");
const driver = require("./owner-phase-a.js");
const { abortable } = require("./owner-phase-a-automation.cjs");
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
const CONNECTION = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8";
const uuid = (v) =>
  typeof v === "string" &&
  /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(v);
const need = (v, message) => {
  if (!v) throw new Error(message);
};
const clone = (v) => JSON.parse(JSON.stringify(v));

function inventoryShape(inventory) {
  need(
    Array.isArray(inventory?.bindings) &&
      inventory.bindings.length < 500 &&
      Array.isArray(inventory.credentials) &&
      inventory.credentials.length < 1000 &&
      Array.isArray(inventory.consumers) &&
      inventory.consumers.length < 1000,
    "Incomplete renewal inventory",
  );
  need(
    inventory.consumers.every(
      (c) =>
        c &&
        uuid(c.binding_id) &&
        ["active", "released", "blocked"].includes(c.state),
    ),
    "Malformed renewal consumers",
  );
}

function bindingScope(inventory, provider) {
  inventoryShape(inventory);
  const p = driver.PROFILES[provider];
  const rows = inventory.bindings.filter((b) => b.id === p.binding);
  need(rows.length === 1, "Ambiguous fixed binding");
  const b = rows[0];
  need(
    b.enabled === true &&
      b.connection_id === CONNECTION &&
      b.host_id === HOST &&
      b.execution_identity === "c4" &&
      driver.canonical(b.grant_spec) ===
        driver.canonical({
          routes: ["canary-pi-" + provider],
          access: [provider === "codex" ? "subscription" : "local"],
          lifetimeSeconds: 86400,
        }) &&
      Number.isSafeInteger(b.revision) &&
      b.revision > 0 &&
      Number.isSafeInteger(b.generation) &&
      b.generation > 0 &&
      b.generation < 127 &&
      !inventory.consumers.some(
        (c) => c.binding_id === b.id && c.state === "active",
      ),
    "Binding scope or active consumer differs",
  );
  return b;
}

function eligible(inventory, provider, now) {
  const b = bindingScope(inventory, provider),
    p = driver.PROFILES[provider];
  need(
    b.canRenew === true &&
      b.state === "failed" &&
      b.current_credential_id === null &&
      b.pending_credential_id === null,
    "Binding is not a clean expired renewal",
  );
  const history = inventory.credentials
    .filter((c) => c.binding_id === b.id)
    .sort((a, z) => a.generation - z.generation);
  need(
    history.length === b.generation &&
      new Set(history.map((c) => c.id)).size === history.length &&
      new Set(history.map((c) => c.router_key_id)).size === history.length &&
      history.every(
        (c, i) =>
          c.generation === i + 1 &&
          uuid(c.id) &&
          /^key_[a-f0-9]{12,32}$/.test(c.router_key_id) &&
          c.state === "revoked" &&
          c.revoke_requested === true &&
          c.cleanup_pending === false &&
          c.failure_code === null &&
          c.retire_after === null &&
          Number.isFinite(Date.parse(c.expires_at)) &&
          Date.parse(c.expires_at) <= now,
      ) &&
      history[0].id === p.credential &&
      history[0].router_key_id === p.key,
    "Renewal history is incomplete or not retired",
  );
  return { bindingId: b.id, revision: b.revision, generation: b.generation };
}

function createRenewalPrelude({
  authenticatedFetch,
  persist,
  observeDeployment,
  deadline,
  signal,
  now = Date.now,
  sleep = (ms, s) => delay(ms, undefined, { signal: s }),
}) {
  need(
    typeof authenticatedFetch === "function" &&
      typeof persist === "function" &&
      typeof observeDeployment === "function" &&
      Number.isSafeInteger(deadline) &&
      deadline > now(),
    "Renewal transport, durable journal and deadline required",
  );
  // This is bootstrap, not an inference window. It cannot consume an unbounded
  // share of the outer run, or extend that deadline when issuance is slow.
  const end = Math.min(deadline, now() + 120000);
  const state = {
    version: 1,
    kind: "phase-a-expired-renewal",
    status: "prepared",
    operations: [],
  };
  let used = false;
  const check = () => {
    signal?.throwIfAborted();
    need(now() < end, "Renewal deadline expired");
  };
  const save = () => {
    const result = persist(clone(state));
    need(
      !result || typeof result.then !== "function",
      "Renewal journal must persist synchronously",
    );
  };
  const api = async (path, method = "GET", body) => {
    check();
    const bounded = AbortSignal.any([
      AbortSignal.timeout(Math.max(1, Math.min(30000, end - now()))),
      ...(signal ? [signal] : []),
    ]);
    const request = Promise.resolve().then(async () => {
      bounded.throwIfAborted();
      const response = await authenticatedFetch(path, {
        method,
        headers: { "Content-Type": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: bounded,
      });
      const text = await response.text();
      bounded.throwIfAborted();
      need(
        response.ok && (method !== "POST" || response.status === 202),
        "Renewal HTTP request refused",
      );
      need(
        Buffer.byteLength(text) <= 2 * 1024 * 1024,
        "Renewal response exceeds bound",
      );
      return JSON.parse(text);
    });
    const result = await abortable(request, bounded);
    check();
    return result;
  };
  const inspectIssued = async (inventory, provider, operation) => {
    const b = bindingScope(inventory, provider);
    need(
      b.generation === operation.generation + 1 &&
        b.revision >= operation.revision + 1,
      "Renewal generation changed or is unobserved",
    );
    const rows = inventory.credentials.filter(
      (c) => c.binding_id === b.id && c.generation === b.generation,
    );
    need(
      rows.length === 1 &&
        rows[0].creation_operation_id === operation.operationId &&
        uuid(rows[0].id),
      "Issuance is not bound to the saved operation",
    );
    const c = rows[0];
    need(
      !["failed", "unknown", "revoked", "revocation_pending"].includes(
        c.state,
      ) &&
        c.failure_code === null &&
        c.revoke_requested === false &&
        c.cleanup_pending === false,
      "Issuance failed or is uncertain",
    );
    if (!["ready", "installing"].includes(c.state)) {
      need(c.state === "requested", "Unrecognized issuance state");
      return null;
    }
    need(
      c.remote_acknowledged === true,
      "Issued credential is not acknowledged",
    );
    const detail = await api("/api/router-credentials/keys/" + c.id);
    const operations = detail.operations;
    need(
      Array.isArray(operations) && operations.length < 32,
      "Issuance operation evidence truncated",
    );
    const matches = operations.filter((o) => o.id === operation.operationId);
    need(
      matches.length === 1 &&
        matches[0].kind === "rotate" &&
        matches[0].state === c.state &&
        matches[0].failureCode === null &&
        detail.credential?.creationOperationId === operation.operationId &&
        detail.credential.id === c.id &&
        detail.credential.bindingId === b.id &&
        detail.credential.state === c.state &&
        detail.credential.remoteAcknowledged === true,
      "Issuance detail does not confirm the saved rotation",
    );
    return detail;
  };
  return {
    async run() {
      need(!used, "Renewal is single-use; inspect retained operations");
      used = true;
      check();
      state.status = "reviewing";
      save();
      try {
        const startedAt = now();
        const observationSignal = AbortSignal.any([
          AbortSignal.timeout(Math.max(1, end - now())),
          ...(signal ? [signal] : []),
        ]);
        const deployment = await abortable(
          Promise.resolve().then(() => {
            observationSignal.throwIfAborted();
            return observeDeployment({
              deadline: end,
              signal: observationSignal,
            });
          }),
          observationSignal,
        );
        check();
        need(
          deployment?.hubSource === driver.RUN_SOURCE &&
            deployment.ready === true &&
            Date.parse(deployment.observedUtc) >= startedAt &&
            Date.parse(deployment.observedUtc) <= now() + 5000,
          "Fresh source-qualified deployment required before renewal",
        );
        state.deployment = {
          hubSource: deployment.hubSource,
          ready: true,
          observedUtc: deployment.observedUtc,
        };
        save();
        let inventory = await api("/api/router-credentials");
        const expected = Object.fromEntries(
          ["codex", "qwen"].map((p) => [p, eligible(inventory, p, now())]),
        );
        // Both fixed bindings must be eligible before the first mutation.
        for (const provider of ["codex", "qwen"]) {
          inventory = await api("/api/router-credentials");
          need(
            driver.canonical(eligible(inventory, provider, now())) ===
              driver.canonical(expected[provider]),
            "Binding changed before rotation",
          );
          const operation = {
            provider,
            ...expected[provider],
            operationId: randomUUID(),
            status: "sending",
          };
          state.operations.push(operation);
          state.status = "rotating";
          save(); // Intent is durable before the single POST, even if its response is lost.
          try {
            await api(
              "/api/router-credentials/" + operation.bindingId + "/rotate",
              "POST",
              {
                operationId: operation.operationId,
                revision: operation.revision,
              },
            );
          } catch (error) {
            operation.status = "uncertain";
            throw error; // No redispatch, next rotation or permission on this path.
          }
          operation.status = "accepted";
          save();
          while (true) {
            inventory = await api("/api/router-credentials");
            const detail = await inspectIssued(inventory, provider, operation);
            if (detail) {
              operation.status = "issued";
              operation.credentialId = detail.credential.id;
              operation.credentialState = detail.credential.state;
              save();
              break;
            }
            check();
            await sleep(Math.min(2000, end - now()), signal);
          }
        }
        inventory = await api("/api/router-credentials");
        const details = {};
        for (const operation of state.operations) {
          details[operation.provider] = await inspectIssued(
            inventory,
            operation.provider,
            operation,
          );
          need(
            details[operation.provider],
            "Issuance regressed before handoff",
          );
        }
        const result = driver.renewedCredentials(inventory, details, now());
        state.status = "issued";
        state.result = result;
        state.observedUtc = new Date(now()).toISOString();
        save();
        return clone(state);
      } catch {
        state.status = "stopped";
        save();
        throw new Error(
          "Renewal stopped; preserve saved operations, do not repeat or assume issuance",
        );
      }
    },
  };
}

module.exports = { createRenewalPrelude, eligible };
