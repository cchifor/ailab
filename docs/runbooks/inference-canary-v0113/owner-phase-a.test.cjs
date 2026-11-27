"use strict";
// No real fetch, timers, browser state, credentials, provider or host operations.
const test = require("node:test");
const assert = require("node:assert/strict");
const { createHash, webcrypto } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const a = require("./owner-phase-a.js");
const driverSource = fs.readFileSync(
  path.join(__dirname, "owner-phase-a.js"),
  "utf8",
);
// Positive entry, metadata, lifecycle and handoff cases use the actual module.
// Only negative hold cases evaluate an explicitly disabled module variant.
// Published v0.13.4 pins bind the separately qualified holder artifacts.
// These actual-module tests run offline; they do not prove live readiness.
const PUBLISHED_HOLDER_TEST_PINS = Object.freeze({
  release: "v0.13.4",
  source: "64dc50f47a4867a6d9a7afc2e7d5ae24068c59af",
  binarySha256:
    "d406b1f363f590b386ab3eaea4a8cb350c5ac9c49a3dcb26acc34034dfcf6926",
});
assert.equal(a.RUN_SOURCE, "5f90d03a6ed291e6c62dfde75938ba07c20d8902");
assert.equal(a.HOLDER_CONTROLS_QUALIFIED, true);
assert.match(PUBLISHED_HOLDER_TEST_PINS.binarySha256, /^[a-f0-9]{64}$/);
const holderDisabledModule = { exports: {} };
assert.match(driverSource, /const HOLDER_CONTROLS_QUALIFIED = (?:true|false);/);
vm.runInThisContext(
  "(function(module){" +
    driverSource.replace(
      /const HOLDER_CONTROLS_QUALIFIED = (?:true|false);/,
      "const HOLDER_CONTROLS_QUALIFIED = false;",
    ) +
    "\n})",
  { filename: "disabled-holder-entrypoint.js", timeout: 1000 },
)(holderDisabledModule);
const holderDisabled = holderDisabledModule.exports;
const heldModule = { exports: {} };
vm.runInThisContext(
  "(function(module){" +
    driverSource.replace(
      /const QUALIFIED_INPUTS = (?:true|false);/,
      "const QUALIFIED_INPUTS = false;",
    ) +
    "\n})",
  { filename: "offline-held-owner-phase-a.js", timeout: 1000 },
)(heldModule);
const held = heldModule.exports;
const NOW = Date.parse("2026-11-26T12:00:00Z");
const SID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const LEASE = "33333333-3333-4333-8333-333333333333";
const OP = "44444444-4444-4444-8444-444444444444";
const REQUEST = "55555555-5555-4555-8555-555555555555";
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
const clone = (v) => JSON.parse(JSON.stringify(v));
const date = (n) => new Date(n).toISOString();
const p = a.PROFILES.codex;
const POST_EXPIRY = a.EXPIRES + 60000;
const command = a.markerCommand("codex", p.run, "effects");
const holderInputs = JSON.parse(
  fs.readFileSync(path.join(__dirname, "holder-inputs.json"), "utf8"),
);
function harness(
  handler = () => {
    throw Error("Unexpected fetch");
  },
) {
  let clock = NOW;
  const calls = [],
    saves = [];
  const runner = new a.Runner({
    origin: a.ORIGIN,
    crypto: webcrypto,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    load: () => null,
    save: (s) => saves.push(clone(s)),
    ask: async (_title, _text, validate) => validate(true),
    fetch: async (path, init) => {
      const call = {
        path,
        method: init.method,
        body: init.body && JSON.parse(init.body),
        init,
      };
      calls.push(call);
      const result = await handler(call, runner, saves);
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        text: async () => JSON.stringify(result),
      };
    },
  });
  return {
    runner,
    calls,
    saves,
    advance: (ms) => {
      clock += ms;
    },
  };
}
function session(extra = {}) {
  return {
    id: SID,
    kind: "holder",
    connector_id: HOST,
    parent_session_id: null,
    generation: 1,
    rebinding: false,
    state: "open",
    control_epoch: 0,
    ...extra,
  };
}
function detail(extra = {}) {
  return {
    id: REQUEST,
    session_id: SID,
    generation: 1,
    rebinding: false,
    state: "pending",
    expires_at: date(NOW + 30000),
    context: {
      title: "Allow bash?",
      description: JSON.stringify({ command, timeout: 30 }),
      toolCallId: "pi-ui-fixed",
      options: [
        { optionId: "yes", kind: "allow_once", name: "Allow once" },
        { optionId: "no", kind: "reject_once", name: "Reject once" },
      ],
    },
    ...extra,
  };
}
function marker(phase = "baseline") {
  return {
    version: 1,
    provider: "codex",
    profileId: p.id,
    runId: p.run,
    phase,
    state: "observed",
    phaseChecksPass: true,
    observedUtc: date(NOW),
    commands: {
      denied: a.markerCommand("codex", p.run, "denied"),
      effects: command,
    },
    files: {
      denied: { state: "absent" },
      effects:
        phase === "baseline"
          ? { state: "absent" }
          : {
              state: "present",
              bytes: 88,
              exactlyOneMarker: true,
              markerOccurrences: 1,
              sha256: "a".repeat(64),
            },
    },
  };
}
function trajectory(time = NOW, rows = []) {
  return {
    version: 1,
    source: a.ROUTER,
    origin: "http://127.0.0.1:8787",
    scope: {
      namespace: "default",
      principal: "key:" + p.key,
      route: "canary-pi-codex",
      since: NOW - 1000,
    },
    requestedAt: time,
    observedAt: time + 1,
    rows,
    fetchedRows: rows.length,
    totalAttempts: rows.reduce((n, r) => n + r.attempts, 0),
  };
}
function trajectoryRow(attempts = 1) {
  return {
    id: "trajectory-1",
    requestId: "request-1",
    namespace: "default",
    principalId: "key:" + p.key,
    route: "canary-pi-codex",
    startedAt: NOW + 2,
    endedAt: NOW + 3,
    incomplete: false,
    status: "complete",
    attempts,
    configRevision: 2803,
  };
}

// Synthetic evidence of the one eligible stopped run; never reads an owner report.
function stoppedReport() {
  const first = { ...trajectory(NOW - 100), source: a.STOPPED_SOURCE };
  const path = "/api/agent-sessions/" + a.STOPPED_SESSION;
  const launch = {
    kind: "launch",
    at: date(NOW - 80),
    id: REQUEST,
    operationId: OP,
    sessionId: a.STOPPED_SESSION,
    profileId: p.id,
    revision: 1,
    journalId: a.HOLDER,
    holderId: OTHER,
    recovery: false,
  };
  return {
    version: 1,
    relay: a.SOURCE,
    router: a.STOPPED_SOURCE,
    status: "stopped",
    reason: "Unexpected tool permission",
    completed: false,
    runs: [
      {
        provider: "codex",
        runId: p.run,
        effort: false,
        prompts: 1,
        deadline: NOW - 1,
        firstSnapshot: first,
        lastSnapshot: clone(first),
      },
    ],
    evidence: [
      launch,
      { kind: "pre-launch", marker: marker() },
      { kind: "cleanup", status: "closed-and-released", at: date(NOW) },
    ],
    operations: [
      {
        status: "received",
        path: "/api/agent-launches",
        body: { operationId: OP },
      },
      {
        status: "received",
        kind: "lease-acquisition",
        path: path + "/control",
      },
      {
        status: "received",
        path: "/api/agent-permissions/" + a.STOPPED_PERMISSION + "/decision",
        body: { outcome: { outcome: "cancelled" } },
      },
      ...["prompt", "cancel", "close"].map((kind, i) => ({
        status: "received",
        path: path + "/commands",
        body: {
          kind,
          generation: 1,
          operationId: [SID, OTHER, LEASE][i],
          ...(kind === "prompt"
            ? { text: "Reply exactly READY. Do not use tools or modify files." }
            : {}),
        },
      })),
    ],
  };
}

test("replacement accepts only the known stopped workload, never uncertain or extra work", () => {
  const report = stoppedReport();
  assert.equal(a.stoppedPredecessor(JSON.stringify(report)).run.prompts, 1);
  for (const mutate of [
    (r) => {
      r.router = a.ROUTER;
    },
    (r) => {
      r.reason = "Network error";
    },
    (r) => {
      r.completed = true;
    },
    (r) => {
      r.predecessor = {};
    },
    (r) => {
      r.runs.push(clone(r.runs[0]));
    },
    (r) => {
      r.runs[0].prompts = 2;
    },
    (r) => {
      r.operations[0].status = "unknown";
    },
    (r) => {
      r.operations.push(clone(r.operations[0]));
    },
    (r) => {
      r.operations[3].body.text += " Again.";
    },
    (r) => {
      r.evidence[0].sessionId = SID;
    },
    (r) => {
      r.evidence[2].status = "uncertain";
    },
    (r) => {
      r.evidence.push({ kind: "permission" });
    },
  ]) {
    const changed = clone(report);
    mutate(changed);
    assert.throws(() => a.stoppedPredecessor(JSON.stringify(changed)));
  }
});

test("replacement refuses duplicate non-command categories even with exactly six received operations", () => {
  for (const category of [0, 1, 2]) {
    const report = stoppedReport();
    report.operations[(category + 1) % 3] = clone(report.operations[category]);
    assert.equal(report.operations.length, 6);
    assert(report.operations.every((o) => o.status === "received"));
    assert.throws(() => a.stoppedPredecessor(JSON.stringify(report)));
  }
});

function renewedFixture(state = "installing", now = POST_EXPIRY) {
  const inventory = { bindings: [], credentials: [], consumers: [] },
    details = {};
  for (const [i, provider] of ["codex", "qwen"].entries()) {
    const prior = a.PROFILES[provider],
      id = [SID, OTHER][i],
      key = "key_" + ["a", "b"][i].repeat(32);
    const expiry = now + (i ? 3600000 : 7200000);
    const binding = {
      id: prior.binding,
      connection_id: "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8",
      host_id: HOST,
      execution_identity: "c4",
      enabled: true,
      state,
      revision: 2,
      generation: 2,
      canRenew: false,
      grant_spec: {
        routes: ["canary-pi-" + provider],
        access: [i ? "local" : "subscription"],
        lifetimeSeconds: 86400,
      },
      current_credential_id: state === "ready" ? id : null,
      pending_credential_id: state === "installing" ? id : null,
    };
    const credential = {
      id,
      binding_id: prior.binding,
      generation: 2,
      router_key_id: key,
      namespace: "default",
      state,
      remote_acknowledged: true,
      revoke_requested: false,
      cleanup_pending: false,
      failure_code: null,
      retire_after: null,
      expires_at: date(expiry),
    };
    inventory.bindings.push(binding);
    inventory.credentials.push(
      {
        id: prior.credential,
        binding_id: prior.binding,
        generation: 1,
        router_key_id: prior.key,
        state: "revoked",
        revoke_requested: true,
        cleanup_pending: false,
        failure_code: null,
      },
      credential,
    );
    details[provider] = {
      credential: {
        id,
        bindingId: prior.binding,
        connectionId: binding.connection_id,
        generation: 2,
        routerKeyId: key,
        namespace: "default",
        state,
        remoteAcknowledged: true,
        revokeRequested: false,
        cleanupPending: false,
        failureCode: null,
        retireAfter: null,
        expiresAt: date(expiry),
      },
      binding: {
        id: prior.binding,
        hostId: HOST,
        executionIdentity: "c4",
        enabled: true,
        state,
        canRenew: false,
        revision: 2,
        generation: 2,
        currentCredentialId: binding.current_credential_id,
        pendingCredentialId: binding.pending_credential_id,
      },
      consumers: [],
      operations: [],
    };
  }
  return { inventory, details };
}

// Captured normal HTTP responses from the 74d1 hub with a private PostgreSQL
// fixture. The generation recipe and source provenance live beside the payloads;
// unlike renewedFixture(), these response shapes were not authored by the driver.
const hubProjectionSource = fs.readFileSync(
  path.join(__dirname, "owner-phase-a.hub-74d1.fixture.json"),
  "utf8",
);
const hubProjection = JSON.parse(hubProjectionSource);
const hubSourceBridge = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "owner-phase-a.hub-5f90.source-bridge.json"),
    "utf8",
  ),
);
const hubObservation = Date.parse(hubProjection.observedAt);

test("renewed credentials accept the exact 74d1 HTTP inventory and detail projections", () => {
  assert.equal(hubProjection.version, 1);
  assert.equal(
    hubProjection.sourceCommit,
    "74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613",
  );
  assert.equal(hubSourceBridge.version, 1);
  assert.equal(hubSourceBridge.capturedSource, hubProjection.sourceCommit);
  assert.equal(hubSourceBridge.targetSource, a.RUN_SOURCE);
  // Git may convert LF to CRLF in a Windows checkout. Bind the original Git
  // bytes, allowing only that checkout conversion; never relabel the capture.
  assert.equal(
    createHash("sha256")
      .update(hubProjectionSource.replace(/\r\n/g, "\n"))
      .digest("hex"),
    hubSourceBridge.fixture.gitBytesSha256,
  );
  assert.equal(hubSourceBridge.files.length, 10);
  assert.deepEqual(
    hubSourceBridge.files.map((f) => f.path).sort(),
    Object.keys(hubProjection.provenance.sourceBlobs).sort(),
  );
  for (const file of hubSourceBridge.files) {
    const captured = hubProjection.provenance.sourceBlobs[file.path];
    assert.equal(file.capturedGitBlob, captured.gitBlob);
    assert.equal(file.targetGitBlob, captured.gitBlob);
    assert.equal(file.capturedSha256, captured.sha256);
    assert.equal(file.targetSha256, captured.sha256);
  }
  assert.equal(
    hubProjection.provenance.sourceBlobs["server/router-credentials/service.ts"]
      .gitBlob,
    "beadba8563481fabf7af7f2079ffda9876106aa9",
  );
  assert.equal(
    hubProjection.provenance.sourceBlobs["server/agent-sessions/routes.ts"]
      .gitBlob,
    "9c19c29cb2fc4bb3d48bc74d89f1a461e4b5a6ea",
  );
  assert(Number.isFinite(hubObservation));
  const { inventory, details } = hubProjection.bootstrap;
  const renewed = a.renewedCredentials(inventory, details, hubObservation);
  for (const provider of ["codex", "qwen"]) {
    const binding = inventory.bindings.find(
      (b) => b.id === a.PROFILES[provider].binding,
    );
    assert.equal(binding.canRenew, false);
    assert.equal(Object.hasOwn(binding, "can_renew"), false);
    assert.equal(details[provider].binding.canRenew, false);
    assert.equal(renewed.profiles[provider].generation, 2);
    assert.equal(
      renewed.profiles[provider].credential,
      details[provider].credential.id,
    );
    assert.equal(
      renewed.profiles[provider].expiresAt,
      Date.parse(details[provider].credential.expiresAt),
    );
  }
});

test("renewed inventory refuses malformed consumer rows before binding filtering", () => {
  for (const row of [
    null,
    [],
    "active",
    { state: "active" },
    { bindingId: p.binding, state: "active" },
    { binding_id: "not-a-uuid", state: "active" },
    { binding_id: p.binding },
    { binding_id: p.binding, state: "unknown" },
    { binding_id: OTHER, state: "unknown" },
  ]) {
    const { inventory, details } = clone(hubProjection.bootstrap);
    inventory.consumers.push(row);
    assert.throws(
      () => a.renewedCredentials(inventory, details, hubObservation),
      /Credential consumer inventory malformed/,
    );
  }
});

test("renewed inventory accepts valid unrelated and terminal consumer rows", () => {
  const { inventory, details } = clone(hubProjection.bootstrap);
  const expected = a.renewedCredentials(inventory, details, hubObservation);
  inventory.consumers.push(
    { binding_id: OTHER, state: "active" },
    { binding_id: p.binding, state: "released" },
    { binding_id: a.PROFILES.qwen.binding, state: "blocked" },
  );
  assert.deepEqual(
    a.renewedCredentials(inventory, details, hubObservation),
    expected,
  );
  inventory.consumers.push({ binding_id: p.binding, state: "active" });
  assert.throws(
    () => a.renewedCredentials(inventory, details, hubObservation),
    /still active/,
  );
});

test("captured hub metadata never permits a missing or alternate canRenew spelling", () => {
  for (const provider of ["codex", "qwen"]) {
    for (const view of ["inventory", "detail"]) {
      for (const rename of [false, true]) {
        const { inventory, details } = clone(hubProjection.bootstrap);
        const binding =
          view === "inventory"
            ? inventory.bindings.find(
                (b) => b.id === a.PROFILES[provider].binding,
              )
            : details[provider].binding;
        delete binding.canRenew;
        if (rename) binding.can_renew = false;
        assert.throws(() =>
          a.renewedCredentials(inventory, details, hubObservation),
        );
      }
    }
  }
});

function capturedDelivery(provider, payload) {
  const renewed = a.renewedCredentials(
    hubProjection.bootstrap.inventory,
    hubProjection.bootstrap.details,
    hubObservation,
  );
  const id = payload.router.sessionId;
  const h = harness(({ path: url }) => {
    if (url === "/api/agent-sessions/" + id + "/router") return payload.router;
    if (
      url ===
      "/api/router-credentials/keys/" + renewed.profiles[provider].credential
    )
      return payload.detail;
    if (url === "/api/agent-sessions/" + id) return payload.session;
    // Communication readiness alone is stubbed; this fixture qualifies metadata
    // projections, not communication, provider execution or live readiness.
    if (url === "/api/agent-sessions/" + id + "/communication")
      return { sessionId: id, status: "ready" };
    throw Error("Unexpected captured delivery request " + url);
  });
  h.runner.now = () => hubObservation;
  h.runner.profiles = renewed.profiles;
  h.runner.effectiveExpiry = renewed.expiresAt;
  h.runner.state.successorCredentials = renewed;
  h.runner.run = { provider };
  h.runner.session = { id, generation: payload.router.generation };
  return h;
}

test("renewed delivery accepts actual 74d1 HTTP router and active-consumer detail", async () => {
  for (const provider of ["codex", "qwen"]) {
    const payload = hubProjection.delivery[provider];
    const h = capturedDelivery(provider, payload);
    await h.runner.checkDelivery();
    assert.equal(payload.router.consumer.state, "active");
    assert.equal(h.calls.length, 4);
    assert(h.calls.every((c) => c.method === "GET"));
    assert.equal(h.runner.state.operations.length, 0);
    assert.equal(h.runner.state.evidence.length, 1);
    assert.equal(h.runner.state.evidence[0].credentialGeneration, 2);
    assert.equal(
      h.runner.state.evidence[0].expiresAt,
      Date.parse(payload.router.credential.expiresAt),
    );
  }
});

test("captured router responses refuse missing delivery identity and authority fields", async () => {
  const required = {
    connection: ["id"],
    binding: ["revision", "generation"],
    credential: [
      "generation",
      "expiresAt",
      "remoteAcknowledged",
      "revokeRequested",
    ],
    consumer: ["generation", "failureCode", "credentialExpiresAt"],
  };
  for (const [section, fields] of Object.entries(required)) {
    for (const field of fields) {
      const payload = clone(hubProjection.delivery.codex);
      delete payload.router[section][field];
      const h = capturedDelivery("codex", payload);
      await assert.rejects(h.runner.checkDelivery(), /not ready|changed/);
      assert(h.calls.every((c) => c.method === "GET"));
      assert.equal(h.runner.state.operations.length, 0);
      assert.equal(h.runner.state.evidence.length, 0);
    }
  }
});

test("renewed credential projection preserves reviewed scope and selects the earlier expiry", () => {
  const installing = renewedFixture(),
    ready = renewedFixture("ready");
  const first = a.renewedCredentials(
    installing.inventory,
    installing.details,
    POST_EXPIRY,
  );
  assert.deepEqual(
    a.renewedCredentials(ready.inventory, ready.details, POST_EXPIRY),
    first,
  );
  assert.equal(first.expiresAt, POST_EXPIRY + 3600000);
  for (const provider of ["codex", "qwen"]) {
    assert.equal(first.profiles[provider].generation, 2);
    assert.equal(
      first.profiles[provider].binding,
      a.PROFILES[provider].binding,
    );
    assert.equal(
      first.profiles[provider].definitionHash,
      a.PROFILES[provider].definitionHash,
    );
    assert.equal(first.profiles[provider].run, a.PROFILES[provider].run);
    assert.notEqual(first.profiles[provider].key, a.PROFILES[provider].key);
    assert(Object.isFrozen(first.profiles[provider]));
  }
  installing.details.codex.credential.unrelated = "must-not-export";
  assert(
    !JSON.stringify(
      a.renewedCredentials(
        installing.inventory,
        installing.details,
        POST_EXPIRY,
      ),
    ).includes("must-not-export"),
  );
});

test("renewed credential validator rejects scope drift, incomplete revocation and stale authority", () => {
  for (const mutate of [
    (f) => {
      f.inventory.bindings[0].grant_spec.routes.push("other");
    },
    (f) => {
      f.inventory.bindings[1].grant_spec.access = ["subscription"];
    },
    (f) => {
      f.inventory.bindings[0].grant_spec.lifetimeSeconds++;
    },
    (f) => {
      f.inventory.bindings[0].id = REQUEST;
    },
    (f) => {
      f.inventory.bindings[0].host_id = OTHER;
    },
    (f) => {
      f.inventory.bindings[0].state = "unknown";
    },
    (f) => {
      f.inventory.bindings[0].canRenew = true;
    },
    (f) => {
      delete f.inventory.bindings[0].canRenew;
    },
    (f) => {
      f.inventory.bindings[0].current_credential_id =
        a.PROFILES.codex.credential;
    },
    (f) => {
      f.inventory.credentials[0].state = "revocation_pending";
    },
    (f) => {
      f.inventory.credentials[0].cleanup_pending = true;
    },
    (f) => {
      f.inventory.credentials[1].remote_acknowledged = false;
    },
    (f) => {
      f.inventory.credentials[1].revoke_requested = true;
    },
    (f) => {
      f.inventory.credentials[1].failure_code = "unavailable";
    },
    (f) => {
      f.inventory.credentials[1].expires_at = date(POST_EXPIRY);
    },
    (f) => {
      f.inventory.credentials[1].router_key_id = a.PROFILES.codex.key;
    },
    (f) => {
      f.inventory.credentials[1].router_key_id = a.PROFILES.qwen.key;
      f.details.codex.credential.routerKeyId = a.PROFILES.qwen.key;
    },
    (f) => {
      f.inventory.credentials[3].router_key_id =
        f.inventory.credentials[1].router_key_id;
      f.details.qwen.credential.routerKeyId =
        f.inventory.credentials[1].router_key_id;
    },
    (f) => {
      f.inventory.credentials.pop();
    },
    (f) => {
      f.inventory.consumers.push({ binding_id: p.binding, state: "active" });
    },
    (f) => {
      f.details.codex.credential.bindingId = REQUEST;
    },
    (f) => {
      f.details.codex.credential.expiresAt = date(POST_EXPIRY + 1);
    },
    (f) => {
      f.details.codex.binding.pendingCredentialId = REQUEST;
    },
    (f) => {
      delete f.details.codex.binding.canRenew;
    },
    (f) => {
      f.details.qwen.credential.state = "unknown";
    },
    (f) => {
      f.details.qwen.consumers.push({ state: "active" });
    },
  ]) {
    const f = renewedFixture();
    mutate(f);
    assert.throws(() =>
      a.renewedCredentials(f.inventory, f.details, POST_EXPIRY),
    );
  }
  const f = renewedFixture();
  assert.throws(
    () => a.renewedCredentials(f.inventory, f.details, NOW),
    /Post-expiry/,
  );
});

