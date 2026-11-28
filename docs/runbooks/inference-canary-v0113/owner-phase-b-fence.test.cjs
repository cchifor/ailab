"use strict";
const test = require("node:test"),
  assert = require("node:assert/strict");
const { createHash, randomUUID } = require("node:crypto");
const { Controller, drain, digest } = require("./owner-phase-b-fence.cjs");
const fixture = require("./owner-phase-b-fence-fixture.cjs");
const NOW = 1795810000000;
const KEY = "key_1234567890abcdef";
function setup({
  failSave = () => false,
  onCall = () => {},
  mutate,
  lose,
} = {}) {
  let now = NOW,
    fixtureClockStep = 0,
    attempts = 0,
    tokenUsageUnknown = 0,
    stopped = false;
  const state = { operations: [], phases: [] },
    saves = [];
  const backend = fixture.transport({
    now: () => (now += fixtureClockStep),
    keyId: KEY,
    tenantId: "00000000-0000-4000-8000-000000000001",
    saves,
    usage: () => ({ attempts, tokenUsageUnknown }),
    mutate,
    lose,
    observeCall: (c) =>
      onCall(c, {
        expire: () => {
          now += 16000;
        },
        stop: () => {
          stopped = true;
        },
      }),
  });
  const controller = new Controller({
    transport: backend.port,
    source: fixture.SOURCE,
    now: () => now,
    randomUUID,
    state,
    deadline: now + 600000,
    save: () => {
      if (failSave(state)) throw Error("synthetic fsync failure");
      saves.push(structuredClone(state));
    },
    check: () => {
      if (stopped) throw Error("stopped");
    },
    scope: {
      keyId: KEY,
      principalId: "key:" + KEY,
      namespace: "default",
      tenantId: "00000000-0000-4000-8000-000000000001",
      holderId: "11111111-1111-4111-8111-111111111111",
      journalId: "22222222-2222-4222-8222-222222222222",
    },
    selection: {
      route: "canary-pi-claude",
      accountId: "claude",
      deploymentId: "claude-1",
      model: "sonnet",
      provider: "claude",
      providerGeneration: 7,
    },
  });
  const run = {
    runId: randomUUID(),
    effort: false,
    deadline: now + 300000,
    lastSnapshot: { totalAttempts: 0 },
  };
  return {
    controller,
    backend,
    state,
    saves,
    run,
    charge: (n, unknown = 0) => {
      attempts += n;
      tokenUsageUnknown += unknown;
    },
    terminal: () => ({
      observedAt: now,
      syntheticTerminalDigest: "1".repeat(64),
    }),
    advanceClock: (ms) => {
      now += ms;
    },
    advanceFixtureClockOnRead: () => {
      fixtureClockStep = 10;
    },
  };
}
for (const mutation of ["arm", "close"]) {
  test(`synthetic ${mutation} receipt uses one commit timestamp across clock reads`, async () => {
    const f = setup();
    await f.controller.initialize();
    if (mutation === "close") await f.controller.row(f.run, "ready", null);
    // Only the synthetic service clock advances; the actual Controller still
    // validates every byte of its receipt against the immutable prior state.
    f.advanceFixtureClockOnRead();
    if (mutation === "arm") await f.controller.row(f.run, "ready", null);
    else await f.controller.close(f.terminal(), true);
    const operation = f.state.operations.at(-1);
    assert.equal(operation.status, "committed");
    assert.equal(
      operation.receipt.fence[mutation === "arm" ? "createdAt" : "closedAt"],
      operation.receipt.committedAt,
    );
    assert(
      f.backend.calls.at(-1).request.notBefore < operation.receipt.committedAt,
      "the regression must cross clock ticks inside the synthetic mutation",
    );
    assert.equal(
      f.backend.calls.filter((call) => call.method === mutation).length,
      1,
    );
  });
}
test("HTTP receipt and recovered operation bind the original raw body, separately from the JWT assertion", async () => {
  const f = setup({ lose: (method) => method === "arm" });
  await f.controller.initialize();
  await f.controller.row(f.run, "ready", null);
  const intent = f.state.operations[0];
  const bodyHash = createHash("sha256").update(intent.raw).digest("hex");
  assert.equal(intent.receipt.requestDigest, bodyHash);
  assert.notEqual(intent.receipt.requestDigest, intent.requestDigest);
  const lookup = f.backend.calls.find((c) => c.method === "readOperation");
  assert(lookup);
  assert.notEqual(lookup.body.operationId, intent.receipt.operationId);
  assert.equal(lookup.body.targetOperationId, intent.receipt.operationId);
  assert.notEqual(lookup.request.bodySha256, intent.receipt.requestDigest);
  assert.equal(f.backend.calls.filter((c) => c.method === "arm").length, 1);
  assert.equal(intent.status, "committed");
});
test("a JWT assertion binding substituted into a mutation receipt is refused", async () => {
  const f = setup({
    mutate: (value, call) => {
      if (call.method === "arm") {
        value.response.requestDigest = call.request.requestDigest;
        value.projectionSha256 = digest(value.response);
      }
      return value;
    },
  });
  await f.controller.initialize();
  await assert.rejects(
    f.controller.row(f.run, "ready", null),
    /mutation receipt\/digest\/count differs/,
  );
  assert.equal(f.backend.calls.filter((c) => c.method === "arm").length, 1);
  assert.equal(f.backend.calls.filter((c) => c.method === "advance").length, 0);
});
test("fsync failure of exact mutation intent prevents any signer invocation", async () => {
  const f = setup({
    failSave: (state) => state.operations.at(-1)?.status === "sending",
  });
  await f.controller.initialize();
  await assert.rejects(f.controller.row(f.run, "ready", null), /fsync failure/);
  assert.equal(f.backend.calls.filter((c) => c.method === "arm").length, 0);
  assert.equal(f.state.phases.length, 0);
});
test("stop during committed signer work refuses all subsequent normal dispatch and reports unresolved", async () => {
  const f = setup({
    onCall: (c, control) => {
      if (c.method === "arm") control.stop();
    },
  });
  await f.controller.initialize();
  await assert.rejects(
    f.controller.row(f.run, "ready", null),
    /mutation unresolved/,
  );
  assert.equal(f.backend.calls.filter((c) => c.method === "arm").length, 1);
  assert.equal(
    f.backend.calls.filter((c) => c.method === "readOperation").length,
    0,
  );
  await assert.rejects(
    f.controller.row(f.run, "ready", null),
    /resolved operation/,
  );
  await assert.rejects(
    f.controller.close({ stopped: true }, true),
    /unresolved/,
  );
});
test("late port response cannot reset its deadline for an operation lookup or new row", async () => {
  const f = setup({
    onCall: (c, control) => {
      if (c.method === "arm") control.expire();
    },
  });
  await f.controller.initialize();
  await assert.rejects(
    f.controller.row(f.run, "ready", null),
    /mutation unresolved/,
  );
  assert.equal(
    f.backend.calls.filter((c) => c.method === "readOperation").length,
    0,
  );
  assert.equal(f.state.operations[0].status, "uncertain");
});
test("new admissions after terminal drain refuse advance and preserve old row", async () => {
  const f = setup();
  await f.controller.initialize();
  await f.controller.row(f.run, "ready", null);
  f.charge(1);
  await f.controller.settle(f.run, 1, f.terminal());
  f.run.lastSnapshot.totalAttempts = 1;
  f.charge(1);
  await assert.rejects(
    f.controller.row(f.run, "denied", f.terminal()),
    /changed after terminal drain/,
  );
  assert.equal(f.backend.calls.filter((c) => c.method === "advance").length, 0);
  assert.equal(f.backend.current().activeRow, 0);
});
test("phase expiry prevents normal fence calls but bounded irreversible cleanup remains possible", async () => {
  const f = setup();
  await f.controller.initialize();
  await f.controller.row(f.run, "ready", null);
  f.advanceClock(300001);
  const before = f.backend.calls.length;
  await assert.rejects(f.controller.inspect(), /deadline expired/);
  assert.equal(f.backend.calls.length, before);
  await f.controller.close({ reason: "expired", at: NOW + 300001 }, true);
  assert.equal(f.backend.current().closedAttempts, 0);
  assert(f.backend.calls.slice(before).every((c) => c.request.cleanup));
});
test("typed drain requires exact completed operation identities and source-bound scope", () => {
  const expected = {
    notBefore: NOW,
    scope: { sessionId: "source", routerSource: fixture.SOURCE },
    completedOperationIds: ["prompt", "cancel"],
    terminalPrompts: [{ operationId: "prompt", stopReason: "cancelled" }],
  };
  const valid = {
    version: 1,
    observedAt: NOW,
    scope: expected.scope,
    completedOperationIds: expected.completedOperationIds,
    terminalPrompts: expected.terminalPrompts,
    pendingPermissions: [],
    pendingTools: [],
    pendingControls: [],
    nativeRequests: [],
    clientRequests: [],
    provenance: {
      nativeSha256: "1".repeat(64),
      clientSha256: "2".repeat(64),
      operationsSha256: "3".repeat(64),
    },
  };
  assert.deepEqual(drain(valid, expected, NOW), valid);
  for (const change of [
    (v) => {
      v.observedAt--;
    },
    (v) => {
      v.completedOperationIds.pop();
    },
    (v) => {
      v.scope.sessionId = "other";
    },
    (v) => {
      v.clientRequests.push("in-flight");
    },
    (v) => {
      delete v.provenance.nativeSha256;
    },
    (v) => {
      v.pendingTools = null;
    },
  ]) {
    const value = structuredClone(valid);
    change(value);
    assert.throws(() => drain(value, expected, NOW), /Attempt fence:/);
  }
});