test("new-principal snapshots cannot rewrite or delta against the old predecessor", () => {
  const f = renewedFixture(),
    current = a.renewedCredentials(f.inventory, f.details, POST_EXPIRY);
  const old = trajectory(),
    fresh = trajectory(POST_EXPIRY);
  fresh.scope.principal = "key:" + current.profiles.codex.key;
  fresh.scope.since = POST_EXPIRY;
  assert.equal(
    a.snapshot(fresh, "codex", a.ROUTER, current.profiles).totalAttempts,
    0,
  );
  assert.throws(
    () => a.snapshot(old, "codex", a.ROUTER, current.profiles),
    /scope/,
  );
  assert.throws(() => a.snapshot(fresh, "codex"), /scope/);
  assert.throws(() => a.delta(old, fresh, "codex", current.profiles));
  assert.equal(
    a.stoppedPredecessor(JSON.stringify(stoppedReport())).first.scope.principal,
    "key:" + p.key,
  );
});

function predecessorResponse(prior, c) {
  assert.equal(c.method, "GET", "Replacement preflight must not dispatch work");
  const prefix = "/api/agent-sessions/" + a.STOPPED_SESSION;
  if (c.path === prefix + "/control") return { lease: null };
  if (c.path.startsWith(prefix + "/operations/")) {
    const op = prior.commands.find((o) => c.path.endsWith(o.body.operationId));
    assert(op);
    return {
      command: {
        id: OP,
        operation_id: op.body.operationId,
        session_id: a.STOPPED_SESSION,
        kind: op.body.kind,
        generation: 1,
        state: "completed",
        receipt: {
          status: "completed",
          result:
            op.body.kind === "prompt"
              ? { stopReason: "end_turn" }
              : { cancelSent: true },
        },
      },
    };
  }
  if (c.path === "/api/agent-permissions/" + a.STOPPED_PERMISSION)
    return {
      id: a.STOPPED_PERMISSION,
      session_id: a.STOPPED_SESSION,
      generation: 1,
      state: "resolved",
      decision: { outcome: "cancelled" },
      context: { title: "Allow mcp__relay__relay_messages_read?" },
    };
  assert.equal(c.path, prefix, "Unexpected predecessor endpoint");
  return session({ id: a.STOPPED_SESSION, state: "closed" });
}

async function replacementHarness({ drift, approval } = {}) {
  const raw = JSON.stringify(stoppedReport(), null, 2);
  const prior = a.stoppedPredecessor(raw);
  const h = harness((c) => {
    const value = predecessorResponse(prior, c);
    if (drift) drift(value, c);
    return value;
  });
  h.runner.state.predecessor = {
    raw,
    canonicalSha256: await a.digest(webcrypto, prior.report),
    source: a.STOPPED_SOURCE,
    sessionId: a.STOPPED_SESSION,
  };
  h.runner.env.ask = async (_title, _text, validate) => {
    h.advance(6000);
    const rows = ["338869f5-complete", "811cab9f-cancelled"].map((id, i) => ({
      ...trajectoryRow(),
      id,
      status: i ? "cancelled" : "complete",
      startedAt: NOW - 50 + i,
      endedAt: NOW - 20 + i,
    }));
    const value = {
      confirmReplacement: true,
      reportDownloaded: true,
      operatorVerified: true,
      priorReportCanonicalSha256: h.runner.state.predecessor.canonicalSha256,
      windowStartUtc: date(h.runner.now()),
      windowEndUtc: date(h.runner.now() + 900000),
      trajectory: trajectory(h.runner.now(), rows),
      marker: { ...marker(), observedUtc: date(h.runner.now()) },
    };
    if (approval) approval(value);
    return validate(value);
  };
  return { ...h, raw, prior };
}

async function renewalReviewHarness(options = {}) {
  const h = await replacementHarness(options);
  h.advance(POST_EXPIRY - NOW);
  h.runner.env.load = () => h.raw;
  let metadata = renewedFixture(),
    inventories = 0,
    confirmations = 0;
  const originalFetch = h.runner.env.fetch,
    originalAsk = h.runner.env.ask;
  h.runner.env.fetch = async (path, init) => {
    if (
      path === "/api/router-credentials" ||
      path.startsWith("/api/router-credentials/keys/")
    ) {
      assert.equal(init.method, "GET");
      h.calls.push({ path, method: init.method, init });
      let value;
      if (path === "/api/router-credentials") {
        inventories++;
        if (options.installDuringReview && inventories === 2)
          metadata = renewedFixture("ready");
        value = metadata.inventory;
      } else {
        value = Object.values(metadata.details).find((v) =>
          path.endsWith(v.credential.id),
        );
        assert(
          value,
          "Only selected renewed credential details may be fetched",
        );
      }
      if (options.metadataDrift)
        options.metadataDrift(metadata, inventories, path);
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        text: async () => JSON.stringify(value),
      };
    }
    return originalFetch(path, init);
  };
  h.runner.env.ask = async (title, text, validate, timeout) => {
    if (title === "Review already-renewed phase-A credentials") {
      assert(timeout <= 120000);
      assert(
        !text.includes(a.RUN_SOURCE),
        "Attestation must not supply its expected source",
      );
      assert(text.includes("OBSERVED_SOURCE_SHA"));
      const value = {
        confirmMetadataReview: true,
        reportDownloaded: true,
        operatorVerified: true,
        deploymentVerified: true,
        observedUtc: date(h.runner.now()),
        hubSource: a.RUN_SOURCE,
      };
      if (options.bootstrap) options.bootstrap(value, h);
      return validate(value);
    }
    if (title === "Confirm exact renewed credentials for all three phases") {
      confirmations++;
      const projection = a.renewedCredentials(
        metadata.inventory,
        metadata.details,
        h.runner.now(),
      );
      const value = { confirmRenewedCredentials: true, ...projection };
      if (options.confirmation) options.confirmation(value, h);
      return validate(value);
    }
    return originalAsk(title, text, validate, timeout);
  };
  return {
    ...h,
    inventories: () => inventories,
    confirmations: () => confirmations,
  };
}

test("held module variant refuses renewal review before metadata or storage mutation", async () => {
  const h = await renewalReviewHarness();
  const runner = new held.Runner(h.runner.env);
  runner.state.predecessor = { raw: h.raw };
  await assert.rejects(runner.explicitReview(), /qualification|source/i);
  await assert.rejects(runner.replaceStopped(), /qualified|draft/i);
  assert.equal(h.calls.length, 0);
  assert.equal(h.saves.length, 0);
});

test("explicit renewed review reads twice then preserves old accounting without any mutation", async () => {
  const h = await renewalReviewHarness({ installDuringReview: true });
  await h.runner.explicitReview();
  assert.equal(h.inventories(), 2);
  assert.equal(h.confirmations(), 1);
  assert.equal(h.calls.length, 6);
  assert(h.calls.every((c) => c.method === "GET"));
  assert.equal(h.runner.effectiveExpiry, POST_EXPIRY + 3600000);
  assert.equal(h.runner.profiles.codex.generation, 2);
  assert.equal(h.runner.state.predecessor.raw, h.raw);
  await h.runner.replacementPreflight(h.prior);
  assert.equal(h.calls.length, 12);
  assert.equal(h.runner.state.replacement.carriedPrompts, 1);
  assert.equal(h.runner.state.replacement.carriedAttempts, 2);
  assert.equal(
    h.runner.state.replacement.priorObservation.scope.principal,
    "key:" + p.key,
  );
  assert.notEqual(h.runner.profiles.codex.key, p.key);
  assert.equal(h.runner.state.operations.length, 0);
});

test("renewed metadata review rejects decline, deployment mismatch, timeout and second-read drift", async () => {
  for (const options of [
    {
      bootstrap: (v) => {
        v.confirmMetadataReview = false;
      },
    },
    {
      bootstrap: (v) => {
        v.hubSource = a.SOURCE;
      },
    },
    {
      bootstrap: (v) => {
        v.deploymentVerified = false;
      },
    },
    {
      bootstrap: (v) => {
        v.observedUtc = date(POST_EXPIRY - 1);
      },
    },
    {
      bootstrap: (_v, h) => {
        h.advance(120001);
      },
    },
    {
      confirmation: (v) => {
        v.confirmRenewedCredentials = false;
      },
    },
    {
      confirmation: (_v, h) => {
        h.advance(120001);
      },
    },
    {
      metadataDrift: (f, n) => {
        if (n === 2) {
          f.inventory.credentials[1].expires_at = date(POST_EXPIRY + 7000000);
          f.details.codex.credential.expiresAt = date(POST_EXPIRY + 7000000);
        }
      },
    },
  ]) {
    const h = await renewalReviewHarness(options);
    await assert.rejects(h.runner.explicitReview());
    assert.equal(h.runner.state.successorCredentials, undefined);
    assert.equal(h.runner.effectiveExpiry, a.EXPIRES);
    assert.equal(h.runner.state.predecessor.raw, h.raw);
    assert(h.calls.every((c) => c.method === "GET"));
    if (options.bootstrap) assert.equal(h.calls.length, 0);
  }
});

test("credential refresh refuses revocation after review before a launch is dispatched", async () => {
  const h = await renewalReviewHarness({
    metadataDrift: (f, n) => {
      if (n >= 3) f.inventory.credentials[1].revoke_requested = true;
    },
  });
  await h.runner.explicitReview();
  h.runner.run = { provider: "codex", deadline: POST_EXPIRY + 900000 };
  await assert.rejects(h.runner.launch(), /credential|acknowledged/i);
  assert(h.calls.every((c) => c.method === "GET"));
  assert.equal(h.runner.state.operations.length, 0);
});

test("replacement preflight independently settles the predecessor using GETs and carries its budget", async () => {
  const h = await replacementHarness();
  await h.runner.replacementPreflight(h.prior);
  assert.equal(h.calls.length, 6);
  assert(h.calls.every((c) => c.method === "GET"));
  assert.equal(h.runner.state.predecessor.raw, h.raw);
  assert.equal(
    h.runner.state.predecessor.canonicalSha256,
    await a.digest(webcrypto, h.prior.report),
  );
  const replacement = h.runner.state.replacement;
  assert.equal(replacement.carriedPrompts, 1);
  assert.equal(replacement.carriedAttempts, 2);
  assert.equal(replacement.originalDeadline, NOW - 1);
  assert.equal(replacement.deadline - replacement.start, 900000);
  assert.equal(replacement.priorObservation.source, a.ROUTER);
  assert.equal(h.prior.first.source, a.STOPPED_SOURCE);
  assert.deepEqual(replacement.priorObservation.scope, h.prior.first.scope);
});

test("replacement declines or changed closure/receipts/permission never dispatch a new launch", async () => {
  const options = [
    {
      approval: (v) => {
        v.confirmReplacement = false;
      },
    },
    {
      approval: (v) => {
        v.priorReportCanonicalSha256 = "0".repeat(64);
      },
    },
    {
      approval: (v) => {
        v.windowEndUtc = date(NOW + 906001);
      },
    },
    {
      approval: (v) => {
        v.trajectory.scope.since++;
      },
    },
    {
      approval: (v) => {
        v.trajectory.rows[0].attempts++;
        v.trajectory.totalAttempts++;
      },
    },
    {
      approval: (v) => {
        v.trajectory.rows[0].id = "unrecognized";
      },
    },
    {
      approval: (v) => {
        v.marker.files.denied = { state: "present" };
      },
    },
    {
      drift: (v, c) => {
        if (c.path.endsWith(a.STOPPED_SESSION)) v.state = "open";
      },
    },
    {
      drift: (v, c) => {
        if (c.path.endsWith("/control")) v.lease = { id: LEASE };
      },
    },
    {
      drift: (v) => {
        if (v.command) v.command.state = "accepted";
      },
    },
    {
      drift: (v) => {
        if (v.command) v.command.generation = 2;
      },
    },
    {
      drift: (v) => {
        if (v.decision) v.decision.outcome = "selected";
      },
    },
  ];
  for (const option of options) {
    const h = await replacementHarness(option);
    await assert.rejects(h.runner.replacementPreflight(h.prior));
    assert(h.calls.every((c) => c.method === "GET"));
    assert.equal(h.runner.state.replacement, undefined);
    assert.equal(h.runner.state.predecessor.raw, h.raw);
  }
});

test("actual public entry preserves the raw predecessor when owner declines", async () => {
  const h = await renewalReviewHarness({
    approval: (v) => {
      v.confirmReplacement = false;
    },
  });
  h.runner.env.load = () => h.raw;
  // Exercise the actual wrapper, not merely a pre-populated predecessor state.
  delete h.runner.state.predecessor;
  await assert.rejects(h.runner.replaceStopped(), /Explicit owner replacement/);
  assert.equal(h.runner.state.predecessor.raw, h.raw);
  assert.equal(
    h.runner.state.predecessor.canonicalSha256,
    await a.digest(webcrypto, JSON.parse(h.raw)),
  );
  assert.equal(h.runner.state.completed, false);
  assert.equal(h.runner.state.status, "stopped");
  assert(h.calls.every((c) => c.method === "GET"));
  assert(h.saves.every((s) => s.predecessor.raw === h.raw));
});

test("replacement lifecycle retains one prior prompt and two attempts within the unchanged caps", async () => {
  const f = phaseServer();
  f.runner.state.replacement = {
    deadline: NOW + 900000,
    carriedPrompts: 1,
    carriedAttempts: 2,
    priorObservation: trajectory(NOW - 10000),
    marker: marker(),
  };
  await f.runner.phase("codex");
  const run = f.runner.state.runs[0];
  assert.equal(run.prompts, 12);
  assert.equal(run.carriedAttempts, 2);
  assert.equal(run.deadline, NOW + 900000);
  assert.equal(
    f.calls.filter((c) => c.method === "POST" && c.body?.kind === "prompt")
      .length,
    11,
  );
  assert.equal(run.firstSnapshot.totalAttempts, 0);
  assert(
    run.firstSnapshot.scope.since >
      f.runner.state.replacement.priorObservation.observedAt,
  );
  assert(run.lastSnapshot.totalAttempts + run.carriedAttempts <= 20);
  assert.equal(f.effects.get(p.run), 1);
});

test("expired replacement refuses before profile reads and a window expiring during handoff never launches", async () => {
  const expired = harness();
  expired.runner.state.replacement = {
    deadline: NOW,
    carriedPrompts: 1,
    carriedAttempts: 2,
    priorObservation: trajectory(NOW - 10000),
    marker: marker(),
  };
  await assert.rejects(
    expired.runner.phase("codex"),
    /deadline|window|expired/i,
  );
  assert.deepEqual(expired.calls, []);
  assert.equal(expired.runner.state.runs.length, 0);

  const f = phaseServer();
  f.runner.state.replacement = {
    deadline: NOW + 900000,
    carriedPrompts: 1,
    carriedAttempts: 2,
    priorObservation: trajectory(NOW - 10000),
    marker: marker(),
  };
  const ask = f.runner.env.ask;
  let handoffs = 0;
  f.runner.env.ask = async (...args) => {
    const result = await ask(...args);
    if (args[0].includes("pre-launch handoff")) {
      handoffs++;
      await f.runner.sleep(900001);
    }
    return result;
  };
  await assert.rejects(f.runner.phase("codex"), /deadline|window|expired/i);
  assert.equal(handoffs, 1);
  assert(f.calls.some((c) => c.path === "/api/agent-profiles"));
  assert.equal(f.calls.filter((c) => c.method !== "GET").length, 0);
});

test("replacement final cleanup requires the old sixth launch as well as the five new launches", async () => {
  const report = stoppedReport();
  const h = finalRevocationServer({ predecessor: report });
  await h.runner.revokeFinal();
  assert.equal(h.runner.state.finalRevocation.status, "confirmed");
  const missing = finalRevocationServer({
    predecessor: report,
    omitPredecessorCleanup: true,
  });
  await assert.rejects(missing.runner.revokeFinal());
  assert.notEqual(missing.runner.state.finalRevocation.status, "confirmed");
});

test("explicit disabled-holder fixture blocks replacement for every saved-report state without effects", () => {
  for (const [raw, savedSuccessor] of [
    [null],
    ["{invalid"],
    [JSON.stringify({ version: 1, status: "stopped" })],
    [JSON.stringify(stoppedReport())],
    [
      JSON.stringify({
        ...stoppedReport(),
        predecessor: { raw: JSON.stringify(stoppedReport()) },
      }),
      true,
    ],
  ]) {
    const calls = [],
      writes = [];
    const createElement = (tag) => {
      const node = {
        tagName: tag,
        style: {},
        children: [],
        disabled: false,
        appendChild(child) {
          this.children.push(child);
        },
      };
      return node;
    };
    const body = createElement("body");
    holderDisabled.mount({
      location: { origin: a.ORIGIN },
      crypto: webcrypto,
      document: { createElement, body, getElementById: () => null },
      localStorage: {
        getItem: () => raw,
        setItem: (...args) => writes.push(args),
        removeItem: (...args) => writes.push(args),
        clear: () => writes.push(["clear"]),
      },
      fetch: (...args) => {
        calls.push(args);
        throw Error("Mount dispatched a request");
      },
      setTimeout: () => {
        throw Error("Mount started a checkpoint timer");
      },
      setInterval: () => {
        throw Error("Mount started a heartbeat");
      },
      addEventListener: (name, listener) => {
        assert.equal(name, "beforeunload");
        assert.equal(typeof listener, "function");
      },
    });
    const descendants = (node) =>
      node.children.flatMap((child) => [child, ...descendants(child)]);
    const button = (text) => {
      const matches = descendants(body).filter(
        (n) => n.tagName === "button" && n.textContent === text,
      );
      assert.equal(matches.length, 1);
      return matches[0];
    };
    assert.equal(button("Start phase A").disabled, true);
    assert.equal(button("Review stopped-run replacement").disabled, true);
    assert.equal(button("Confirm current checkpoint").disabled, true);
    assert.equal(button("Review final grant revocation").disabled, true);
    assert.equal(body.children.length, 1);
    assert.equal(body.children[0].id, "relay-pi-acceptance-panel");
    assert(
      descendants(body).some(
        (n) =>
          n.tagName === "p" &&
          n.textContent.includes("Holder controls are not qualified"),
      ),
    );
    if (savedSuccessor)
      assert(
        descendants(body).some(
          (n) =>
            n.tagName === "p" && n.textContent.startsWith("Saved run exists:"),
        ),
      );
    assert.deepEqual(calls, []);
    assert.deepEqual(writes, []);
  }
});

test("requiring the browser script does not dispatch work", () => {
  const h = harness();
  assert.equal(h.calls.length, 0);
  assert.equal(h.saves.length, 0);
  assert.equal(h.runner.state.completed, false);
});
test("explicit disabled-holder fixture rejects both entry points before storage, owner questions or requests", async () => {
  for (const entry of ["startRun", "replaceStopped"]) {
    const h = harness();
    h.runner = new holderDisabled.Runner(h.runner.env);
    const initial = clone(h.runner.state);
    h.runner.env.load = () => {
      throw Error("Hold must precede storage read");
    };
    h.runner.env.ask = () => {
      throw Error("Hold must precede owner questions");
    };
    await assert.rejects(
      h.runner[entry](true),
      /Holder controls are not qualified/,
    );
    assert.deepEqual(h.runner.state, initial);
    assert.deepEqual(h.calls, []);
    assert.deepEqual(h.saves, []);
  }
});
test("unqualified draft start refuses before network, checkpoints or saved-run mutation", async () => {
  const h = harness();
  // Exercise the disabled packet even when the reviewed successor is enabled.
  // This keeps the no-dispatch regression without editing runtime authority.
  const source = fs.readFileSync(
    path.join(__dirname, "owner-phase-a.js"),
    "utf8",
  );
  assert.match(source, /const QUALIFIED_INPUTS = (?:true|false);/);
  const context = { module: { exports: {} }, TextEncoder };
  vm.runInNewContext(
    source.replace(
      /const QUALIFIED_INPUTS = (?:true|false);/,
      "const QUALIFIED_INPUTS = false;",
    ),
    context,
    { filename: "disabled-owner-phase-a.js", timeout: 1000 },
  );
  h.runner = new context.module.exports.Runner(h.runner.env);
  h.runner.env.load = () => JSON.stringify(stoppedReport());
  h.runner.env.ask = () => {
    throw Error("Draft must not ask to activate");
  };
  await assert.rejects(
    h.runner.replaceStopped(),
    /draft|qualified|publication|pins/i,
  );
  assert.equal(h.calls.length, 0);
  assert.equal(h.saves.length, 0);
  assert.equal(h.runner.state.completed, false);
});
function preservingHandoffFixture() {
  // Synthetic pins exercise the pure validator only. They are not publication
  // evidence and never enable a Runner or replace the production pins.
  const pins = {
    release: "v9.9.9",
    source: "a".repeat(40),
    binarySha256: "b".repeat(64),
  };
  const report = stoppedReport();
  const prior = report.evidence.find((e) => e.kind === "launch");
  const value = {
    holderId: SID,
    journalId: LEASE,
    oldHolderId: prior.holderId,
    oldJournalId: prior.journalId,
    preparationId: OP,
    stateDirectory:
      "/home/c4/.local/state/relay-holder-handoff-44444444-4444-4444-8444-444444444444",
    handoffReceipt: {
      path: "/home/c4/.config/relay-holder/used-handoffs/44444444-4444-4444-8444-444444444444/complete.json",
      sha256: "c".repeat(64),
    },
    observedUtc: date(NOW),
    qualifiedRelease: pins.release,
    holderSource: pins.source,
    holderBinarySha256: pins.binarySha256,
    profilesSha256: holderInputs.relay_holder_profiles_sha256,
    nativeRecovery: 2,
    nativeStateReady: true,
    activeSessions: 0,
    pendingCommands: 0,
    admissionPaused: false,
    preservedOldStateConfirmed: true,
    operatorVerified: true,
  };
  return { value, prior, pins, earliest: NOW - 1000, now: NOW, report };
}

test("pure preserving handoff binds fresh identities and copies only reviewed evidence", () => {
  for (const sameNewIds of [false, true]) {
    for (const observed of [NOW - 1000, NOW, NOW + 5000]) {
      const f = preservingHandoffFixture();
      if (sameNewIds) f.value.journalId = f.value.holderId;
      f.value.observedUtc = date(observed);
      f.value.unreviewedField = "must not enter the receipt";
      f.value.handoffReceipt.unreviewedField = "must not enter the receipt";
      const before = structuredClone(f);
      const result = a.preservingHandoff(
        f.value,
        f.prior,
        f.pins,
        f.earliest,
        f.now,
      );
      assert.deepEqual(f, before, "validation must not alter prior accounting");
      assert.equal(result.holderId, SID);
      assert.equal(result.journalId, sameNewIds ? SID : LEASE);
      assert.equal(result.oldHolderId, f.prior.holderId);
      assert.equal(result.oldJournalId, f.prior.journalId);
      assert.notEqual(result.holderId, f.prior.holderId);
      assert.notEqual(result.journalId, f.prior.journalId);
      assert.equal(result.preparationId, OP);
      assert.equal(result.stateDirectory, f.value.stateDirectory);
      assert.equal(result.observedUtc, date(observed));
      assert.equal(result.qualifiedRelease, f.pins.release);
      assert.equal(result.holderSource, f.pins.source);
      assert.equal(result.holderBinarySha256, f.pins.binarySha256);
      assert.equal(
        result.profilesSha256,
        holderInputs.relay_holder_profiles_sha256,
      );
      assert.deepEqual(result.handoffReceipt, {
        path: f.value.handoffReceipt.path,
        sha256: "c".repeat(64),
      });
      assert.notStrictEqual(result, f.value);
      assert.notStrictEqual(result.handoffReceipt, f.value.handoffReceipt);
      assert.equal(Object.hasOwn(result, "unreviewedField"), false);
      assert.equal(result.nativeRecovery, 2);
      assert.equal(result.nativeStateReady, true);
      assert.equal(result.activeSessions, 0);
      assert.equal(result.pendingCommands, 0);
      assert.equal(result.admissionPaused, false);
      assert.equal(result.preservedOldStateConfirmed, true);
      assert.match(result.provenance, /operator-reported/);
      assert.match(result.provenance, /not a browser host measurement/);
      assert.match(result.provenance, /not .*a new acceptance window/);
      result.handoffReceipt.sha256 = "d".repeat(64);
      assert.deepEqual(f, before, "returned evidence must not alias the input");
    }
  }
});

test("pure preserving handoff refuses unqualified pins and drift without changing accounting", () => {
  const cases = [
    ["null pins", (f) => (f.pins = null)],
    ["missing pins", (f) => (f.pins = undefined)],
    ["empty pins", (f) => (f.pins = {})],
    ["unversioned release", (f) => (f.pins.release = "latest")],
    ["unqualified prerelease", (f) => (f.pins.release = "v9.9.9-rc.1")],
    ["short source hash", (f) => (f.pins.source = "a".repeat(39))],
    ["invalid binary hash", (f) => (f.pins.binarySha256 = "g".repeat(64))],
    ["missing predecessor", (f) => (f.prior = null)],
    ["invalid predecessor holder", (f) => (f.prior.holderId = "unknown")],
    [
      "relabelled predecessor journal",
      (f) => (f.value.oldJournalId = f.prior.journalId = REQUEST),
    ],
    ["inverted observation window", (f) => (f.earliest = f.now + 1)],
    ["nonfinite earliest", (f) => (f.earliest = NaN)],
    ["nonfinite current clock", (f) => (f.now = Infinity)],
    ["null handoff", (f) => (f.value = null)],
    ["missing handoff", (f) => (f.value = undefined)],
    ["empty handoff", (f) => (f.value = {})],
    ["array handoff", (f) => (f.value = [])],
    ["missing preparation", (f) => delete f.value.preparationId],
    ["invalid preparation", (f) => (f.value.preparationId = "unknown")],
    ["other preparation", (f) => (f.value.preparationId = REQUEST)],
    ["changed old holder", (f) => (f.value.oldHolderId = SID)],
    ["changed old journal", (f) => (f.value.oldJournalId = OTHER)],
    ["missing old holder", (f) => delete f.value.oldHolderId],
    ["missing old journal", (f) => delete f.value.oldJournalId],
    ["invalid new holder", (f) => (f.value.holderId = "unknown")],
    ["invalid new journal", (f) => (f.value.journalId = "unknown")],
    ["reused old holder", (f) => (f.value.holderId = f.prior.holderId)],
    ["reused old journal", (f) => (f.value.journalId = f.prior.journalId)],
    [
      "old state directory",
      (f) => (f.value.stateDirectory = "/home/c4/.local/state/relay-holder"),
    ],
    ["state path alias", (f) => (f.value.stateDirectory += "/")],
    [
      "state in another root",
      (f) => (f.value.stateDirectory = "/tmp/relay-holder-handoff-" + OP),
    ],
    ["missing receipt", (f) => delete f.value.handoffReceipt],
    ["null receipt", (f) => (f.value.handoffReceipt = null)],
    [
      "another preparation receipt",
      (f) =>
        (f.value.handoffReceipt.path =
          "/home/c4/.config/relay-holder/used-handoffs/" +
          REQUEST +
          "/complete.json"),
    ],
    [
      "receipt path alias",
      (f) => (f.value.handoffReceipt.path += "/../complete.json"),
    ],
    ["missing receipt hash", (f) => delete f.value.handoffReceipt.sha256],
    [
      "short receipt hash",
      (f) => (f.value.handoffReceipt.sha256 = "c".repeat(63)),
    ],
    [
      "invalid receipt hash",
      (f) => (f.value.handoffReceipt.sha256 = "g".repeat(64)),
    ],
    ["missing observation", (f) => delete f.value.observedUtc],
    ["invalid observation", (f) => (f.value.observedUtc = "unknown")],
    ["stale observation", (f) => (f.value.observedUtc = date(f.earliest - 1))],
    ["future observation", (f) => (f.value.observedUtc = date(f.now + 5001))],
    ["different release", (f) => (f.value.qualifiedRelease = "v9.9.8")],
    ["different source", (f) => (f.value.holderSource = "d".repeat(40))],
    ["missing binary hash", (f) => delete f.value.holderBinarySha256],
    ["different binary", (f) => (f.value.holderBinarySha256 = "d".repeat(64))],
    ["missing profile hash", (f) => delete f.value.profilesSha256],
    ["different profiles", (f) => (f.value.profilesSha256 = "d".repeat(64))],
    [
      "historical revision-three profiles",
      (f) =>
        (f.value.profilesSha256 =
          "58120839759fdff7c8bcd4ef483c75711558613c1487366d8406de4bceaa3b39"),
    ],
    ["old recovery protocol", (f) => (f.value.nativeRecovery = 1)],
    ["string recovery protocol", (f) => (f.value.nativeRecovery = "2")],
    ["unready native state", (f) => (f.value.nativeStateReady = false)],
    ["active session", (f) => (f.value.activeSessions = 1)],
    ["string active count", (f) => (f.value.activeSessions = "0")],
    ["pending command", (f) => (f.value.pendingCommands = 1)],
    ["missing pending count", (f) => delete f.value.pendingCommands],
    ["paused admission", (f) => (f.value.admissionPaused = true)],
    [
      "unconfirmed preserved state",
      (f) => (f.value.preservedOldStateConfirmed = false),
    ],
    [
      "missing preserved-state confirmation",
      (f) => delete f.value.preservedOldStateConfirmed,
    ],
    ["unverified operator", (f) => (f.value.operatorVerified = false)],
    [
      "string operator confirmation",
      (f) => (f.value.operatorVerified = "true"),
    ],
  ];
  for (const [name, mutate] of cases) {
    const f = preservingHandoffFixture();
    mutate(f);
    const before = structuredClone(f);
    assert.throws(
      () => a.preservingHandoff(f.value, f.prior, f.pins, f.earliest, f.now),
      /inputs are not qualified|observation window changed|handoff missing or changed/,
      name,
    );
    assert.deepEqual(
      f,
      before,
      name + " must not change prior evidence/accounting",
    );
  }
});
test("profile hashes bind reviewed definitions after deployed schema defaults", async () => {
  const definitions = JSON.parse(
    fs.readFileSync(path.join(__dirname, "launch-definitions.json"), "utf8"),
  );
  // Independently checked against ApproveLaunchProfile in deployed e843 OCI021a8f:
  // these four communication defaults are the ONLY parsed-definition changes.
  for (const profile of Object.values(a.PROFILES)) {
    const definition = clone(
      definitions.find((d) => d.holder.id === profile.id),
    );
    definition.session.communication = {
      directWake: true,
      broadcastWake: false,
      maxWakesPerHour: 10,
      maxCausalHops: 4,
      ...definition.session.communication,
    };
    assert.equal(await a.digest(webcrypto, definition), profile.definitionHash);
    definition.holder.revision++;
    assert.notEqual(
      await a.digest(webcrypto, definition),
      profile.definitionHash,
    );
  }
});
test("mutation is persisted before a lost response and cannot silently restart", async () => {
  const h = harness((_c, _r, saves) => {
    assert.equal(saves.at(-1).operations[0].status, "sending");
    assert.deepEqual(saves.at(-1).operations[0].body, {
      operationId: OP,
      kind: "prompt",
      text: "READY",
    });
    throw Error("lost response after possible acceptance");
  });
  await assert.rejects(
    h.runner.mutate("/api/agent-sessions/" + SID + "/commands", {
      operationId: OP,
      kind: "prompt",
      text: "READY",
    }),
    /lost response/,
  );
  assert.equal(h.calls.length, 1);
  assert.equal(h.saves.at(-1).operations[0].status, "uncertain");
  assert.equal(h.saves.at(-1).operations[0].body.operationId, OP);
  h.runner.env.load = () => JSON.stringify(h.saves.at(-1));
  await assert.rejects(h.runner.start(), /original stopped report|fresh-start/);
  assert.equal(h.calls.length, 1);
});
test("operation lookup preserves ID and refuses null or mismatched receipts", async () => {
  let response = { command: null };
  const h = harness(() => response);
  const op = {
    sessionId: SID,
    body: { operationId: OP, kind: "prompt", generation: 1 },
  };
  await assert.rejects(h.runner.operation(op), /not visible/);
  response = {
    command: {
      operation_id: OTHER,
      session_id: SID,
      kind: "prompt",
      generation: 1,
      state: "completed",
      receipt: { status: "completed" },
    },
  };
  await assert.rejects(h.runner.operation(op), /identity/);
  assert(
    h.calls.every(
      (c) => c.method === "GET" && c.path.endsWith("/operations/" + OP),
    ),
  );
});
test("completed is not accepted, unknown, rejected or withdrawn", () => {
  const expected = {
    operationId: OP,
    sessionId: SID,
    kind: "prompt",
    generation: 1,
  };
  const row = {
    operation_id: OP,
    session_id: SID,
    kind: "prompt",
    generation: 1,
    state: "accepted",
  };
  assert.equal(a.completed(row, expected), false);
  for (const state of ["unknown", "rejected", "withdrawn", "other"])
    assert.throws(() => a.completed({ ...row, state }, expected));
  assert.throws(
    () => a.completed({ ...row, state: "completed" }, expected),
    /receipt/,
  );
  assert.equal(
    a.completed(
      { ...row, state: "completed", receipt: { status: "completed" } },
      expected,
    ),
    true,
  );
});
test("acquire reads flat session and refuses existing lease without takeover", async () => {
  const h = harness((c) =>
    c.path.endsWith("/control")
      ? { lease: { owned: false, id: OTHER } }
      : session(),
  );
  h.runner.session = session();
  await assert.rejects(h.runner.acquire(), /Existing control lease/);
  assert(h.calls.every((c) => c.method === "GET"));
});
test("lease renewal keeps exact ID; identity change is rejected", async () => {
  let changed = false;
  const h = harness((c) => {
    assert.equal(c.body.leaseId, LEASE);
    return {
      id: changed ? OTHER : LEASE,
      generation: 1,
      expires_at: date(NOW + 60000),
    };
  });
  h.runner.session = session();
  h.runner.lease = { id: LEASE, generation: 1, expires_at: date(NOW + 20000) };
  await h.runner.renew();
  assert.equal(h.runner.lease.id, LEASE);
  changed = true;
  await assert.rejects(h.runner.renew(), /identity changed/);
  assert.equal(h.runner.lease.id, LEASE);
});
test("expired lease and changed session generation block command dispatch", async () => {
  const h = harness(() => session({ generation: 2 }));
  h.runner.session = session();
  h.runner.lease = { id: LEASE, expires_at: date(NOW - 1) };
  await assert.rejects(
    h.runner.command("prompt", { text: "READY" }),
    /lease expired/,
  );
  assert.equal(h.calls.length, 0);
  h.runner.lease.expires_at = date(NOW + 60000);
  await assert.rejects(
    h.runner.command("prompt", { text: "READY" }),
    /identity\/generation/,
  );
  assert(h.calls.every((c) => c.method === "GET"));
});
test("deployed empty permission description fails before approval checkpoint or POST", async () => {
  const d = detail();
  d.context.description = "";
  const h = harness((c) =>
    c.path.endsWith("/permissions") ? [{ id: REQUEST, state: "pending" }] : d,
  );
  h.runner.session = session();
  h.runner.env.ask = () => {
    throw Error("Must not ask owner to approve unverifiable input");
  };
  await assert.rejects(
    h.runner.decide(command, "allow_once"),
    /Full tool input unavailable/,
  );
  assert(h.calls.every((c) => c.method === "GET"));
});
test("once-only permission validates exact input, expiry, options and identity", () => {
  assert.equal(
    a.permission(detail(), SID, 1, command, "allow_once", NOW),
    "yes",
  );
  assert.equal(
    a.permission(detail(), SID, 1, command, "reject_once", NOW),
    "no",
  );
  for (const mutation of [
    (d) => {
      d.context.description = JSON.stringify({
        command: command + "; unexpected",
      });
    },
    (d) => {
      d.context.description = JSON.stringify({ command, cwd: "/elsewhere" });
    },
    (d) => {
      d.context.description = JSON.stringify({ command, timeout: 121 });
    },
    (d) => {
      d.context.options[0].kind = "allow_always";
    },
    (d) => {
      d.context.options.push(clone(d.context.options[0]));
    },
    (d) => {
      d.session_id = OTHER;
    },
    (d) => {
      d.generation = 2;
    },
    (d) => {
      d.expires_at = date(NOW);
    },
    (d) => {
      d.rebinding = true;
    },
  ]) {
    const d = detail();
    mutation(d);
    assert.throws(() => a.permission(d, SID, 1, command, "allow_once", NOW));
  }
});
function communicationDetail(input = {}) {
  const d = detail();
  d.context.title = "Allow mcp__relay__relay_messages_read?";
  d.context.description = JSON.stringify(input);
  return d;
}

test("communication read permission accepts only reviewed inbox/channel cursor shapes and owner-once option", () => {
  const definitions = JSON.parse(
    fs.readFileSync(path.join(__dirname, "launch-definitions.json"), "utf8"),
  );
  assert.equal(a.COMMUNICATION_CHANNEL, "b4f9f5f7-840f-4e62-b6cd-e16c9d46dff1");
  for (const definition of definitions)
    assert(
      definition.session.communication.channels.includes(
        a.COMMUNICATION_CHANNEL,
      ),
    );
  for (const input of [
    {},
    { limit: 1 },
    { limit: 50 },
    { after: "0" },
    { after: "9223372036854775807" },
    { channelId: a.COMMUNICATION_CHANNEL },
    { channelId: a.COMMUNICATION_CHANNEL, after: "10", limit: 5 },
  ]) {
    const d = communicationDetail(input);
    assert.equal(a.communicationPermission(d, SID, 1, NOW), "yes");
    // Validation cannot authorize marker writes. A read may update read_at;
    // this contract does not authorize acknowledgement, send or write tools.
    assert.throws(() => a.permission(d, SID, 1, command, "allow_once", NOW));
  }
  const boundary = communicationDetail();
  boundary.context.toolCallId = "a".repeat(128);
  assert.equal(a.communicationPermission(boundary, SID, 1, NOW), "yes");
});

test("communication read permission refuses malformed, broader or ambiguous requests", () => {
  for (const input of [
    null,
    [],
    "read",
    { command },
    { channelId: OTHER },
    { channelId: null },
    { channelId: "not-a-uuid" },
    { after: "-1" },
    { after: "01" },
    { after: "+1" },
    { after: "1.0" },
    { after: " 1" },
    { after: "9223372036854775808" },
    { after: "99999999999999999999" },
    { after: 1 },
    { after: null },
    { limit: 0 },
    { limit: 51 },
    { limit: 1.5 },
    { limit: "1" },
    { limit: null },
    { limit: true },
    { limit: 1, acknowledge: true },
  ]) {
    assert.throws(
      () => a.communicationPermission(communicationDetail(input), SID, 1, NOW),
      undefined,
      JSON.stringify(input),
    );
  }
  const mutations = [
    (d) => {
      d.context.title = "Allow mcp__relay__relay_messages_ack?";
    },
    (d) => {
      d.context.title = "Allow mcp__relay__relay_messages_send?";
    },
    (d) => {
      d.context.title = "Allow bash?";
    },
    (d) => {
      d.context.description = "";
    },
    (d) => {
      d.context.description = "{";
    },
    (d) => {
      d.context.description = " ".repeat(4000) + "{}";
    },
    // Oversized UTF-8 input is also outside the exact reviewed channel schema.
    (d) => {
      d.context.description = JSON.stringify({ channelId: "é".repeat(2100) });
    },
    (d) => {
      d.context.toolCallId = "";
    },
    (d) => {
      d.context.toolCallId = "a".repeat(129);
    },
    (d) => {
      d.context.toolCallId = "unsafe id";
    },
    (d) => {
      delete d.context.toolCallId;
    },
    (d) => {
      d.context.options[0].kind = "allow_always";
    },
    (d) => {
      d.context.options.push(clone(d.context.options[0]));
    },
    (d) => {
      d.context.options[0].optionId = null;
    },
    (d) => {
      d.session_id = OTHER;
    },
    (d) => {
      d.generation = 2;
    },
    (d) => {
      d.rebinding = true;
    },
    (d) => {
      d.state = "resolved";
    },
    (d) => {
      d.expires_at = date(NOW);
    },
    (d) => {
      d.expires_at = "invalid";
    },
  ];
  for (const mutate of mutations) {
    const d = communicationDetail();
    mutate(d);
    assert.throws(() => a.communicationPermission(d, SID, 1, NOW));
  }
});

test("owner-once communication read waits for accepted delivery and never returns marker approval", async () => {
  for (const outcome of ["accepted", "unknown"]) {
    let posted = false,
      polls = 0,
      asks = 0;
    const communication = [];
    const h = harness((c) => {
      if (c.path.endsWith("/permissions"))
        return [{ id: REQUEST, state: "pending" }];
      if (c.method === "POST") {
        posted = true;
        assert.equal(c.path, "/api/agent-permissions/" + REQUEST + "/decision");
        assert.deepEqual(c.body.outcome, {
          outcome: "selected",
          optionId: "yes",
        });
        assert.equal(c.body.generation, 1);
        return {
          commandId: OP,
          requestId: REQUEST,
          delivery: "pending",
          execution: "unknown",
        };
      }
      const d = communicationDetail({ limit: 1 });
      if (!posted) return d;
      if (++polls === 1) return { ...d, state: "decided", delivery: "offered" };
      return {
        ...d,
        state: outcome === "accepted" ? "resolved" : "decided",
        delivery: outcome,
        decision: { outcome: "selected", optionId: "yes" },
      };
    });
    h.runner.session = session();
    h.runner.env.ask = async (title, text, validate) => {
      asks++;
      assert.equal(title, "Approve one scoped Relay message read");
      assert.match(text, /mark inbox messages read/);
      assert.match(text, /cannot send, acknowledge/);
      return validate(true);
    };
    if (outcome === "accepted") {
      assert.equal(
        await h.runner.decide(undefined, undefined, communication),
        "communication",
      );
      assert.equal(communication.length, 1);
      assert.equal(communication[0].permissionToolCallId, "pi-ui-fixed");
      assert.equal(
        h.runner.state.evidence.at(-1).purpose,
        "relay-message-read",
      );
      assert.match(h.runner.state.evidence.at(-1).execution, /unknown/);
    } else {
      await assert.rejects(
        h.runner.decide(undefined, undefined, communication),
        /delivery failed\/uncertain/,
      );
      assert.equal(communication.length, 0);
      assert.equal(h.runner.state.evidence.length, 0);
    }
    assert.equal(asks, 1);
    assert.equal(polls, 2);
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  }
});

test("communication permission changes during owner review cause zero decision POSTs", async () => {
  for (const change of [
    (d) => {
      d.context.description = JSON.stringify({ limit: 2 });
    },
    (d) => {
      d.context.toolCallId = "pi-ui-other";
    },
    (d) => {
      d.context.options[0].name = "Changed label";
    },
    (d) => {
      d.id = OTHER;
    },
    (d) => {
      d.generation = 2;
    },
    (d) => {
      d.expires_at = date(NOW);
    },
  ]) {
    let reviewed = false;
    const h = harness((c) => {
      if (c.path.endsWith("/permissions"))
        return [{ id: REQUEST, state: "pending" }];
      const d = communicationDetail({ limit: 1 });
      if (reviewed) change(d);
      return d;
    });
    h.runner.session = session();
    h.runner.env.ask = async (_title, _text, validate) => {
      reviewed = true;
      return validate(true);
    };
    await assert.rejects(h.runner.decide(undefined, undefined, []));
    assert.equal(reviewed, true);
    assert(h.calls.every((c) => c.method === "GET"));
  }
});

test("second communication read in one prompt refuses before asking or posting", async () => {
  const h = harness((c) =>
    c.path.endsWith("/permissions")
      ? [{ id: REQUEST, state: "pending" }]
      : communicationDetail(),
  );
  h.runner.session = session();
  h.runner.env.ask = async () => {
    throw Error("Must not ask again");
  };
  await assert.rejects(
    h.runner.decide(undefined, undefined, [{ requestId: OTHER }]),
    /More than one Relay read/,
  );
  assert(h.calls.every((c) => c.method === "GET"));
});

test("recorded communication permission does not consume the separate marker deny decision", async () => {
  let submitted = false;
  const communication = [
    { requestId: OTHER, permissionToolCallId: "pi-ui-read" },
  ];
  const h = harness((c) => {
    if (c.path.endsWith("/permissions"))
      return [{ id: REQUEST, state: "pending" }];
    if (c.method === "POST") {
      submitted = true;
      assert.deepEqual(c.body.outcome, { outcome: "selected", optionId: "no" });
      return {
        commandId: OP,
        requestId: REQUEST,
        delivery: "pending",
        execution: "unknown",
      };
    }
    return submitted
      ? detail({
          state: "resolved",
          delivery: "accepted",
          decision: { outcome: "selected", optionId: "no" },
        })
      : detail();
  });
  h.runner.session = session();
  assert.equal(
    await h.runner.decide(command, "reject_once", communication),
    true,
  );
  assert.equal(communication.length, 1);
  assert.equal(h.runner.state.evidence.at(-1).purpose, "marker");
  assert.equal(h.runner.state.evidence.at(-1).kind, "reject_once");
});

test("permission changes during owner review do not dispatch a decision", async () => {
  let reads = 0,
    asked = 0;
  const h = harness((c) => {
    if (c.path.endsWith("/permissions"))
      return [{ id: REQUEST, state: "pending" }];
    const d = detail();
    if (++reads > 1) d.state = "expired";
    return d;
  });
  h.runner.session = session();
  h.runner.env.ask = async () => {
    asked++;
    return true;
  };
  await assert.rejects(h.runner.decide(command, "allow_once"), /expired/);
  assert.equal(asked, 1);
  assert(h.calls.every((c) => c.method === "GET"));
});
test("multiple pending permissions are not chosen by title or order", async () => {
  const h = harness(() => [
    { id: REQUEST, state: "pending" },
    { id: OTHER, state: "pending" },
  ]);
  h.runner.session = session();
  await assert.rejects(h.runner.decide(command, "allow_once"), /Concurrent/);
  assert.equal(h.calls.length, 1);
});
test("baseline preserves absence, exact commands, freshness and unchanged open observation", () => {
  const before = marker();
  a.marker(before, "codex", p.run, "baseline", NOW);
  a.unchangedBaseline(before, { ...before, observedUtc: date(NOW + 1000) });
  const changed = clone(before);
  changed.files.effects = { state: "present" };
  assert.throws(
    () => a.marker(changed, "codex", p.run, "baseline", NOW),
    /Unexpected effect/,
  );
  assert.throws(() => a.unchangedBaseline(before, changed), /changed/);
  assert.throws(
    () => a.marker(before, "codex", p.run, "baseline", NOW + 120001),
    /fresh/,
  );
  const duplicate = marker("approved");
  duplicate.files.effects.markerOccurrences = 2;
  assert.throws(
    () => a.marker(duplicate, "codex", p.run, "approved", NOW),
    /exactly once/,
  );
});
test("paired trajectory delta rejects truncation, moving baseline and wrong scope", () => {
  const before = trajectory(),
    after = trajectory(NOW + 5, [trajectoryRow()]);
  assert.equal(a.delta(before, after, "codex"), 1);
  assert.throws(
    () => a.snapshot({ ...after, fetchedRows: 500 }, "codex"),
    /Incomplete/,
  );
  const wrong = clone(after);
  wrong.scope.principal = "key:other";
  assert.throws(() => a.delta(before, wrong, "codex"), /scope/);
  const lost = trajectory(NOW + 10);
  assert.throws(() => a.delta(after, lost, "codex"), /disappeared/);
  const changed = clone(after);
  changed.rows[0].incomplete = true;
  assert.throws(() => a.snapshot(changed, "codex"), /Unsettled/);
});
test("lifecycle and effort prompt budgets stop before any fetch", async () => {
  for (const [effort, prompts] of [
    [false, 12],
    [true, 4],
  ]) {
    const h = harness();
    h.runner.run = { effort, prompts, deadline: NOW + 60000 };
    await assert.rejects(
      h.runner.prompt("should never dispatch"),
      /Prompt budget/,
    );
    assert.equal(h.calls.length, 0);
  }
});
test("observed generation ceiling and zero-attempt checkpoints are enforced", async () => {
  for (const [effort, attempts, zero] of [
    [false, 21, false],
    [true, 7, false],
    [false, 1, true],
  ]) {
    const h = harness();
    const before = trajectory(NOW - 10);
    h.runner.run = {
      provider: "codex",
      effort,
      deadline: NOW + 60000,
      firstSnapshot: before,
      lastSnapshot: before,
    };
    h.runner.env.ask = async (_t, _i, validate) => {
      h.advance(6000);
      return validate({
        coverageConfirmed: true,
        trajectory: trajectory(NOW + 6005, [trajectoryRow(attempts)]),
      });
    };
    await assert.rejects(
      h.runner.evidence("bounded", null, zero),
      /budget|zero recorded/,
    );
    assert.deepEqual(h.runner.run.lastSnapshot, before);
    assert.equal(h.calls.length, 0);
  }
});
test("recovery receipt cannot bypass profile revision or workspace identity", async () => {
  const h = harness((c) => {
    if (c.method === "POST")
      return { id: REQUEST, expires_at: date(NOW + 60000) };
    if (c.path === "/api/agent-launches")
      return [
        {
          id: REQUEST,
          operation_id: h.runner.state.operations[0].body.operationId,
          state: "open",
          session_id: OTHER,
          resume_from: SID,
          profile_id: OTHER,
          profile_revision: 99,
          cwd: "/wrong",
          holder_id: a.HOLDER,
          holder_journal_id: a.HOLDER,
          native_recovery_version: 2,
          receipt: {
            status: "completed",
            result: {
              sessionId: OTHER,
              nativeSessionId: OP,
              nativeRecoveryVersion: 2,
            },
          },
        },
      ];
    return session({ id: OTHER });
  });
  h.runner.run = { provider: "codex", deadline: NOW + 120000 };
  h.runner.session = session();
  h.runner.launchRow = {
    session_id: SID,
    receipt: { result: { nativeSessionId: OP } },
  };
  let delivery = 0;
  h.runner.checkDelivery = async () => {
    delivery++;
  };
  h.runner.acquire = async () => {};
  await assert.rejects(h.runner.launch(true), /profile|workspace|identity/i);
  assert.equal(delivery, 0);
});
test("cleanup cannot take over another owner's lease", async () => {
  const h = harness((c) =>
    c.path.endsWith("/control")
      ? { lease: { owned: false, id: LEASE } }
      : session(),
  );
  h.runner.session = session();
  h.runner.lease = { id: LEASE };
  await h.runner.cleanup();
  assert(h.calls.every((c) => c.method === "GET"));
  assert.equal(
    h.runner.state.evidence.at(-1).status,
    "unresolved-owner-inspection-required",
  );
  assert.equal(h.runner.state.completed, false);
});
test("sanitized projections discard unrelated pasted fields at every level", () => {
  const m = marker();
  m.unrelated = "not-for-export";
  m.files.effects.unrelated = "not-for-export";
  const t = trajectory(NOW + 5, [trajectoryRow()]);
  t.unrelated = "not-for-export";
  t.scope.unrelated = "not-for-export";
  t.rows[0].captured = { private: "not-for-export" };
  assert(
    !JSON.stringify(a.marker(m, "codex", p.run, "baseline", NOW)).includes(
      "not-for-export",
    ),
  );
  assert(!JSON.stringify(a.snapshot(t, "codex")).includes("not-for-export"));
});
test("closed session may release an already retired lease; open session may not", async () => {
  for (const state of ["closed", "open"]) {
    const h = harness((c) =>
      c.path.endsWith("/control") ? { lease: null } : session({ state }),
    );
    h.runner.session = session();
    h.runner.lease = { id: LEASE };
    if (state === "closed") {
      await h.runner.release();
      assert.equal(h.runner.lease, null);
    } else {
      await assert.rejects(h.runner.release(), /disappeared/);
      assert.equal(h.runner.lease.id, LEASE);
    }
    assert(h.calls.every((c) => c.method === "GET"));
  }
});
test("timeline drains independent cursors and deduplicates shared rows", async () => {
  const cursor = "2026-11-26 12:00:00+00|" + OP;
  let page = 0;
  const h = harness((c) => {
    const q = new URL(c.path, a.ORIGIN).searchParams;
    if (page++) {
      assert.equal(q.get("after"), "2");
      assert.equal(q.get("promptsAfter"), cursor);
    }
    return {
      sessionId: SID,
      generation: 1,
      text: true,
      state: "open",
      events: [
        {
          sequence: page === 1 ? "2" : "3",
          createdAt: date(NOW),
          event: { kind: "update", type: "agent_message_chunk", text: "READY" },
        },
      ],
      prompts: [{ id: OP, state: "completed" }],
      nextAfter: null,
      nextPromptsAfter: page === 1 ? cursor : null,
    };
  });
  h.runner.session = session();
  const result = await h.runner.timeline();
  assert.deepEqual(
    result.events.map((e) => e.sequence),
    ["2", "3"],
  );
  assert.equal(result.prompts.length, 1);
  assert.equal(h.calls.length, 2);
});
test("bounded prompt requires actual terminal receipt and exact new assistant text", async () => {
  for (const stopReason of ["end_turn", "max_tokens", "cancelled"]) {
    let posted,
      timelines = 0;
    const h = harness((c) => {
      if (c.path.includes("/timeline?"))
        return {
          sessionId: SID,
          generation: 1,
          text: true,
          events: timelines++
            ? [
                {
                  sequence: "1",
                  event: {
                    kind: "update",
                    type: "agent_message_chunk",
                    text: "READY",
                  },
                },
              ]
            : [],
          prompts: posted ? [{ id: REQUEST, state: "completed" }] : [],
          nextAfter: null,
          nextPromptsAfter: null,
        };
      if (c.path.endsWith("/permissions")) return [];
      if (c.path.endsWith("/commands")) {
        posted = c.body;
        return { id: REQUEST };
      }
      if (c.path.includes("/operations/"))
        return {
          command: {
            operation_id: posted.operationId,
            session_id: SID,
            kind: "prompt",
            generation: 1,
            state: "completed",
            receipt: { status: "completed", result: { stopReason } },
          },
        };
      return session();
    });
    h.runner.session = session();
    h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
    h.runner.run = { effort: false, prompts: 0, deadline: NOW + 60000 };
    if (stopReason === "end_turn")
      assert.equal(
        await h.runner.prompt("Reply READY", { expectedText: "READY" }),
        "READY",
      );
    else
      await assert.rejects(
        h.runner.prompt("Reply READY", { expectedText: "READY" }),
        /stop reason/,
      );
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
    assert.equal(h.runner.run.prompts, 1);
  }
});
function nativeReadEvents() {
  return [
    {
      sequence: "1",
      event: {
        kind: "update",
        type: "tool_call",
        toolCallId: "native-read-1",
        title: "mcp__relay__relay_messages_read",
        status: "pending",
      },
    },
    {
      sequence: "2",
      event: {
        kind: "update",
        type: "tool_call_update",
        toolCallId: "native-read-1",
        status: "in_progress",
      },
    },
    {
      sequence: "3",
      event: {
        kind: "update",
        type: "tool_call_update",
        toolCallId: "native-read-1",
        status: "completed",
      },
    },
  ];
}

test("native communication classification keeps permission UI identity separate and rejects ambiguous outcomes", () => {
  const approvals = [
    { requestId: REQUEST, permissionToolCallId: "pi-ui-fixed" },
  ];
  assert.deepEqual(
    a.communicationToolEvents(nativeReadEvents(), approvals).nativeToolIds,
    ["native-read-1"],
  );
  for (const mutate of [
    (e) => e.splice(0),
    (e) => e.pop(),
    (e) => {
      e[2].event.status = "failed";
    },
    (e) => {
      e[2].event.title = "bash";
    },
    (e) => {
      e[2].event.toolCallId = "unrelated";
    },
    (e) => {
      e[0].event.toolCallId = "bad id";
    },
    (e) => e.push({ ...clone(e[0]), sequence: "4" }),
    (e) =>
      e.push({
        sequence: "4",
        event: {
          kind: "update",
          type: "tool_call_update",
          toolCallId: "native-read-1",
          status: "pending",
        },
      }),
  ]) {
    const events = nativeReadEvents();
    mutate(events);
    assert.throws(() => a.communicationToolEvents(events, approvals));
  }
  assert.throws(() => a.communicationToolEvents(nativeReadEvents(), []));
  const extra = {
    sequence: "4",
    event: {
      kind: "update",
      type: "tool_call",
      toolCallId: "other-tool",
      title: "bash",
      status: "completed",
    },
  };
  assert.throws(() =>
    a.communicationToolEvents([...nativeReadEvents(), extra], approvals),
  );
});

function projectedCodexReadEvents() {
  // Actual Codex IDs can contain '|'; pinned e843 omits those IDs from projection.
  // Model that omission, not a client-side regex expansion or fabricated UI-ID link.
  return nativeReadEvents().map(({ event, ...row }) => {
    const { toolCallId, ...projected } = event;
    return { ...row, event: projected };
  });
}

test("single-active native read classification permits consistently absent projected Codex IDs", () => {
  const approvals = [
    { requestId: REQUEST, permissionToolCallId: "pi-ui-fixed" },
  ];
  const result = a.communicationToolEvents(
    projectedCodexReadEvents(),
    approvals,
  );
  assert.deepEqual(result.nativeToolIds, []);
  for (const mutate of [
    (e) => {
      e[1].event.toolCallId = "late-present-id";
    },
    (e) => e.unshift(clone(e[1])),
    (e) => e.splice(1, 0, clone(e[0])),
    (e) => {
      e[1].event.title = "unknown_tool";
    },
    (e) => {
      e[2].event.status = "failed";
    },
  ]) {
    const events = projectedCodexReadEvents();
    mutate(events);
    assert.throws(() => a.communicationToolEvents(events, approvals));
  }
  const mixed = nativeReadEvents();
  delete mixed[1].event.toolCallId;
  assert.throws(() => a.communicationToolEvents(mixed, approvals));
});

test("ordered marker classification requires exact command and decision-specific native terminal state", () => {
  const events = [
    {
      sequence: "1",
      event: {
        kind: "update",
        type: "tool_call",
        title: "bash",
        status: "pending",
      },
    },
    {
      sequence: "2",
      event: {
        kind: "update",
        type: "tool_call_update",
        title: command,
        status: "in_progress",
      },
    },
    {
      sequence: "3",
      event: {
        kind: "update",
        type: "tool_call_update",
        title: command,
        status: "completed",
      },
    },
  ];
  assert.doesNotThrow(() =>
    a.communicationToolEvents(events, [], { command, decision: "allow_once" }),
  );
  assert.throws(() =>
    a.communicationToolEvents(events, [], { command, decision: "reject_once" }),
  );
  const denied = clone(events);
  denied[2].event.status = "failed";
  assert.doesNotThrow(() =>
    a.communicationToolEvents(denied, [], { command, decision: "reject_once" }),
  );
  for (const mutate of [
    (e) => {
      e[1].event.title = command + "; unexpected";
    },
    (e) => {
      e[1].event.title = "bash";
      e[2].event.title = "bash";
    },
    (e) =>
      e.unshift({ event: { type: "tool_call_update", status: "pending" } }),
    (e) => e.splice(1, 0, clone(e[0])),
    (e) => e.push(...clone(e)),
  ]) {
    const changed = clone(events);
    mutate(changed);
    assert.throws(() =>
      a.communicationToolEvents(changed, [], {
        command,
        decision: "allow_once",
      }),
    );
  }
});

function readyWithCommunication(events, delayedCompletionPolls = 0) {
  let posted,
    timelines = 0,
    decided = false;
  const h = harness((c) => {
    if (c.path.includes("/timeline?"))
      return {
        sessionId: SID,
        generation: 1,
        text: true,
        events: timelines++
          ? [
              ...(timelines <= delayedCompletionPolls + 1
                ? events.filter((e) => e.event.status !== "completed")
                : events),
              {
                sequence: "10",
                event: {
                  kind: "update",
                  type: "agent_message_chunk",
                  text: "READY",
                },
              },
            ]
          : [],
        prompts: posted ? [{ id: OP, state: "completed" }] : [],
        nextAfter: null,
        nextPromptsAfter: null,
      };
    if (c.path.endsWith("/permissions"))
      return decided ? [] : [{ id: REQUEST, state: "pending" }];
    if (c.path.endsWith("/decision")) {
      decided = true;
      assert.deepEqual(c.body.outcome, {
        outcome: "selected",
        optionId: "yes",
      });
      return {
        commandId: OTHER,
        requestId: REQUEST,
        delivery: "pending",
        execution: "unknown",
      };
    }
    if (c.path === "/api/agent-permissions/" + REQUEST) {
      const d = communicationDetail({ limit: 1 });
      return decided
        ? {
            ...d,
            state: "resolved",
            delivery: "accepted",
            decision: { outcome: "selected", optionId: "yes" },
          }
        : d;
    }
    if (c.path.endsWith("/commands")) {
      posted = c.body;
      return { id: OP };
    }
    if (c.path.includes("/operations/"))
      return {
        command: {
          operation_id: posted.operationId,
          session_id: SID,
          kind: "prompt",
          generation: 1,
          state: "completed",
          receipt: { status: "completed", result: { stopReason: "end_turn" } },
        },
      };
    return session();
  });
  h.runner.session = session();
  h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
  h.runner.run = { effort: false, prompts: 0, deadline: NOW + 60000 };
  return h;
}

test("READY permits one owner-confirmed read with distinct UI/native IDs and no marker approval", async () => {
  const h = readyWithCommunication(nativeReadEvents());
  assert.equal(
    await h.runner.prompt("Reply exactly READY", { expectedText: "READY" }),
    "READY",
  );
  const result = h.runner.state.evidence.find((e) => e.kind === "prompt");
  assert.equal(result.permissionExercised, false);
  assert.equal(result.communicationReads, 1);
  assert.equal(
    result.communicationPermissions[0].permissionToolCallId,
    "pi-ui-fixed",
  );
  assert.deepEqual(result.communicationNativeToolIds, ["native-read-1"]);
  assert.match(result.communicationCorrelation, /does not link/);
  assert.equal(h.runner.run.prompts, 1);
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
});

test("READY waits for late native read completion without repeating prompt or permission decision", async () => {
  const h = readyWithCommunication(nativeReadEvents(), 2);
  assert.equal(
    await h.runner.prompt("Reply exactly READY", { expectedText: "READY" }),
    "READY",
  );
  assert.equal(h.calls.filter((c) => c.path.includes("/timeline?")).length, 4);
  assert.equal(h.runner.now(), NOW + 2000);
  assert.equal(
    h.calls.filter((c) => c.method === "POST" && c.path.endsWith("/commands"))
      .length,
    1,
  );
  assert.equal(
    h.calls.filter((c) => c.method === "POST" && c.path.endsWith("/decision"))
      .length,
    1,
  );
  assert.equal(h.runner.run.prompts, 1);
  const result = h.runner.state.evidence.find((e) => e.kind === "prompt");
  assert.equal(result.communicationReads, 1);
  assert.equal(result.permissionExercised, false);
  assert.deepEqual(result.communicationNativeToolIds, ["native-read-1"]);
});

test("READY accepts completed single read with consistently missing pinned-hub native IDs", async () => {
  const h = readyWithCommunication(projectedCodexReadEvents());
  assert.equal(
    await h.runner.prompt("Reply exactly READY", { expectedText: "READY" }),
    "READY",
  );
  const result = h.runner.state.evidence.find((e) => e.kind === "prompt");
  assert.equal(result.permissionExercised, false);
  assert.equal(result.communicationReads, 1);
  assert.deepEqual(result.communicationNativeToolIds, []);
  assert.equal(
    result.communicationPermissions[0].permissionToolCallId,
    "pi-ui-fixed",
  );
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
});

test("READY refuses extra, failed, missing or conflicting communication activity", async () => {
  for (const mutate of [
    (e) =>
      e.push({
        sequence: "4",
        event: {
          kind: "update",
          type: "tool_call",
          title: "bash",
          toolCallId: "other-tool",
          status: "completed",
        },
      }),
    (e) => {
      e[2].event.status = "failed";
    },
    (e) => e.splice(0),
    (e) => {
      e[2].event.title = "mcp__relay__relay_messages_ack";
    },
  ]) {
    const events = nativeReadEvents();
    mutate(events);
    const h = readyWithCommunication(events);
    await assert.rejects(
      h.runner.prompt("Reply exactly READY", { expectedText: "READY" }),
    );
    assert.equal(
      h.runner.state.evidence.filter((e) => e.kind === "prompt").length,
      0,
    );
    assert.equal(h.calls.filter((c) => c.path.endsWith("/decision")).length, 1);
  }
});

test("communication read during marker prompt cannot satisfy the marker permission requirement", async () => {
  const h = readyWithCommunication(nativeReadEvents());
  await assert.rejects(
    h.runner.prompt("Request the fixed marker", {
      tool: command,
      decision: "allow_once",
    }),
    /Expected permission path not exercised/,
  );
  assert.equal(
    h.runner.state.evidence.filter((e) => e.kind === "prompt").length,
    0,
  );
  assert.equal(h.calls.filter((c) => c.path.endsWith("/decision")).length, 1);
});

test("queued permission decision waits for resolved accepted delivery, never execution proof", async () => {
  for (const outcome of ["accepted", "unknown"]) {
    let submitted = false,
      polls = 0;
    const h = harness((c) => {
      if (c.path.endsWith("/permissions"))
        return [{ id: REQUEST, state: "pending" }];
      if (c.method === "POST") {
        submitted = true;
        return {
          commandId: OP,
          requestId: REQUEST,
          delivery: "pending",
          execution: "unknown",
        };
      }
      if (!submitted) return detail();
      if (++polls === 1)
        return detail({ state: "decided", delivery: "offered" });
      return detail({
        state: outcome === "accepted" ? "resolved" : "decided",
        delivery: outcome,
        decision: { outcome: "selected", optionId: "yes" },
      });
    });
    h.runner.session = session();
    if (outcome === "accepted") {
      assert.equal(await h.runner.decide(command, "allow_once"), true);
      assert.match(h.runner.state.evidence.at(-1).execution, /unknown/);
    } else {
      await assert.rejects(
        h.runner.decide(command, "allow_once"),
        /delivery failed\/uncertain/,
      );
      assert.equal(h.runner.state.evidence.length, 0);
    }
    assert.equal(polls, 2);
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  }
});
test("lost prompt POST cleanup observes original operation and narrows once", async () => {
  const commands = new Map();
  let closed = false;
  const h = harness((c) => {
    if (c.path.endsWith("/control"))
      return {
        lease: closed ? null : { id: LEASE, owned: true, generation: 1 },
      };
    if (c.path.endsWith("/permissions")) return [];
    if (c.path.endsWith("/commands")) {
      commands.set(c.body.operationId, c.body);
      if (c.body.kind === "prompt") throw Error("response lost after POST");
      if (c.body.kind === "close") closed = true;
      return { id: c.body.operationId };
    }
    if (c.path.includes("/operations/")) {
      const body = commands.get(c.path.split("/").at(-1));
      assert(body, "Only saved operations may be queried");
      return {
        command: {
          operation_id: body.operationId,
          session_id: SID,
          generation: 1,
          kind: body.kind,
          state: "completed",
          receipt: {
            status: "completed",
            result:
              body.kind === "prompt"
                ? { stopReason: "cancelled" }
                : { cancelSent: true, execution: "unknown" },
          },
        },
      };
    }
    return session({ state: closed ? "closed" : "open" });
  });
  h.runner.session = session();
  h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
  await assert.rejects(
    h.runner.command("prompt", { text: "READY" }),
    /response lost/,
  );
  const original = h.runner.pending.body.operationId;
  assert.equal(original, h.runner.state.operations[0].body.operationId);
  await h.runner.cleanup();
  assert.deepEqual(
    [...commands.values()].map((c) => c.kind),
    ["prompt", "cancel", "close"],
  );
  assert(h.calls.some((c) => c.path.endsWith("/operations/" + original)));
  assert.equal(h.runner.state.evidence.at(-1).status, "closed-and-released");
  const count = h.calls.length;
  await h.runner.cleanup();
  assert.equal(h.calls.length, count);
  assert.equal(h.runner.state.completed, false);
});
test("lost close POST cleanup inspects same close and never creates replacement", async () => {
  let body,
    closed = false;
  const h = harness((c) => {
    if (c.path.endsWith("/control"))
      return {
        lease: closed ? null : { id: LEASE, owned: true, generation: 1 },
      };
    if (c.path.endsWith("/permissions")) return [];
    if (c.path.endsWith("/commands")) {
      body = c.body;
      throw Error("close response lost");
    }
    if (c.path.includes("/operations/")) {
      assert(c.path.endsWith(body.operationId));
      closed = true;
      return {
        command: {
          operation_id: body.operationId,
          session_id: SID,
          generation: 1,
          kind: "close",
          state: "completed",
          receipt: {
            status: "completed",
            result: { cancelSent: true, execution: "unknown" },
          },
        },
      };
    }
    return session({ state: closed ? "closed" : "open" });
  });
  h.runner.session = session();
  h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
  await assert.rejects(h.runner.command("close"), /close response lost/);
  await h.runner.cleanup();
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  assert.equal(h.runner.state.evidence.at(-1).status, "closed-and-released");
});

// A stateful HTTP boundary fixture: actual Runner methods remain untouched.
// No live runtime is claimed. Responses use the audited hub field names and
// independent queued/accepted/completed states, not a stubbed phase result.
function phaseServer({
  configurationNotice = "Router refused changed session configuration (SESSION_CONFIGURATION_CHANGED). Explicit reconciliation is required. No provider or model fallback was used.",
  clocksWithin5s = true,
  approvedRevision = 1,
  renewal = null,
  preloadRenewal = true,
  holderId = a.HOLDER,
  journalId = a.HOLDER,
  predecessorEvidence = {},
} = {}) {
  const projection =
    renewal &&
    a.renewedCredentials(renewal.inventory, renewal.details, POST_EXPIRY);
  const profiles = projection ? projection.profiles : a.PROFILES;
  let clock = renewal ? POST_EXPIRY : NOW,
    trajectorySince = clock,
    serial = 100,
    sequence = 0;
  const sessions = new Map(),
    launches = [],
    commands = new Map(),
    permissions = new Map();
  const calls = [],
    saves = [],
    checkpoints = [],
    attempts = [],
    consumerReads = [],
    effects = new Map();
  function consumerRows() {
    return [...sessions.values()].map((s) => {
      const pp = profiles[s.provider];
      return {
        id: s.id,
        binding_id: pp.binding,
        credential_id: pp.credential,
        generation: pp.generation,
        kind: "holder-session",
        host_id: HOST,
        execution_identity: "c4",
        state: s.state === "closed" ? "released" : "active",
        failure_code: null,
        created_at: date(clock),
        last_used_at: date(clock),
        lease_expires_at: date(clock + 60000),
        ended_at: s.state === "closed" ? date(clock) : null,
        retire_after: null,
        unobserved: false,
      };
    });
  }
  const id = () =>
    `00000000-0000-4000-8000-${String(++serial).padStart(12, "0")}`;
  const definitions = JSON.parse(
    fs.readFileSync(path.join(__dirname, "launch-definitions.json"), "utf8"),
  );
  for (const d of definitions)
    Object.assign(d.session.communication, {
      directWake: true,
      broadcastWake: false,
      maxWakesPerHour: 10,
      maxCausalHops: 4,
    });
  const profile = (sid) => profiles[sessions.get(sid).provider];
  function observeTrajectory(provider) {
    const pp = profiles[provider],
      rows = attempts.filter((r) => r.route === "canary-pi-" + provider);
    clock += 10;
    return {
      version: 1,
      source: a.ROUTER,
      origin: "http://127.0.0.1:8787",
      scope: {
        namespace: "default",
        principal: "key:" + pp.key,
        route: "canary-pi-" + provider,
        since: trajectorySince,
      },
      requestedAt: clock,
      observedAt: clock + 1,
      fetchedRows: rows.length,
      rows: clone(rows),
      totalAttempts: rows.length,
    };
  }
  function observeMarker(provider, runId, phase) {
    const count = effects.get(runId) || 0;
    return {
      version: 1,
      provider,
      profileId: profiles[provider].id,
      runId,
      phase,
      state: "observed",
      phaseChecksPass: true,
      observedUtc: date(clock),
      commands: {
        denied: a.markerCommand(provider, runId, "denied"),
        effects: a.markerCommand(provider, runId, "effects"),
      },
      files: {
        denied: { state: "absent" },
        effects: count
          ? {
              state: "present",
              bytes: 88,
              exactlyOneMarker: count === 1,
              markerOccurrences: count,
              sha256: "a".repeat(64),
            }
          : { state: "absent" },
      },
    };
  }
  function message(s, text) {
    s.events.push({
      sequence: String(++sequence),
      createdAt: date(clock),
      event: { kind: "update", type: "agent_message_chunk", text },
    });
  }
  function finish(c) {
    const s = sessions.get(c.session_id);
    c.state = "completed";
    c.receipt = { status: "completed", result: {} };
    if (c.kind === "prompt") {
      c.receipt.result.stopReason = c.cancelled
        ? "cancelled"
        : c.refusal
          ? "refusal"
          : "end_turn";
      if (c.permission) {
        const q = permissions.get(c.permission);
        s.events.push({
          sequence: String(++sequence),
          createdAt: date(clock),
          event: {
            kind: "update",
            type: "tool_call_update",
            title: JSON.parse(q.context.description).command,
            ...(s.provider === "codex" ? {} : { toolCallId: c.nativeToolId }),
            status: q.decision.optionId === "yes" ? "completed" : "failed",
          },
        });
      }
      message(s, c.response);
    } else if (c.kind === "close") {
      s.state = "closed";
      s.lease = null;
      c.receipt.result = { cancelSent: true, execution: "unknown" };
    } else if (c.kind === "cancel") {
      const prompt = [...commands.values()].find(
        (x) =>
          x.session_id === c.session_id &&
          x.kind === "prompt" &&
          x.state === "accepted",
      );
      assert(prompt, "Cancel must target actual accepted work");
      prompt.cancelled = true;
      c.receipt.result = { cancelSent: true, execution: "unknown" };
    } else if (c.kind === "set-config") {
      s.effort = c.payload.value;
      s.revision++;
      c.receipt.result.settingsApplied = true;
    }
  }
  function publicSession(s) {
    return session({ id: s.id, state: s.state, control_epoch: s.epoch });
  }
  const runner = new a.Runner({
    ...predecessorEvidence,
    origin: a.ORIGIN,
    crypto: webcrypto,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    load: () => null,
    save: (state) => saves.push(clone(state)),
    ask: async (title, instructions, validate, _timeout, context) => {
      checkpoints.push(title);
      clock += 20;
      const boundary = runner.state.evidence
        .filter((e) => e.kind === "collection-boundary")
        .at(-1);
      if (boundary) clock = Math.max(clock, boundary.notBefore + 1);
      if (runner.lease && Date.parse(runner.lease.expires_at) - clock < 30000)
        await runner.renew();
      let value = true;
      if (context?.kind === "reconcile-choice") value = { reconcile: true };
      else if (context?.kind === "pre-launch")
        value = {
          trajectory: observeTrajectory(context.provider),
          marker: observeMarker(context.provider, context.runId, context.phase),
          coverageConfirmed: true,
          holderReadyConfirmed: true,
          routeConfirmed: true,
          clocksWithin5s,
        };
      else if (context?.kind === "post-open")
        value = {
          marker: observeMarker(context.provider, context.runId, context.phase),
          deliveryConfirmed: true,
        };
      else if (context?.kind === "observation") {
        const phase = context.phase;
        value = {
          trajectory: observeTrajectory(context.provider),
          coverageConfirmed: true,
          ...(phase
            ? {
                marker: observeMarker(context.provider, context.runId, phase),
              }
            : {}),
        };
      }
      return validate(value);
    },
    fetch: async (url, init) => {
      clock += 5;
      const u = new URL(url, a.ORIGIN),
        pathName = u.pathname,
        body = init.body && JSON.parse(init.body);
      calls.push({
        path: pathName,
        method: init.method,
        body: body && clone(body),
      });
      let result;
      if (pathName === "/api/router-credentials" && renewal) {
        result = {
          ...renewal.inventory,
          consumers: [...renewal.inventory.consumers, ...consumerRows()],
        };
        consumerReads.push({
          path: pathName,
          consumers: clone(result.consumers),
        });
      } else if (pathName === "/api/agent-profiles")
        result = definitions.map((d) => ({
          id: d.holder.id,
          revision: approvedRevision,
          enabled: true,
          definition: d,
        }));
      else if (
        init.method === "POST" &&
        (pathName === "/api/agent-launches" || pathName.endsWith("/resume"))
      ) {
        const previous = pathName.endsWith("/resume")
          ? sessions.get(pathName.split("/")[3])
          : null;
        if (previous) {
          assert.equal(previous.state, "closed");
          assert.equal(body.acknowledgeUnknownToolEffects, undefined);
        }
        const provider =
          previous?.provider ||
          Object.keys(profiles).find((k) => profiles[k].id === body.profileId);
        assert(provider);
        const pp = profiles[provider];
        const sid = id(),
          native = previous?.native || id();
        const s = {
          id: sid,
          provider,
          native,
          state: "open",
          epoch: 0,
          lease: null,
          events: [],
          effort: "off",
          revision: 1,
        };
        sessions.set(sid, s);
        const l = {
          id: id(),
          operation_id: body.operationId,
          profile_id: pp.id,
          profile_revision: 1,
          cwd: "/home/c4/.local/state/relay-holder/workspaces/" + pp.id,
          state: "pending",
          session_id: sid,
          holder_id: holderId,
          holder_journal_id: journalId,
          native_recovery_version: 2,
          expires_at: date(clock + 120000),
          resume_from: previous?.id || null,
          receipt: null,
          polls: 0,
        };
        launches.unshift(l);
        result = l;
      } else if (pathName === "/api/agent-launches") {
        const l = launches[0];
        if (++l.polls >= 2) {
          l.state = "open";
          l.receipt = {
            status: "completed",
            result: {
              sessionId: l.session_id,
              nativeSessionId: sessions.get(l.session_id).native,
              nativeRecoveryVersion: 2,
            },
          };
        } else l.state = "offered";
        result = launches;
      } else if (pathName.startsWith("/api/router-credentials/keys/")) {
        const pp = Object.values(profiles).find(
          (x) => x.credential === pathName.split("/").at(-1),
        );
        assert(pp);
        result = renewal
          ? Object.values(renewal.details).find(
              (v) => v.credential.id === pp.credential,
            )
          : {
              credential: {
                id: pp.credential,
                bindingId: pp.binding,
                routerKeyId: pp.key,
                namespace: "default",
                state: "ready",
              },
              binding: { currentCredentialId: pp.credential },
            };
        if (renewal) {
          result = {
            ...result,
            consumers: [
              ...result.consumers,
              ...consumerRows()
                .filter((c) => c.credential_id === pp.credential)
                .map((c) => ({
                  id: c.id,
                  kind: c.kind,
                  generation: c.generation,
                  state: c.state,
                  failureCode: c.failure_code,
                  createdAt: c.created_at,
                  lastUsedAt: c.last_used_at,
                  leaseExpiresAt: c.lease_expires_at,
                  endedAt: c.ended_at,
                })),
            ],
          };
          consumerReads.push({
            path: pathName,
            consumers: clone(result.consumers),
          });
        }
      } else if (pathName.startsWith("/api/agent-permissions/")) {
        const req = permissions.get(pathName.split("/")[3]);
        assert(req);
        if (init.method === "POST") {
          assert.equal(req.state, "pending");
          req.state = "decided";
          req.delivery = "pending";
          req.decision = body.outcome;
          req.polls = 0;
          result = {
            commandId: id(),
            requestId: req.id,
            delivery: "pending",
            execution: "unknown",
          };
        } else {
          if (req.state === "decided" && ++req.polls >= 2) {
            req.state = "resolved";
            req.delivery = "accepted";
            if (req.decision.optionId === "yes") {
              const runId = /acceptance-([a-f0-9-]+)\.effects/.exec(
                JSON.parse(req.context.description).command,
              )?.[1];
              assert(runId, "Allow must address effects marker only");
              effects.set(runId, (effects.get(runId) || 0) + 1);
            }
          }
          result = req;
        }
      } else {
        const sid = pathName.includes("/conversations/")
          ? pathName.split("/")[4]
          : pathName.split("/")[3];
        const s = sessions.get(sid);
        assert(s, "Unknown endpoint/session " + pathName);
        const pp = profile(sid),
          tail = pathName
            .split("/")
            .slice(pathName.includes("/conversations/") ? 5 : 4)
            .join("/");
        if (!tail) result = publicSession(s);
        else if (tail === "router")
          result = {
            sessionId: sid,
            generation: 1,
            connection: {
              enabled: true,
              ...(renewal ? { id: pp.connection } : {}),
            },
            binding: {
              id: pp.binding,
              enabled: true,
              state: "ready",
              ...(renewal
                ? { revision: pp.revision, generation: pp.generation }
                : {}),
            },
            credential: {
              id: pp.credential,
              state: "ready",
              ...(renewal
                ? {
                    generation: pp.generation,
                    expiresAt: date(pp.expiresAt),
                    remoteAcknowledged: true,
                    revokeRequested: false,
                  }
                : {}),
            },
            consumer: {
              credentialId: pp.credential,
              state: "active",
              credentialState: "ready",
              remoteAcknowledged: true,
              revokeRequested: false,
              credentialExpiresAt: date(renewal ? pp.expiresAt : a.EXPIRES),
              leaseExpiresAt: date(clock + 60000),
              ...(renewal
                ? { generation: pp.generation, failureCode: null }
                : {}),
            },
          };
        else if (tail === "communication")
          result = { sessionId: sid, status: "ready" };
        else if (tail === "control") {
          if (init.method === "POST") {
            assert.equal(body.generation, 1);
            s.lease = {
              id: body.leaseId || id(),
              generation: 1,
              expires_at: date(clock + 60000),
            };
            result = s.lease;
          } else result = { lease: s.lease && { ...s.lease, owned: true } };
        } else if (tail === "permissions")
          result = [...permissions.values()]
            .filter((q) => q.session_id === sid)
            .map((q) => ({
              id: q.id,
              state: q.state,
              delivery: q.delivery,
              execution: "unknown",
            }));
        else if (tail === "native-recovery") {
          assert.equal(s.state, "closed");
          result = {
            available: true,
            kind: "pi",
            profileName: "pi-canary-" + s.provider,
          };
        } else if (tail === "settings")
          result = {
            generation: 1,
            controlEpoch: s.epoch,
            snapshot: {
              version: 1,
              revision: s.revision,
              modes: null,
              configOptions: [
                {
                  id: "thought_level",
                  name: "Effort",
                  currentValue: s.effort,
                  options: ["off", "low", "high"].map((x) => ({
                    id: x,
                    name: x,
                  })),
                },
              ],
            },
          };
        else if (tail === "timeline") {
          const active = [...commands.values()].find(
            (c) => c.session_id === sid && c.counting && c.state === "accepted",
          );
          if (active && !active.streaming) {
            active.streaming = true;
            message(s, "1\n2\n");
          }
          result = {
            sessionId: sid,
            generation: 1,
            state: s.state,
            text: true,
            events: s.events,
            prompts: [...commands.values()]
              .filter((c) => c.session_id === sid && c.kind === "prompt")
              .map((c) => ({ id: c.id, state: c.state, text: c.payload.text })),
            nextAfter: null,
            nextPromptsAfter: null,
          };
        } else if (tail === "commands" && init.method === "POST") {
          assert.equal(body.leaseId, s.lease.id);
          assert.equal(body.generation, 1);
          const c = {
            id: id(),
            operation_id: body.operationId,
            session_id: sid,
            kind: body.kind,
            generation: 1,
            state: "pending",
            payload: body,
            polls: 0,
            receipt: null,
          };
          if (c.kind === "prompt") {
            const text = body.text;
            c.counting = text.includes("count upward");
            c.refusal = text.includes("EFFORT_HIGH_PROBE");
            const fixed = {
              "/relay-history": "Completed turns: aabbccdd, 1234abcd",
              "/relay-branch 1234abcd": "Independent branch selected.",
              "/relay-compact": "History compacted.",
              "/relay-reconcile confirmed": "Previous branch retired.",
            };
            if (Object.hasOwn(fixed, text)) c.response = fixed[text];
            else if (
              text.startsWith(
                "After any required scoped Relay message read, request permission",
              )
            ) {
              const cmd = text.split("COMMAND: ")[1];
              assert(cmd);
              const q = detail({
                id: id(),
                session_id: sid,
                expires_at: date(clock + 120000),
              });
              q.context.description = JSON.stringify({
                command: cmd,
                timeout: 30,
              });
              permissions.set(q.id, q);
              c.permission = q.id;
              c.nativeToolId = "native-bash-" + c.id;
              for (const event of [
                { type: "tool_call", title: "bash", status: "pending" },
                { type: "tool_call_update", title: cmd, status: "in_progress" },
              ])
                s.events.push({
                  sequence: String(++sequence),
                  createdAt: date(clock),
                  event: {
                    kind: "update",
                    ...event,
                    ...(s.provider === "codex"
                      ? {}
                      : { toolCallId: c.nativeToolId }),
                  },
                });
              c.response = "Decision handled.";
            } else if (c.counting) c.response = "";
            else {
              const expected =
                /^After any required scoped Relay message read, reply exactly ([A-Z_]+)\./.exec(
                  text,
                );
              assert(expected, "Unexpected prompt " + text);
              c.response = c.refusal ? configurationNotice : expected[1];
            }
            if (
              !c.refusal &&
              (!text.startsWith("/relay-") || text === "/relay-compact")
            )
              attempts.push({
                id: id(),
                requestId: id(),
                namespace: "default",
                principalId: "key:" + pp.key,
                route: "canary-pi-" + s.provider,
                startedAt: clock,
                endedAt: clock + 1,
                incomplete: false,
                status: "complete",
                attempts: 1,
                configRevision: 2882,
              });
          } else if (["cancel", "close"].includes(c.kind)) s.epoch++;
          else assert.equal(c.kind, "set-config");
          commands.set(body.operationId, c);
          result = c;
        } else if (tail.startsWith("operations/")) {
          const c = commands.get(tail.slice(11));
          assert(c);
          if (c.state !== "completed") {
            if (++c.polls === 1) c.state = "accepted";
            else if (
              (!c.permission ||
                permissions.get(c.permission).state === "resolved") &&
              (!c.counting || c.cancelled)
            )
              finish(c);
          }
          result = { command: c };
        } else
          throw Error("Unimplemented endpoint " + pathName + " " + init.method);
      }
      // Browser receives serialized values; no aliasing mutable server state.
      const raw = JSON.stringify(result);
      return {
        ok: true,
        status: init.method === "POST" ? 202 : 200,
        headers: { get: () => "application/json" },
        text: async () => raw,
      };
    },
  });
  if (projection && preloadRenewal) {
    runner.profiles = projection.profiles;
    runner.effectiveExpiry = projection.expiresAt;
    runner.state.successorCredentials = projection;
  }
  return {
    runner,
    calls,
    sessions,
    launches,
    commands,
    permissions,
    checkpoints,
    effects,
    attempts,
    consumerReads,
    saves,
    advance: (ms) => {
      clock += ms;
    },
    setTrajectorySince: (since) => {
      trajectorySince = since;
    },
  };
}

function publicReplacementHarness({
  mutateHandoff,
  expireDuringHandoff = false,
  refuseDelivery = false,
  predecessorEvidence = {},
  driftPredecessor,
} = {}) {
  const renewal = renewedFixture("ready");
  const f = phaseServer({
    renewal,
    preloadRenewal: false,
    // Independently supplied server identities, never read back from Runner.
    // A new root can legitimately have equal incarnation and journal UUIDs.
    holderId: SID,
    journalId: SID,
    predecessorEvidence,
  });
  const raw = JSON.stringify(stoppedReport(), null, 2);
  const prior = a.stoppedPredecessor(raw);
  const trace = [],
    entry = {};
  const fetch = f.runner.env.fetch,
    ask = f.runner.env.ask;
  f.runner.env.load = () => raw;
  f.runner.env.fetch = async (url, init) => {
    const c = {
      path: new URL(url, a.ORIGIN).pathname,
      method: init.method,
      body: init.body && JSON.parse(init.body),
    };
    trace.push({ kind: "request", ...c });
    if (
      c.path.startsWith("/api/agent-sessions/" + a.STOPPED_SESSION) ||
      c.path.startsWith("/api/agent-permissions/" + a.STOPPED_PERMISSION)
    ) {
      f.calls.push(c);
      f.advance(5);
      const result = predecessorResponse(prior, c);
      if (driftPredecessor) driftPredecessor(result, c);
      return {
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        text: async () => JSON.stringify(result),
      };
    }
    return fetch(url, init);
  };
  f.runner.env.ask = async (
    title,
    instructions,
    validate,
    timeout,
    context,
  ) => {
    const accept = (value) => {
      const result = validate(value);
      trace.push({ kind: "checkpoint-accepted", title });
      return result;
    };
    let value;
    if (context?.kind === "renewal-deployment") {
      assert.equal(f.runner.state.successorCredentials, undefined);
      assert(timeout <= 120000);
      value = {
        confirmMetadataReview: true,
        reportDownloaded: true,
        operatorVerified: true,
        deploymentVerified: true,
        observedUtc: date(f.runner.now()),
        hubSource: a.RUN_SOURCE,
      };
    } else if (context?.kind === "renewed-credentials") {
      value = {
        confirmRenewedCredentials: true,
        ...a.renewedCredentials(
          renewal.inventory,
          renewal.details,
          f.runner.now(),
        ),
      };
    } else if (context?.kind === "stopped-replacement") {
      f.advance(6000);
      const rows = ["338869f5-complete", "811cab9f-cancelled"].map((id, i) => ({
        ...trajectoryRow(),
        id,
        status: i ? "cancelled" : "complete",
        startedAt: NOW - 50 + i,
        endedAt: NOW - 20 + i,
      }));
      value = {
        confirmReplacement: true,
        reportDownloaded: true,
        operatorVerified: true,
        priorReportCanonicalSha256: f.runner.state.predecessor.canonicalSha256,
        windowStartUtc: date(f.runner.now()),
        windowEndUtc: date(f.runner.now() + 900000),
        trajectory: trajectory(f.runner.now(), rows),
        marker: { ...marker(), observedUtc: date(f.runner.now()) },
      };
      entry.window = {
        start: Date.parse(value.windowStartUtc),
        deadline: Date.parse(value.windowEndUtc),
      };
    } else if (context?.kind === "preserving-holder") {
      f.advance(expireDuringHandoff ? 900001 : 20);
      value = {
        holderId: SID,
        journalId: SID,
        oldHolderId: prior.launch.holderId,
        oldJournalId: prior.launch.journalId,
        preparationId: OP,
        stateDirectory: "/home/c4/.local/state/relay-holder-handoff-" + OP,
        handoffReceipt: {
          path:
            "/home/c4/.config/relay-holder/used-handoffs/" +
            OP +
            "/complete.json",
          sha256: "c".repeat(64),
        },
        observedUtc: date(f.runner.now()),
        qualifiedRelease: PUBLISHED_HOLDER_TEST_PINS.release,
        holderSource: PUBLISHED_HOLDER_TEST_PINS.source,
        holderBinarySha256: PUBLISHED_HOLDER_TEST_PINS.binarySha256,
        profilesSha256: holderInputs.relay_holder_profiles_sha256,
        nativeRecovery: 2,
        nativeStateReady: true,
        activeSessions: 0,
        pendingCommands: 0,
        admissionPaused: false,
        preservedOldStateConfirmed: true,
        operatorVerified: true,
      };
      if (mutateHandoff) mutateHandoff(value, f);
      entry.handoff = clone(value);
      // The fresh principal/window starts after the old accounting observation.
      f.setTrajectorySince(f.runner.now());
    } else {
      return ask(
        title,
        instructions,
        (result) =>
          accept(
            refuseDelivery && title.startsWith("Open complete:")
              ? { ...result, deliveryConfirmed: false }
              : result,
          ),
        timeout,
        context,
      );
    }
    f.checkpoints.push(title);
    return accept(value);
  };
  assert.equal(Object.getPrototypeOf(f.runner), a.Runner.prototype);
  assert.equal(f.runner.state.predecessor, undefined);
  assert.equal(f.runner.state.replacement, undefined);
  assert.equal(f.runner.state.successorCredentials, undefined);
  return { ...f, raw, prior, trace, entry };
}

test("actual public replacement uses published holder pins through all phases without resetting history", async () => {
  const f = publicReplacementHarness();
  await f.runner.replaceStopped();
  assert.equal(f.runner.state.status, "phase-a-passed");
  assert.equal(f.runner.state.phaseACompleted, true);
  assert.equal(f.runner.state.completed, false);
  assert.equal(f.runner.state.predecessor.raw, f.raw);
  assert.equal(
    f.calls.filter((c) =>
      c.path.startsWith(
        "/api/agent-sessions/" + a.STOPPED_SESSION + "/operations/",
      ),
    ).length,
    3,
  );
  assert(
    !f.runner.state.evidence.some(
      (e) => e.kind === "operator-predecessor-projection",
    ),
  );
  assert.equal(
    f.runner.state.predecessor.canonicalSha256,
    await a.digest(webcrypto, JSON.parse(f.raw)),
  );
  assert(f.saves.length > 0);
  assert(f.saves.every((s) => s.predecessor.raw === f.raw));
  const replacement = f.runner.state.replacement;
  assert.equal(replacement.carriedPrompts, 1);
  assert.equal(replacement.carriedAttempts, 2);
  assert.equal(replacement.originalDeadline, f.prior.run.deadline);
  assert.equal(replacement.start, f.entry.window.start);
  assert.equal(replacement.deadline, f.entry.window.deadline);
  assert.equal(f.runner.state.runs[0].deadline, f.entry.window.deadline);
  assert(replacement.deadline <= f.runner.effectiveExpiry);
  const handoffs = f.runner.state.evidence.filter(
    (e) => e.kind === "operator-holder-handoff",
  );
  assert.equal(handoffs.length, 1);
  assert.equal(f.entry.handoff.operatorVerified, true);
  // The validator requires this input attestation but saves only its documented
  // projection and provenance; it does not copy arbitrary input fields.
  for (const key of [
    "holderId",
    "journalId",
    "oldHolderId",
    "oldJournalId",
    "preparationId",
    "stateDirectory",
    "handoffReceipt",
    "observedUtc",
    "qualifiedRelease",
    "holderSource",
    "profilesSha256",
    "holderBinarySha256",
    "nativeRecovery",
    "nativeStateReady",
    "activeSessions",
    "pendingCommands",
    "admissionPaused",
    "preservedOldStateConfirmed",
  ])
    assert.deepEqual(handoffs[0][key], f.entry.handoff[key]);
  assert.equal(handoffs[0].operatorVerified, undefined);
  assert.equal(
    handoffs[0].provenance,
    "operator-reported installed hashes, receipt and fresh hello; not a browser host measurement or a new acceptance window",
  );
  assert.equal(f.runner.holderId, SID);
  assert.equal(f.runner.journalId, SID);
  assert.equal(f.launches.length, 5);
  assert.equal(f.launches.filter((l) => l.resume_from).length, 2);
  assert(
    f.launches.every((l) => l.holder_id === SID && l.holder_journal_id === SID),
  );
  assert.deepEqual(
    f.runner.state.runs.map((r) => [r.provider, r.effort, r.prompts]),
    [
      ["codex", false, 12],
      ["qwen", false, 11],
      ["codex", true, 4],
    ],
  );
  assert.equal(f.runner.state.runs[0].carriedAttempts, 2);
  assert(
    f.runner.state.runs.every(
      (r) => r.status === "passed-with-live-wire-unobserved",
    ),
  );
  assert(
    f.runner.state.runs[0].firstSnapshot.scope.since >
      replacement.priorObservation.observedAt,
  );
  assert.equal(replacement.priorObservation.scope.principal, "key:" + p.key);
  assert.equal(
    f.runner.state.runs[0].firstSnapshot.scope.principal,
    "key:" + f.runner.profiles.codex.key,
  );
  const deliveries = f.runner.state.evidence.filter(
    (e) => e.kind === "delivery",
  );
  assert(deliveries.length >= 5);
  assert(deliveries.every((e) => e.credentialGeneration === 2));
  assert(
    [...f.sessions.values()].every(
      (s) => s.state === "closed" && s.lease === null,
    ),
  );
  assert.equal(f.effects.get(a.PROFILES.codex.run), 1);
  assert.equal(f.effects.get(a.PROFILES.qwen.run), 1);
  const handoffAt = f.trace.findIndex(
    (e) => e.title === "Fresh preserving-holder handoff",
  );
  const launchAt = f.trace.findIndex(
    (e) => e.method === "POST" && e.path === "/api/agent-launches",
  );
  const deliveryAt = f.trace.findIndex(
    (e) => e.title === "Open complete: verify delivery before prompting",
  );
  const promptAt = f.trace.findIndex(
    (e) => e.method === "POST" && e.body?.kind === "prompt",
  );
  assert(
    handoffAt >= 0 &&
      handoffAt < launchAt &&
      launchAt < deliveryAt &&
      deliveryAt < promptAt,
  );
});

test("actual public replacement supplies immutable checkpoint context without parsing names or instructions", async () => {
  const f = publicReplacementHarness(),
    ask = f.runner.env.ask,
    contexts = [];
  const frozen = (v) => {
    if (v && typeof v === "object") {
      assert(Object.isFrozen(v));
      Object.values(v).forEach(frozen);
    }
  };
  f.runner.env.ask = async (
    _title,
    _instructions,
    validate,
    timeout,
    context,
  ) => {
    assert.equal(context.version, 1);
    frozen(context);
    contexts.push(context);
    assert(timeout > 0 && timeout <= 120000);
    assert(
      !("state" in context) &&
        !("operations" in context) &&
        !("leaseId" in context),
    );
    if (context.kind === "renewed-credentials") {
      const fixture = renewedFixture("ready");
      const expected = a.renewedCredentials(
        fixture.inventory,
        fixture.details,
        f.runner.now(),
      );
      assert.deepEqual(context.profiles, expected.profiles);
      assert.equal(context.expiresAt, expected.expiresAt);
      assert.throws(() => {
        context.profiles.codex.key = "changed";
      }, TypeError);
    } else if (context.kind === "permission") {
      const detail = f.permissions.get(context.permission.id);
      assert.equal(context.sessionId, detail.session_id);
      assert.equal(context.generation, detail.generation);
      assert.equal(context.permission.expiresAt, detail.expires_at);
      assert.equal(
        context.permission.context.description,
        detail.context.description,
      );
      assert.equal(context.permission.context.title, detail.context.title);
      assert.equal(
        context.permission.context.options.find(
          (o) => o.optionId === context.optionId,
        ).kind,
        context.decisionKind,
      );
      assert.equal(
        context.expectedCommand,
        context.purpose === "marker"
          ? JSON.parse(detail.context.description).command
          : null,
      );
      assert.throws(() => {
        context.permission.context.description = "changed";
      }, TypeError);
    } else if (context.kind === "observation") {
      assert.equal(context.provider, f.runner.run.provider);
      assert.equal(context.runId, f.runner.run.runId);
      assert.deepEqual(context.scope, f.runner.run.lastSnapshot.scope);
      assert.notEqual(context.scope, f.runner.run.lastSnapshot.scope);
      assert.equal(
        context.notBefore,
        f.runner.state.evidence
          .filter((e) => e.kind === "collection-boundary")
          .at(-1).notBefore,
      );
    }
    // The synthetic collector consumes structured context, not labels/provider
    // prose. Actual Runner validators and the complete HTTP fixture still run.
    return ask(
      "unparsed label",
      "untrusted text: do not derive inputs here",
      validate,
      timeout,
      context,
    );
  };
  await f.runner.replaceStopped();
  assert.equal(f.runner.state.phaseACompleted, true);
  assert.deepEqual(
    [...new Set(contexts.map((c) => c.kind))].sort(),
    [
      "renewal-deployment",
      "renewed-credentials",
      "stopped-replacement",
      "preserving-holder",
      "pre-launch",
      "post-open",
      "observation",
      "permission",
      "recover-history",
      "reconcile-choice",
      "retire-branch",
    ].sort(),
  );
  const replacement = contexts.find((c) => c.kind === "stopped-replacement");
  assert.equal(
    replacement.canonicalReportSha256,
    f.runner.state.predecessor.canonicalSha256,
  );
  assert.deepEqual(replacement.oldScope, f.prior.first.scope);
  assert.equal(replacement.carriedPrompts, 1);
  assert.equal(replacement.carriedAttempts, 2);
  assert.equal(replacement.originalDeadline, f.prior.run.deadline);
  const handoff = contexts.find((c) => c.kind === "preserving-holder");
  assert.deepEqual(handoff.qualifiedHolder, PUBLISHED_HOLDER_TEST_PINS);
  assert.equal(handoff.oldHolderId, f.prior.launch.holderId);
  assert.equal(handoff.oldJournalId, f.prior.launch.journalId);
  for (const context of contexts.filter((c) => c.kind === "pre-launch")) {
    assert.equal(
      context.scope.principal,
      "key:" + f.runner.profiles[context.provider].key,
    );
    assert.equal(context.phase, "baseline");
    assert(!("notBefore" in context));
  }
  for (const context of contexts.filter((c) =>
    ["recover-history", "reconcile-choice", "retire-branch"].includes(c.kind),
  )) {
    assert(f.sessions.has(context.sessionId));
    assert.equal(context.generation, 1);
    assert.equal(context.holderId, SID);
    assert.equal(context.journalId, SID);
    assert(!("notBefore" in context));
  }
});

function predecessorProjectionFixture({
  mutate,
  mutatePin,
  rawSuffix = "",
} = {}) {
  const prior = a.stoppedPredecessor(JSON.stringify(stoppedReport()));
  const value = {
    kind: "relay-predecessor-command-projection",
    version: 1,
    source: a.RUN_SOURCE,
    schemaVersion: 93,
    observedUtc: date(POST_EXPIRY),
    tenantId: OTHER,
    databaseIdentity: "synthetic-relay",
    querySha256: "b".repeat(64),
    sessionId: a.STOPPED_SESSION,
    generation: 1,
    priorReportCanonicalSha256: createHash("sha256")
      .update(a.canonical(prior.report))
      .digest("hex"),
    commands: prior.commands.map((op, i) => ({
      ...predecessorResponse(prior, {
        method: "GET",
        path:
          "/api/agent-sessions/" +
          a.STOPPED_SESSION +
          "/operations/" +
          op.body.operationId,
      }).command,
      id: [OP, REQUEST, HOST][i],
    })),
  };
  // This trusted fixture pin is separate from the returned document. Mutations
  // below retain its original scope, and rehash only to reach semantic guards.
  const pin = {
    sha256: "",
    tenantId: value.tenantId,
    databaseIdentity: value.databaseIdentity,
    querySha256: value.querySha256,
    commands: value.commands.map((row) => ({
      operationId: row.operation_id,
      commandId: row.id,
      kind: row.kind,
    })),
  };
  if (mutate) mutate(value);
  const raw = JSON.stringify(value, null, 2);
  pin.sha256 = createHash("sha256").update(raw).digest("hex");
  if (mutatePin) mutatePin(pin);
  const contexts = [];
  return {
    value,
    pin,
    raw,
    contexts,
    env: {
      predecessorEvidencePin: pin,
      readPredecessorEvidence: async (context) => {
        contexts.push(context);
        return raw + rawSuffix;
      },
    },
  };
}

test("actual public replacement uses only pinned predecessor command projection and retains live GETs and all phases", async () => {
  const projection = predecessorProjectionFixture();
  const expectedPin = clone(projection.pin);
  const f = publicReplacementHarness({ predecessorEvidence: projection.env });
  // Pin and reader are captured once, not replaced by later environment edits.
  projection.pin.sha256 = "f".repeat(64);
  projection.pin.commands[0].commandId = LEASE;
  f.runner.env.readPredecessorEvidence = () => {
    throw Error("Changed reader must not be used");
  };
  await f.runner.replaceStopped();
  assert.equal(f.runner.state.status, "phase-a-passed");
  assert.equal(projection.contexts.length, 1);
  const context = projection.contexts[0];
  assert(Object.isFrozen(context));
  assert(Object.isFrozen(context.commands));
  assert(context.commands.every(Object.isFrozen));
  assert(context.signal instanceof AbortSignal);
  assert.equal(context.source, a.RUN_SOURCE);
  assert.equal(context.schemaVersion, 93);
  assert.equal(context.sessionId, a.STOPPED_SESSION);
  assert.equal(context.generation, 1);
  assert(
    context.deadline > POST_EXPIRY && context.deadline <= POST_EXPIRY + 30000,
  );
  assert.equal(
    context.priorReportCanonicalSha256,
    f.runner.state.predecessor.canonicalSha256,
  );
  assert.deepEqual(
    context.commands,
    f.prior.commands.map((op) => ({
      operationId: op.body.operationId,
      kind: op.body.kind,
    })),
  );
  const oldReads = f.calls.filter(
    (c) =>
      c.path.includes(a.STOPPED_SESSION) ||
      c.path.includes(a.STOPPED_PERMISSION),
  );
  assert.deepEqual(
    oldReads.map((c) => c.path),
    [
      "/api/agent-sessions/" + a.STOPPED_SESSION,
      "/api/agent-sessions/" + a.STOPPED_SESSION + "/control",
      "/api/agent-permissions/" + a.STOPPED_PERMISSION,
    ],
  );
  assert(oldReads.every((c) => c.method === "GET"));
  const evidence = f.runner.state.evidence.filter(
    (e) => e.kind === "operator-predecessor-projection",
  );
  assert.equal(evidence.length, 1);
  assert.equal(evidence[0].sha256, expectedPin.sha256);
  assert.equal(evidence[0].querySha256, expectedPin.querySha256);
  assert.equal(evidence[0].databaseIdentity, expectedPin.databaseIdentity);
  assert.equal(evidence[0].tenantId, expectedPin.tenantId);
  assert.equal(evidence[0].hubSource, a.RUN_SOURCE);
  assert.deepEqual(evidence[0].commands, expectedPin.commands);
  assert.match(evidence[0].source, /operator-attested.*not independent/);
  const authorization = f.runner.state.evidence.find(
    (e) => e.kind === "replacement-authorization",
  );
  assert.deepEqual(
    authorization.commands.map((c) => c.commandId),
    expectedPin.commands.map((c) => c.commandId),
  );
  assert.equal(f.runner.state.predecessor.raw, f.raw);
  assert(f.saves.every((s) => s.predecessor.raw === f.raw));
  assert.equal(f.runner.state.replacement.carriedPrompts, 1);
  assert.equal(f.runner.state.replacement.carriedAttempts, 2);
  assert.equal(f.runner.state.runs[0].carriedAttempts, 2);
  assert.equal(f.launches.length, 5);
  assert.equal(f.runner.state.phaseACompleted, true);
  assert.equal(f.runner.state.completed, false);
});

test("actual public replacement refuses untrusted, stale or unsettled predecessor projections before any POST", async () => {
  const cases = [
    ["raw bytes changed", { rawSuffix: "\n" }, /hash changed/],
    [
      "wrong pin hash",
      {
        mutatePin: (v) => {
          v.sha256 = "c".repeat(64);
        },
      },
      /hash changed/,
    ],
    [
      "wrong source",
      {
        mutate: (v) => {
          v.source = a.SOURCE;
        },
      },
      /provenance/,
    ],
    [
      "wrong schema",
      {
        mutate: (v) => {
          v.schemaVersion = 92;
        },
      },
      /provenance/,
    ],
    [
      "stale",
      {
        mutate: (v) => {
          v.observedUtc = date(POST_EXPIRY - 120000);
        },
      },
      /freshness/,
    ],
    [
      "future",
      {
        mutate: (v) => {
          v.observedUtc = date(POST_EXPIRY + 60000);
        },
      },
      /freshness/,
    ],
    [
      "wrong tenant",
      {
        mutate: (v) => {
          v.tenantId = SID;
        },
      },
      /provenance/,
    ],
    [
      "wrong database",
      {
        mutate: (v) => {
          v.databaseIdentity = "other-relay";
        },
      },
      /provenance/,
    ],
    [
      "wrong query",
      {
        mutate: (v) => {
          v.querySha256 = "e".repeat(64);
        },
      },
      /provenance/,
    ],
    [
      "wrong report",
      {
        mutate: (v) => {
          v.priorReportCanonicalSha256 = "e".repeat(64);
        },
      },
      /scope/,
    ],
    [
      "wrong session",
      {
        mutate: (v) => {
          v.sessionId = SID;
        },
      },
      /scope/,
    ],
    [
      "wrong generation",
      {
        mutate: (v) => {
          v.generation = 2;
        },
      },
      /scope/,
    ],
    [
      "extra field",
      {
        mutate: (v) => {
          v.httpResponse = {};
        },
      },
      /provenance/,
    ],
    [
      "missing command",
      {
        mutate: (v) => {
          v.commands.pop();
        },
      },
      /scope/,
    ],
    [
      "extra command",
      {
        mutate: (v) => {
          v.commands.push(clone(v.commands[0]));
        },
      },
      /scope/,
    ],
    [
      "duplicate operation",
      {
        mutate: (v) => {
          v.commands[1] = clone(v.commands[0]);
        },
      },
      /command set/,
    ],
    [
      "wrong command ID",
      {
        mutate: (v) => {
          v.commands[0].id = LEASE;
        },
      },
      /command identity/,
    ],
    [
      "duplicate pin command",
      {
        mutatePin: (v) => {
          v.commands[1].commandId = v.commands[0].commandId;
        },
      },
      /pinned.*scope/,
    ],
    [
      "wrong pin operation",
      {
        mutatePin: (v) => {
          v.commands[0].operationId = OP;
        },
      },
      /pinned.*scope/,
    ],
    [
      "wrong row session",
      {
        mutate: (v) => {
          v.commands[0].session_id = SID;
        },
      },
      /Operation identity/,
    ],
    [
      "wrong row generation",
      {
        mutate: (v) => {
          v.commands[0].generation = 2;
        },
      },
      /Operation identity/,
    ],
    [
      "wrong kind",
      {
        mutate: (v) => {
          v.commands[0].kind = "cancel";
        },
      },
      /Operation identity/,
    ],
    [
      "unknown command",
      {
        mutate: (v) => {
          v.commands[0].state = "unknown";
        },
      },
      /Operation unknown/,
    ],
    [
      "pending command",
      {
        mutate: (v) => {
          v.commands[0].state = "pending";
        },
      },
      /settled native/,
    ],
    [
      "missing receipt",
      {
        mutate: (v) => {
          v.commands[0].receipt = null;
        },
      },
      /receipt shape/,
    ],
    [
      "unsettled receipt",
      {
        mutate: (v) => {
          v.commands[0].receipt.status = "accepted";
        },
      },
      /Missing native receipt/,
    ],
    [
      "unknown prompt result",
      {
        mutate: (v) => {
          v.commands[0].receipt.result.stopReason = "unknown";
        },
      },
      /settled native/,
    ],
    [
      "unsent cancel",
      {
        mutate: (v) => {
          v.commands[1].receipt.result.cancelSent = false;
        },
      },
      /settled native/,
    ],
    [
      "unsent close",
      {
        mutate: (v) => {
          v.commands[2].receipt.result.cancelSent = false;
        },
      },
      /settled native/,
    ],
    [
      "raw result payload",
      {
        mutate: (v) => {
          v.commands[0].receipt.result.text = "not permitted";
        },
      },
      /receipt shape/,
    ],
  ];
  for (const [name, options, expected] of cases) {
    const projection = predecessorProjectionFixture(options);
    const f = publicReplacementHarness({ predecessorEvidence: projection.env });
    await assert.rejects(f.runner.replaceStopped(), expected, name);
    assert.equal(f.calls.filter((c) => c.method === "POST").length, 0, name);
    assert(!f.calls.some((c) => c.path.includes("/operations/")), name);
    assert.equal(f.runner.state.status, "stopped", name);
    assert.equal(f.runner.state.predecessor.raw, f.raw, name);
    assert.equal(f.runner.state.replacement, undefined, name);
    assert(
      !f.runner.state.evidence.some(
        (e) => e.kind === "operator-predecessor-projection",
      ),
      name,
    );
  }
});

test("projection configuration and live predecessor settlement are independently required", async () => {
  for (const missing of ["predecessorEvidencePin", "readPredecessorEvidence"]) {
    const projection = predecessorProjectionFixture();
    delete projection.env[missing];
    const f = publicReplacementHarness({ predecessorEvidence: projection.env });
    await assert.rejects(f.runner.replaceStopped(), /required together/);
    assert.equal(projection.contexts.length, 0);
    assert.equal(f.calls.filter((c) => c.method === "POST").length, 0);
  }
  for (const field of ["session", "control", "permission"]) {
    const projection = predecessorProjectionFixture();
    const f = publicReplacementHarness({
      predecessorEvidence: projection.env,
      driftPredecessor: (row, c) => {
        if (field === "session" && c.path.endsWith(a.STOPPED_SESSION))
          row.state = "open";
        if (field === "control" && c.path.endsWith("/control"))
          row.lease = { id: LEASE };
        if (field === "permission" && c.path.endsWith(a.STOPPED_PERMISSION))
          row.state = "pending";
      },
    });
    await assert.rejects(
      f.runner.replaceStopped(),
      /Prior session|Prior permission/,
    );
    assert.equal(f.calls.filter((c) => c.method === "POST").length, 0);
    assert.equal(f.runner.state.replacement, undefined);
  }
});

test("projection never bypasses missing original report, Stop or its read deadline", async () => {
  for (const mode of [
    "missing-report",
    "stopped",
    "deadline",
    "invalid-json",
  ]) {
    const projection = predecessorProjectionFixture();
    let f;
    const read = projection.env.readPredecessorEvidence;
    if (mode === "invalid-json") {
      projection.env.readPredecessorEvidence = async () => "{";
      projection.pin.sha256 = createHash("sha256").update("{").digest("hex");
    } else
      projection.env.readPredecessorEvidence = async (context) => {
        if (mode === "stopped") f.runner.stopped = true;
        if (mode === "deadline") f.advance(15001);
        return read(context);
      };
    f = publicReplacementHarness({ predecessorEvidence: projection.env });
    if (mode === "missing-report") f.runner.env.load = () => null;
    await assert.rejects(
      f.runner.replaceStopped(),
      /Original stopped report|Stopped|deadline reached|Invalid predecessor projection JSON/,
    );
    assert.equal(f.calls.filter((c) => c.method === "POST").length, 0, mode);
    assert.equal(f.runner.state.replacement, undefined, mode);
  }
});

test("actual public replacement refuses changed handoff pins, identity, provenance and expired window before dispatch", async () => {
  for (const [name, options, expected] of [
    [
      "old holder release",
      {
        mutateHandoff: (v) => {
          v.qualifiedRelease = "v0.13.1";
        },
      },
      /handoff missing or changed/,
    ],
    [
      "wrong source",
      {
        mutateHandoff: (v) => {
          v.holderSource = "a".repeat(40);
        },
      },
      /handoff missing or changed/,
    ],
    [
      "wrong binary",
      {
        mutateHandoff: (v) => {
          v.holderBinarySha256 = "b".repeat(64);
        },
      },
      /handoff missing or changed/,
    ],
    [
      "old incarnation",
      {
        mutateHandoff: (v) => {
          v.holderId = v.oldHolderId;
        },
      },
      /handoff missing or changed/,
    ],
    [
      "old journal",
      {
        mutateHandoff: (v) => {
          v.journalId = v.oldJournalId;
        },
      },
      /handoff missing or changed/,
    ],
    [
      "different preparation receipt",
      {
        mutateHandoff: (v) => {
          v.handoffReceipt.path = v.handoffReceipt.path.replace(OP, REQUEST);
        },
      },
      /handoff missing or changed/,
    ],
    [
      "stale observation",
      {
        mutateHandoff: (v, f) => {
          v.observedUtc = date(f.runner.now() - 120000);
        },
      },
      /handoff missing or changed/,
    ],
    [
      "unready native state",
      {
        mutateHandoff: (v) => {
          v.nativeStateReady = false;
        },
      },
      /handoff missing or changed/,
    ],
    [
      "expired owner window",
      { expireDuringHandoff: true },
      /Replacement window expired/,
    ],
  ]) {
    const f = publicReplacementHarness(options);
    await assert.rejects(f.runner.replaceStopped(), expected, name);
    assert(f.calls.length > 0, name);
    assert(
      f.calls.every((c) => c.method === "GET"),
      name,
    );
    assert.equal(f.launches.length, 0, name);
    assert.equal(f.attempts.length, 0, name);
    assert.equal(f.runner.state.predecessor.raw, f.raw, name);
    assert.equal(
      f.runner.state.predecessor.canonicalSha256,
      await a.digest(webcrypto, JSON.parse(f.raw)),
      name,
    );
    assert(
      f.saves.every((s) => s.predecessor.raw === f.raw),
      name,
    );
    assert.equal(f.runner.state.replacement.carriedPrompts, 1, name);
    assert.equal(f.runner.state.replacement.carriedAttempts, 2, name);
    assert.equal(
      f.runner.state.replacement.deadline,
      f.entry.window.deadline,
      name,
    );
    assert.equal(f.runner.state.status, "stopped", name);
    assert.equal(f.runner.state.completed, false, name);
  }
});

test("actual public replacement never prompts when the post-Open delivery handoff is refused", async () => {
  const f = publicReplacementHarness({ refuseDelivery: true });
  await assert.rejects(f.runner.replaceStopped(), /Delivery handoff missing/);
  assert.equal(f.launches.length, 1);
  assert.equal(f.attempts.length, 0);
  assert(
    !f.calls.some(
      (c) =>
        c.method === "POST" &&
        (c.body?.kind === "prompt" || c.path.includes("/agent-permissions/")),
    ),
  );
  assert(
    [...f.sessions.values()].every(
      (s) => s.state === "closed" && s.lease === null,
    ),
  );
  assert.equal(f.runner.state.predecessor.raw, f.raw);
  assert.equal(f.runner.state.replacement.carriedPrompts, 1);
  assert.equal(f.runner.state.replacement.carriedAttempts, 2);
  assert.equal(f.runner.state.replacement.deadline, f.entry.window.deadline);
  assert.equal(f.runner.state.status, "stopped");
});

test("renewed generation two runs all three phases with fresh principals and historical budget carried", async () => {
  const renewal = renewedFixture("ready"),
    f = phaseServer({ renewal });
  f.runner.state.predecessor = { raw: JSON.stringify(stoppedReport()) };
  f.runner.state.replacement = {
    deadline: POST_EXPIRY + 900000,
    carriedPrompts: 1,
    carriedAttempts: 2,
    priorObservation: trajectory(POST_EXPIRY - 100),
    marker: marker(),
  };
  await f.runner.phase("codex");
  await f.runner.phase("qwen");
  await f.runner.phase("codex", true);
  assert.deepEqual(
    f.runner.state.runs.map((r) => [r.provider, r.effort, r.prompts]),
    [
      ["codex", false, 12],
      ["qwen", false, 11],
      ["codex", true, 4],
    ],
  );
  assert.equal(f.runner.state.runs[0].carriedAttempts, 2);
  assert.equal(f.launches.length, 5);
  assert.equal(f.launches.filter((l) => l.resume_from).length, 2);
  assert(
    f.consumerReads.some((r) => r.consumers.some((c) => c.state === "active")),
  );
  const refreshes = f.consumerReads.filter(
    (r) => r.path === "/api/router-credentials",
  );
  assert(refreshes.some((r) => r.consumers.length > 0));
  assert(
    refreshes.every((r) => r.consumers.every((c) => c.state === "released")),
  );
  assert.equal(f.effects.get(a.PROFILES.codex.run), 1);
  assert.equal(f.effects.get(a.PROFILES.qwen.run), 1);
  for (const run of f.runner.state.runs) {
    assert.equal(run.status, "passed-with-live-wire-unobserved");
    assert.equal(
      run.firstSnapshot.scope.principal,
      "key:" + f.runner.profiles[run.provider].key,
    );
  }
  assert(
    f.runner.state.evidence
      .filter((e) => e.kind === "delivery")
      .every((e) => e.credentialGeneration === 2),
  );
  assert(
    f.calls.filter((c) => c.path === "/api/router-credentials").length >= 5,
  );
  assert(
    [...f.sessions.values()].every(
      (s) => s.state === "closed" && s.lease === null,
    ),
  );
  assert.equal(f.runner.state.completed, false);
});

test("renewed delivery refuses a stale consumer generation before a prompt", async () => {
  const f = phaseServer({ renewal: renewedFixture("ready") });
  const fetch = f.runner.env.fetch;
  f.runner.env.fetch = async (url, init) => {
    const response = await fetch(url, init);
    if (!url.endsWith("/router")) return response;
    const value = JSON.parse(await response.text());
    value.consumer.generation = 1;
    return { ...response, text: async () => JSON.stringify(value) };
  };
  await assert.rejects(
    f.runner.phase("codex"),
    /Credential\/principal changed/,
  );
  assert.equal(f.calls.filter((c) => c.body?.kind === "prompt").length, 0);
});

test("renewed refresh refuses owned-open, unrelated and wrong-generation active consumers", async () => {
  for (const variant of [
    "owned-open",
    "unrelated",
    "wrong-generation",
    "detail-only",
  ]) {
    const renewal = renewedFixture("ready");
    const f = phaseServer({ renewal });
    await f.runner.phase("codex");
    const old = [...f.sessions.values()][0];
    assert.equal(old.state, "closed");
    assert(f.launches.some((l) => l.resume_from === old.id));
    const pp = f.runner.profiles.codex;
    const consumer = {
      id: OTHER,
      binding_id: pp.binding,
      credential_id: pp.credential,
      generation: variant === "wrong-generation" ? 1 : pp.generation,
      kind: "holder-session",
      state: "active",
    };
    if (variant === "owned-open") old.state = "open";
    else if (variant === "detail-only")
      renewal.details.codex.consumers.push(consumer);
    else renewal.inventory.consumers.push(consumer);
    const before = f.calls.length;
    await assert.rejects(
      f.runner.refreshRenewedCredentials(),
      /still active|detail changed or is incomplete/,
      variant,
    );
    assert(f.calls.slice(before).every((c) => c.method === "GET"));
    assert.equal(
      f.launches.length,
      2,
      "No extra recovery or launch on refusal",
    );
  }
});

test("full lifecycle phase crosses queued hub contracts through recovery without hidden mutations", async () => {
  for (const provider of ["codex", "qwen"]) {
    const f = phaseServer();
    await f.runner.phase(provider);
    const writes = f.calls.filter((c) => c.method !== "GET");
    const prompts = writes.filter((c) => c.body?.kind === "prompt");
    assert.equal(prompts.length, 11);
    assert.deepEqual(
      prompts.map((c) =>
        c.body.text.startsWith("/relay-")
          ? c.body.text
          : c.body.text.split(".")[0],
      ),
      [
        "After any required scoped Relay message read, reply exactly READY",
        "After any required scoped Relay message read, request permission to run exactly the following shell command once",
        "After any required scoped Relay message read, request permission to run exactly the following shell command once",
        "/relay-history",
        "/relay-branch 1234abcd",
        "After any required scoped Relay message read, reply exactly BRANCH_OK",
        "/relay-compact",
        "After any required scoped Relay message read, reply exactly COMPACT_OK",
        "After any required scoped Relay message read, count upward from 1, one integer per line, until 500",
        "/relay-reconcile confirmed",
        "After any required scoped Relay message read, reply exactly RECOVERED_OK",
      ],
    );
    assert.deepEqual(
      writes
        .filter((c) => c.path.endsWith("/decision"))
        .map((c) => c.body.outcome.optionId),
      ["no", "yes"],
    );
    assert.equal(
      writes.filter((c) => c.path === "/api/agent-launches").length,
      1,
    );
    assert.equal(writes.filter((c) => c.path.endsWith("/resume")).length, 1);
    assert.equal(writes.filter((c) => c.body?.kind === "close").length, 2);
    assert.equal(writes.filter((c) => c.body?.kind === "cancel").length, 1);
    assert.equal(
      writes.filter((c) => !(c.path.endsWith("/control") && c.body.leaseId))
        .length,
      20,
    );
    assert.equal(
      writes.filter((c) => c.path.endsWith("/control") && c.body.leaseId)
        .length,
      2,
    );
    assert.equal(
      new Set(
        writes.filter((c) => c.body.operationId).map((c) => c.body.operationId),
      ).size,
      18,
    );
    assert.equal(f.effects.get(a.PROFILES[provider].run), 1);
    assert(
      [...f.sessions.values()].every(
        (s) => s.state === "closed" && s.lease === null,
      ),
    );
    assert.equal(
      new Set([...f.sessions.values()].map((s) => s.native)).size,
      1,
    );
    assert.equal(
      f.runner.state.runs[0].status,
      "passed-with-live-wire-unobserved",
    );
    assert.equal(f.runner.state.completed, false); // one phase is not full plan completion
    assert(f.attempts.length <= 20);
    assert.equal(f.runner.session, null);
  }
});

test("full effort phase uses fresh session low/high refusal and explicit reconstruction within four prompts", async () => {
  const f = phaseServer();
  await f.runner.phase("codex", true);
  const writes = f.calls.filter((c) => c.method !== "GET");
  assert.deepEqual(
    writes
      .filter((c) => c.body?.kind === "set-config")
      .map((c) => c.body.value),
    ["low", "high"],
  );
  const texts = writes
    .filter((c) => c.body?.kind === "prompt")
    .map((c) => c.body.text);
  assert.equal(texts.length, 4);
  assert(texts[0].includes("EFFORT_LOW_OK"));
  assert(texts[1].includes("EFFORT_HIGH_PROBE"));
  assert.equal(texts[2], "/relay-reconcile confirmed");
  assert(texts[3].includes("EFFORT_HIGH_OK"));
  assert.equal(f.attempts.length, 2);
  assert.equal(f.permissions.size, 0);
  assert.equal(f.effects.size, 0);
  assert.equal(
    writes.filter((c) => !(c.path.endsWith("/control") && c.body.leaseId))
      .length,
    9,
  );
  assert.equal(
    writes.filter((c) => c.path.endsWith("/control") && c.body.leaseId).length,
    1,
  );
  assert.equal(f.launches.length, 1);
  assert.equal(
    f.runner.state.runs[0].status,
    "passed-with-live-wire-unobserved",
  );
});
test("generic backend failure and refusal alone cannot pass effort configuration fence", async () => {
  for (const configurationNotice of [
    "Model backend unavailable. No provider or model fallback was used.",
    "",
  ]) {
    const f = phaseServer({ configurationNotice });
    await assert.rejects(
      f.runner.phase("codex", true),
      /refusal|configuration|notice/i,
    );
    const prompts = f.calls.filter((c) => c.body?.kind === "prompt");
    assert.equal(prompts.length, 2); // low completion + probe; no reconciliation/high continuation
    assert(!prompts.some((c) => c.body.text === "/relay-reconcile confirmed"));
    assert.equal(f.runner.state.runs[0].status, undefined);
    assert.equal(f.runner.state.completed, false);
  }
});
test("lost lease acquisition is persisted, then observed without adopting foreign or owned leases", async () => {
  for (const owned of [true, false, null]) {
    let posted = false;
    const h = harness((c, _r, saves) => {
      if (!c.path.endsWith("/control")) return session();
      if (c.method === "POST") {
        assert.equal(saves.at(-1).controlAcquisition.status, "sending");
        assert.deepEqual(saves.at(-1).controlAcquisition.body, {
          generation: 1,
        });
        posted = true;
        throw Error("lease response lost");
      }
      return {
        lease:
          !posted || owned === null
            ? null
            : {
                id: LEASE,
                generation: 1,
                expires_at: date(NOW + 60000),
                owned,
              },
      };
    });
    h.runner.session = session();
    await assert.rejects(h.runner.acquire(), /response lost/);
    assert.equal(h.runner.lease, null);
    assert.equal(h.runner.state.controlAcquisition.status, "uncertain");
    await h.runner.cleanup();
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
    assert(!h.calls.some((c) => c.method === "DELETE"));
    assert.equal(
      h.runner.state.controlAcquisition.observation.status,
      owned === null ? "no-lease-observed" : "lease-observed",
    );
  }
});
test("fresh increasing snapshots collected before action cannot prove its zero delta", async () => {
  const h = harness();
  const before = trajectory(NOW - 2000),
    freshButBeforeAction = trajectory(NOW - 1000);
  h.runner.run = {
    provider: "codex",
    effort: false,
    deadline: NOW + 60000,
    firstSnapshot: before,
    lastSnapshot: before,
  };
  h.runner.env.ask = async (_t, _i, validate) =>
    validate({ coverageConfirmed: true, trajectory: freshButBeforeAction });
  await assert.rejects(
    h.runner.evidence("after action", null, true),
    /boundary|checkpoint|after|predate/i,
  );
  assert.deepEqual(h.runner.run.lastSnapshot, before);
  assert.equal(h.calls.length, 0);
});
test("unverified observer clock bound stops phase before the first launch", async () => {
  const f = phaseServer({ clocksWithin5s: false });
  await assert.rejects(f.runner.phase("codex"), /handoff|clock/i);
  assert(!f.calls.some((c) => c.method !== "GET"));
});

function finalRevocationServer(options = {}) {
  const renewed =
    options.renewal &&
    a.renewedCredentials(
      options.renewal.inventory,
      options.renewal.details,
      POST_EXPIRY,
    );
  const profiles = renewed ? renewed.profiles : a.PROFILES;
  const reviewRevision = renewed ? renewed.profiles.codex.revision : 3;
  let serial = 500;
  const uid = () =>
    `00000000-0000-4000-8000-${String(++serial).padStart(12, "0")}`;
  const rows = Object.entries(profiles).map(([provider, pp]) => ({
    provider,
    pp,
    binding: {
      id: pp.binding,
      host_id: HOST,
      execution_identity: "c4",
      revision: reviewRevision,
      generation: 2,
      enabled: true,
      state: "ready",
      current_credential_id: pp.credential,
      pending_credential_id: null,
      ...(renewed
        ? {
            canRenew: false,
            connection_id: pp.connection,
            grant_spec: options.renewal.inventory.bindings.find(
              (b) => b.id === pp.binding,
            ).grant_spec,
          }
        : {}),
    },
    credentials: [
      {
        id: uid(),
        binding_id: pp.binding,
        generation: 1,
        router_key_id: "key_0123456789abcdef",
      },
      {
        id: pp.credential,
        binding_id: pp.binding,
        generation: 2,
        router_key_id: pp.key,
      },
    ],
    op: null,
  }));
  const h = harness((c, _r, saves) => {
    if (c.path === "/api/router-credentials") {
      const v = {
        bindings: rows.map((r) => r.binding),
        credentials: rows.flatMap((r) => r.credentials),
        consumers: [],
      };
      if (options.truncated) v.credentials = Array(1000).fill(v.credentials[0]);
      if (options.gap)
        v.credentials = v.credentials.filter((x) => x.generation !== 1);
      if (options.activeConsumer)
        v.consumers.push({ binding_id: p.binding, state: "active" });
      return v;
    }
    if (c.method === "POST") {
      const row = rows.find(
        (r) => c.path === "/api/router-credentials/" + r.pp.binding + "/revoke",
      );
      assert(row);
      assert.equal(c.body.revision, reviewRevision);
      const saved = saves.at(-1).finalRevocation.intents.at(-1);
      assert.deepEqual(saved.body, c.body);
      assert.equal(saved.status, "sending");
      if (!options.uncommitted) row.op = clone(c.body);
      if (options.lost) throw Error("POST response lost");
      return { bindings: [], credentials: [], consumers: [] }; //202 inventory alone proves nothing
    }
    const row = rows.find((r) =>
      r.credentials.some((x) => c.path.endsWith("/" + x.id)),
    );
    assert(row, "Unexpected revocation endpoint");
    const cred = row.credentials.find((x) => c.path.endsWith("/" + x.id));
    const revoked = !!row.op;
    return {
      credential: {
        id: cred.id,
        bindingId: row.pp.binding,
        generation: cred.generation,
        routerKeyId: cred.router_key_id,
        state: revoked ? "revoked" : "ready",
        revokeRequested: revoked,
        cleanupPending: options.escrowPending && revoked ? true : false,
        failureCode: null,
      },
      binding: {
        id: row.pp.binding,
        revision: revoked
          ? options.changedRevision
            ? 6
            : reviewRevision + 1
          : reviewRevision,
        generation: 2,
        enabled: !revoked,
        state: revoked ? "revoked" : "ready",
        currentCredentialId: revoked ? null : row.pp.credential,
        pendingCredentialId: null,
      },
      operations:
        revoked && cred.generation === 1
          ? [
              {
                id: options.wrongOperation ? OTHER : row.op.operationId,
                kind: "revoke-binding",
                state: "revoked",
                failureCode: null,
              },
            ]
          : [],
      consumers: [],
    };
  });
  const r = h.runner;
  if (renewed) {
    h.advance(POST_EXPIRY - NOW);
    r.profiles = renewed.profiles;
    r.effectiveExpiry = renewed.expiresAt;
    r.state.successorCredentials = renewed;
  }
  r.state.status = "phase-a-passed";
  r.state.runs = ["codex", "qwen", "codex"].map((provider, i) => ({
    provider,
    effort: i === 2,
    status: "passed-with-live-wire-unobserved",
  }));
  const launches = ["codex", "codex", "qwen", "qwen", "codex"].map(
    (provider) => ({
      kind: "launch",
      at: date(NOW - 60000),
      id: uid(),
      sessionId: uid(),
      profileId: a.PROFILES[provider].id,
    }),
  );
  r.state.evidence = clone(launches);
  if (options.predecessor) {
    r.state.predecessor = { raw: JSON.stringify(options.predecessor) };
    if (!options.omitPredecessorCleanup)
      launches.push(a.stoppedPredecessor(r.state.predecessor.raw).launch);
  }
  for (const provider of ["codex", "qwen"])
    r.state.evidence.push({
      kind: "operator-observation",
      marker: {
        ...marker("recovered"),
        provider,
        runId: a.PROFILES[provider].run,
      },
    });
  let reviews = 0,
    rendererChecks = 0;
  r.env.ask = async (title, _text, validate) => {
    if (title.startsWith("Review final")) {
      reviews++;
      if (options.decline) return validate(false);
      const targets = rows.map((row) => ({
        provider: row.provider,
        bindingId: row.pp.binding,
        revision: reviewRevision,
        generations: row.credentials.map((c) => ({
          id: c.id,
          generation: c.generation,
          routerKeyId: c.router_key_id,
        })),
      }));
      const result = validate({
        confirmRevokeAllGenerations: true,
        noFurtherUse: true,
        targets,
      });
      if (options.changeDuringReview) rows[0].binding.revision++;
      return result;
    }
    rendererChecks++;
    h.advance(6000);
    const records = launches.map((l) => {
      const row = rows.find((x) => x.pp.id === l.profileId);
      return {
        launchId: l.id,
        sessionId: l.sessionId,
        bindingId: row.pp.binding,
        credentialId:
          l.sessionId === a.STOPPED_SESSION
            ? a.PROFILES.codex.credential
            : row.pp.credential,
        generation: l.sessionId === a.STOPPED_SESSION ? 1 : 2,
        commandId: uid(),
        cleanedAt: date(
          options.staleRenderer
            ? NOW - 120000
            : options.historicalRenderer
              ? NOW - 30000
              : r.now(),
        ),
        status: options.notCurrent ? "not-current" : "absent",
        fileAbsent: !options.filePresent,
      };
    });
    if (options.missingRenderer) records.pop();
    const markers = ["codex", "qwen"].map((provider) => {
      const pp = a.PROFILES[provider],
        m = marker("recovered");
      if (options.changedMarkerHash) m.files.effects.sha256 = "b".repeat(64);
      if (options.changedMarkerBytes) m.files.effects.bytes++;
      return {
        ...m,
        provider,
        profileId: pp.id,
        runId: pp.run,
        observedUtc: date(r.now()),
        commands: {
          denied: a.markerCommand(provider, pp.run, "denied"),
          effects: a.markerCommand(provider, pp.run, "effects"),
        },
      };
    });
    return validate({
      operatorVerified: true,
      observedUtc: date(r.now()),
      launches: records,
      markers,
      unrelated: "do-not-save",
    });
  };
  return {
    ...h,
    rows,
    reviews: () => reviews,
    rendererChecks: () => rendererChecks,
  };
}
test("renewed final revocation uses the reviewed new keys and retains the predecessor sixth cleanup", async () => {
  const h = finalRevocationServer({
    renewal: renewedFixture("ready"),
    predecessor: stoppedReport(),
  });
  await h.runner.revokeFinal();
  assert.equal(h.runner.state.finalRevocation.status, "confirmed");
  assert.equal(h.rows.length, 2);
  for (const row of h.rows) {
    assert.notEqual(row.pp.credential, a.PROFILES[row.provider].credential);
    assert.notEqual(row.pp.key, a.PROFILES[row.provider].key);
    assert(
      h.calls.some(
        (c) => c.path === "/api/router-credentials/keys/" + row.pp.credential,
      ),
    );
  }
  assert.equal(h.runner.state.completed, false);
});

test("explicit final revocation covers both bindings/all generations and separate renderer proof", async () => {
  const h = finalRevocationServer();
  await h.runner.revokeFinal();
  const posts = h.calls.filter((c) => c.method === "POST");
  assert.equal(posts.length, 2);
  assert.equal(h.reviews(), 1);
  assert.equal(h.rendererChecks(), 1);
  assert.deepEqual(
    posts.map((c) => c.path),
    Object.values(a.PROFILES).map(
      (p) => "/api/router-credentials/" + p.binding + "/revoke",
    ),
  );
  assert.notEqual(posts[0].body.operationId, posts[1].body.operationId);
  assert(
    h.rows.every((row) =>
      h.calls.some((c) => c.path.endsWith(row.credentials[0].id)),
    ),
  );
  assert.equal(h.runner.state.finalRevocation.status, "confirmed");
  assert.equal(h.runner.state.completed, false);
  assert(!JSON.stringify(h.runner.state).includes("do-not-save"));
  const count = h.calls.length;
  await assert.rejects(h.runner.revokeFinal());
  assert.equal(h.calls.length, count);
});
test("lost revoke response inspects original oldest-generation operation without retransmission", async () => {
  const h = finalRevocationServer({ lost: true });
  await h.runner.revokeFinal();
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
  assert(h.runner.state.operations.every((op) => op.status === "uncertain"));
  assert.equal(h.runner.state.finalRevocation.status, "confirmed");
});
test("uncommitted or mismatched revoke stays unresolved without new operation or second binding", async () => {
  for (const options of [
    { lost: true, uncommitted: true },
    { wrongOperation: true },
    { escrowPending: true },
    { changedRevision: true },
  ]) {
    const h = finalRevocationServer(options);
    await assert.rejects(h.runner.revokeFinal(), /Revocation|revocation/);
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
    assert.equal(
      h.runner.state.finalRevocation.status,
      "unresolved-owner-inspection-required",
    );
    assert.equal(h.rendererChecks(), 0);
    assert.equal(h.runner.state.completed, false);
  }
});
test("revocation refuses incomplete generations, active consumers, changed review or absent consent", async () => {
  for (const options of [
    { truncated: true },
    { gap: true },
    { activeConsumer: true },
    { changeDuringReview: true },
    { decline: true },
  ]) {
    const h = finalRevocationServer(options);
    await assert.rejects(h.runner.revokeFinal());
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 0);
  }
});
test("router revocation cannot stand in for every renderer receipt and file absence", async () => {
  for (const options of [
    { missingRenderer: true },
    { notCurrent: true },
    { filePresent: true },
    { staleRenderer: true },
  ]) {
    const h = finalRevocationServer(options);
    await assert.rejects(h.runner.revokeFinal(), /renderer|Renderer/);
    assert(
      h.runner.state.finalRevocation.intents.every(
        (i) => i.status === "router-revoked-and-escrow-cleaned",
      ),
    );
    assert.equal(
      h.runner.state.finalRevocation.status,
      "unresolved-owner-inspection-required",
    );
    assert.equal(h.runner.state.completed, false);
  }
});
test("launch-bound historical cleanup with fresh absence and identical markers is accepted", async () => {
  const h = finalRevocationServer({ historicalRenderer: true });
  await h.runner.revokeFinal();
  assert.equal(h.runner.state.finalRevocation.status, "confirmed");
  const evidence = h.runner.state.evidence.find(
    (e) => e.kind === "final-renderer-cleanup",
  );
  assert(
    evidence.launches.every(
      (l) =>
        Date.parse(l.cleanedAt) < h.runner.state.finalRevocation.intents[0].at,
    ),
  );
  assert(
    Date.parse(evidence.observedUtc) >
      h.runner.state.finalRevocation.intents.at(-1).at,
  );
});
test("final cleanup refuses changed marker bytes or hashes despite one-marker shape", async () => {
  for (const options of [
    { changedMarkerHash: true },
    { changedMarkerBytes: true },
  ]) {
    const h = finalRevocationServer(options);
    await assert.rejects(
      h.runner.revokeFinal(),
      /marker bytes or hash changed/,
    );
    assert.equal(
      h.runner.state.finalRevocation.status,
      "unresolved-owner-inspection-required",
    );
  }
});
test("stopped or incomplete phases cannot trigger final grant revocation", async () => {
  for (const change of [
    (r) => {
      r.stopped = true;
    },
    (r) => {
      r.pending = {};
    },
    (r) => {
      r.state.runs.pop();
    },
    (r) => {
      r.state.runs[0].status = "failed";
    },
  ]) {
    const h = finalRevocationServer();
    change(h.runner);
    await assert.rejects(h.runner.revokeFinal());
    assert.equal(h.calls.length, 0);
  }
  const h = finalRevocationServer();
  await h.runner.cleanup();
  assert.equal(h.calls.length, 0);
  assert.equal(h.runner.state.finalRevocation, undefined);
});

test("pre-dispatch decline or active consumer permits a fresh exact revocation review", async () => {
  for (const flag of ["decline", "activeConsumer"]) {
    const options = { [flag]: true };
    const h = finalRevocationServer(options);
    await assert.rejects(h.runner.revokeFinal());
    assert.equal(
      h.runner.state.finalRevocation.status,
      "declined-before-dispatch",
    );
    assert.deepEqual(h.runner.state.finalRevocation.intents, []);
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 0);
    options[flag] = false;
    await h.runner.revokeFinal();
    assert.equal(h.runner.state.finalRevocation.status, "confirmed");
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
  }
});

test("cleanup records open session identity without taking or adopting control", async () => {
  const h = harness();
  h.runner.session = session();
  await h.runner.cleanup();
  assert.equal(h.calls.length, 0);
  assert.deepEqual(h.runner.state.evidence.at(-1), {
    at: date(NOW),
    kind: "cleanup",
    status: "open-session-without-lease; operator-close-required",
    sessionId: SID,
    generation: 1,
    launchId: null,
    operations: [],
  });
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

test("Stop waits for a dispatched launch response and records its late identity once", async () => {
  const arrived = deferred(),
    response = deferred();
  const h = harness(async (c) => {
    assert.equal(c.path, "/api/agent-launches");
    assert.equal(c.method, "POST");
    arrived.resolve();
    return response.promise;
  });
  h.runner.run = { provider: "codex", deadline: NOW + 120000 };
  const action = h.runner.runExclusive(() => h.runner.launch());
  const failed = assert.rejects(action, /Stopped/);
  await arrived.promise;
  const stop = h.runner.stop();
  assert.equal(h.runner.stop(), stop);
  assert.equal(h.runner.cleanupAttempted, undefined);
  response.resolve({ id: OTHER, expires_at: date(NOW + 120000) });
  await failed;
  await stop;
  assert.equal(h.calls.length, 1);
  const entries = h.runner.state.evidence.filter((e) => e.kind === "cleanup");
  assert.equal(entries.length, 1);
  assert.equal(entries[0].launchId, OTHER);
  assert.equal(
    entries[0].status,
    "uncertain-launch; operator-inspection-and-close-required",
  );
  assert.equal(
    entries[0].operations[0].operationId,
    h.calls[0].body.operationId,
  );
});

test("Stop waits for prompt POST before original-operation cancel and Close", async () => {
  const arrived = deferred(),
    response = deferred(),
    commands = new Map();
  let closed = false;
  const h = harness(async (c) => {
    if (c.path.endsWith("/control"))
      return {
        lease: closed ? null : { id: LEASE, owned: true, generation: 1 },
      };
    if (c.path.endsWith("/permissions")) return [];
    if (c.path.endsWith("/commands")) {
      commands.set(c.body.operationId, c.body);
      if (c.body.kind === "prompt") {
        arrived.resolve();
        return response.promise;
      }
      if (c.body.kind === "close") closed = true;
      return { id: c.body.operationId };
    }
    if (c.path.includes("/operations/")) {
      const body = commands.get(c.path.split("/").at(-1));
      assert(body);
      return {
        command: {
          operation_id: body.operationId,
          session_id: SID,
          generation: 1,
          kind: body.kind,
          state: "completed",
          receipt: {
            status: "completed",
            result:
              body.kind === "prompt"
                ? { stopReason: "cancelled" }
                : { cancelSent: true, execution: "unknown" },
          },
        },
      };
    }
    return session({ state: closed ? "closed" : "open" });
  });
  h.runner.session = session();
  h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
  const action = h.runner.runExclusive(() =>
    h.runner.command("prompt", { text: "READY" }),
  );
  const failed = assert.rejects(action, /lost response/);
  await arrived.promise;
  const stop = h.runner.stop();
  assert.equal(h.runner.cleanupAttempted, undefined);
  assert.deepEqual(
    [...commands.values()].map((c) => c.kind),
    ["prompt"],
  );
  response.reject(Error("lost response"));
  await failed;
  await stop;
  assert.deepEqual(
    [...commands.values()].map((c) => c.kind),
    ["prompt", "cancel", "close"],
  );
  assert.equal(h.runner.state.evidence.at(-1).status, "closed-and-released");
  assert.equal(
    h.runner.state.evidence.filter((e) => e.kind === "cleanup").length,
    1,
  );
});

test("Stop rejects a checkpoint then waits for the active action before cleanup", async () => {
  const checkpoint = deferred(),
    arrived = deferred();
  const h = harness();
  h.runner.env.ask = () => {
    arrived.resolve();
    return checkpoint.promise;
  };
  h.runner.env.cancelCheckpoint = () =>
    checkpoint.reject(Error("Owner stopped"));
  const action = h.runner.runExclusive(() =>
    h.runner.ask("owner review", "confirm"),
  );
  const failed = assert.rejects(action, /Owner stopped/);
  await arrived.promise;
  await h.runner.stop();
  await failed;
  assert.equal(h.runner.cleanupAttempted, true);
  assert.equal(h.calls.length, 0);
});

test("Stop settles a sent revocation and never sends the second binding", async () => {
  const h = finalRevocationServer();
  const arrived = deferred(),
    response = deferred();
  const fetch = h.runner.env.fetch;
  h.runner.env.fetch = async (path, init) => {
    const result = await fetch(path, init);
    if (init.method === "POST") {
      arrived.resolve();
      await response.promise;
    }
    return result;
  };
  const action = h.runner.revokeFinal();
  const failed = assert.rejects(action, /Stopped/);
  await arrived.promise;
  const intent = clone(h.runner.state.finalRevocation.intents[0]);
  const stop = h.runner.stop();
  assert.equal(h.runner.cleanupAttempted, undefined);
  response.resolve();
  await failed;
  await stop;
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  assert.equal(
    h.runner.state.finalRevocation.intents[0].status,
    "router-revoked-and-escrow-cleaned",
  );
  assert.deepEqual(h.runner.state.finalRevocation.intents[0].body, intent.body);
  assert.equal(
    h.runner.state.finalRevocation.status,
    "unresolved-owner-inspection-required",
  );
  assert.equal(h.rendererChecks(), 0);
  const count = h.calls.length;
  await assert.rejects(h.runner.revokeFinal());
  assert.equal(h.calls.length, count);
});

test("Stop after verified closure does not mislabel historical launches unresolved", async () => {
  const h = harness();
  h.runner.state.operations.push({
    path: "/api/agent-launches",
    body: { operationId: OP },
    status: "received",
  });
  h.runner.state.activeLaunch = { id: OTHER, operationId: OP };
  h.runner.record("launch", { id: OTHER, operationId: OP, sessionId: SID });
  h.runner.record("closed", { sessionId: SID });
  await h.runner.stop();
  assert.equal(h.calls.length, 0);
  assert.equal(
    h.runner.state.evidence.filter((e) => e.kind === "cleanup").length,
    0,
  );
});

test("Stop during control GET prevents acquisition intent and POST", async () => {
  const arrived = deferred(),
    response = deferred();
  const h = harness((c) => {
    assert.equal(c.method, "GET");
    if (c.path.endsWith("/control")) {
      arrived.resolve();
      return response.promise;
    }
    return session();
  });
  h.runner.session = session();
  const action = h.runner.runExclusive(() => h.runner.acquire());
  const failed = assert.rejects(action, /Stopped/);
  await arrived.promise;
  const stop = h.runner.stop();
  assert.equal(h.runner.cleanupAttempted, undefined);
  response.resolve({ lease: null });
  await failed;
  await stop;
  assert.equal(h.calls.filter((c) => c.method !== "GET").length, 0);
  assert.equal(h.runner.state.controlAcquisition, undefined);
  assert.equal(h.runner.state.operations.length, 0);
  assert.equal(h.runner.lease, null);
  assert.equal(
    h.runner.state.evidence.at(-1).status,
    "open-session-without-lease; operator-close-required",
  );
  assert.equal(h.runner.state.evidence.at(-1).sessionId, SID);
});

function cleanupServer({
  slow = false,
  lost = null,
  unreadable = false,
  permission = false,
} = {}) {
  const commands = new Map();
  let closed = false;
  const h = harness((c) => {
    if (c.path.endsWith("/control"))
      return {
        lease: closed ? null : { id: LEASE, owned: true, generation: 1 },
      };
    if (c.path.endsWith("/permissions"))
      return permission ? [{ id: REQUEST, state: "pending" }] : [];
    if (c.path.endsWith("/decision"))
      throw Error("private-token-do-not-export");
    if (c.path.endsWith("/commands")) {
      commands.set(c.body.operationId, c.body);
      if (c.body.kind === "close") closed = true;
      if (c.body.kind === lost) throw Error("private-token-do-not-export");
      return { id: c.body.operationId };
    }
    if (c.path.includes("/operations/")) {
      const body = commands.get(c.path.split("/").at(-1));
      assert(body);
      if (unreadable) throw Error("private-token-do-not-export");
      if (slow) h.advance(25000);
      return {
        command: {
          operation_id: body.operationId,
          session_id: SID,
          generation: 1,
          kind: body.kind,
          state: "completed",
          receipt: {
            status: "completed",
            result:
              body.kind === "prompt"
                ? { stopReason: "cancelled" }
                : { cancelSent: true, execution: "unknown" },
          },
        },
      };
    }
    return session({ state: closed ? "closed" : "open" });
  });
  h.runner.session = session();
  h.runner.lease = {
    id: LEASE,
    expires_at: date(NOW + (slow ? 40000 : 60000)),
  };
  if (!permission) {
    const body = {
      operationId: OP,
      leaseId: LEASE,
      generation: 1,
      kind: "prompt",
      text: "READY",
    };
    commands.set(OP, body);
    h.runner.pending = { body, sessionId: SID, deadline: NOW + 30000 };
  }
  return { ...h, commands };
}

test("slow cancel and prompt settlement refuse expired Close before any POST", async () => {
  const h = cleanupServer({ slow: true });
  await h.runner.cleanup();
  assert.deepEqual(
    h.calls.filter((c) => c.method === "POST").map((c) => c.body.kind),
    ["cancel"],
  );
  const report = h.runner.state.evidence.at(-1);
  assert.equal(report.step, "dispatch-close");
  assert.equal(report.reason, "Cleanup lease expired; no command dispatched");
  assert.equal(report.sessionId, SID);
  assert.equal(h.runner.state.operations.at(-1).status, "received");
  assert(!h.calls.some((c) => c.method === "DELETE"));
});

test("lost cleanup cancel or Close reads the same operation without resend", async () => {
  for (const kind of ["cancel", "close"]) {
    const h = cleanupServer({ lost: kind });
    await h.runner.cleanup();
    const posts = h.calls.filter((c) => c.method === "POST");
    assert.deepEqual(
      posts.map((c) => c.body.kind),
      ["cancel", "close"],
    );
    const item = h.runner.state.operations.find((o) => o.body.kind === kind);
    assert.equal(item.status, "uncertain");
    assert(
      h.calls.some((c) =>
        c.path.endsWith("/operations/" + item.body.operationId),
      ),
    );
    assert.equal(h.runner.state.evidence.at(-1).status, "closed-and-released");
    assert(!JSON.stringify(h.runner.state).includes("private-token"));
    const count = h.calls.length;
    await h.runner.cleanup();
    assert.equal(h.calls.length, count);
  }
});

test("failed cleanup readback reports original operation, sanitized reason and step", async () => {
  const h = cleanupServer({ lost: "cancel", unreadable: true });
  await h.runner.cleanup();
  const item = h.runner.state.operations.find((o) => o.body.kind === "cancel");
  assert.equal(item.status, "uncertain");
  const report = h.runner.state.evidence.at(-1);
  assert.equal(report.step, "inspect-cancel-receipt");
  assert.equal(report.operationId, item.body.operationId);
  assert.equal(report.reason, "Request or receipt inspection failed");
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  assert(!JSON.stringify(h.runner.state).includes("private-token"));
});

test("lost cleanup permission decision records uncertain without repeating it", async () => {
  const h = cleanupServer({ permission: true });
  await h.runner.cleanup();
  assert.equal(h.runner.state.operations[0].status, "uncertain");
  const report = h.runner.state.evidence.at(-1);
  assert.equal(report.step, "cancel-permission");
  assert.equal(
    report.operationId,
    h.runner.state.operations[0].body.operationId,
  );
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  assert(!JSON.stringify(h.runner.state).includes("private-token"));
});

test("first registration refuses a wrong approved revision before launch", async () => {
  for (const provider of ["codex", "qwen"]) {
    for (const approvedRevision of [2, 3]) {
      const f = phaseServer({ approvedRevision });
      await assert.rejects(f.runner.phase(provider), /Approved profile absent/);
      assert.equal(f.calls.length, 1);
      assert.equal(f.calls[0].method, "GET");
      assert.equal(f.launches.length, 0);
      assert.equal(f.runner.state.operations.length, 0);
      assert.equal(f.checkpoints.length, 0);
    }
  }
});
