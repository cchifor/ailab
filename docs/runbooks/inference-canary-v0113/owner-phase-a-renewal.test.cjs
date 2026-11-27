"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fixture = require("./owner-phase-a.hub-74d1.fixture.json");
const driver = require("./owner-phase-a.js");
const { createRenewalPrelude } = require("./owner-phase-a-renewal.cjs");
const clone = (v) => JSON.parse(JSON.stringify(v));

function harness({ initial, post, observe, persistFailure } = {}) {
  // The captured fixture remains historical. Transitions below are explicitly
  // synthetic, preserving its deployed snake_case inventory/detail schemas.
  let now = Date.parse("2026-11-27T04:04:00Z");
  const ready = clone(fixture.bootstrap);
  const inventory = clone(ready.inventory);
  inventory.credentials = inventory.credentials.filter(
    (c) => c.generation === 1,
  );
  for (const b of inventory.bindings)
    Object.assign(b, {
      revision: 1,
      generation: 1,
      state: "failed",
      canRenew: true,
      current_credential_id: null,
      pending_credential_id: null,
    });
  if (initial) initial(inventory);
  const saved = [],
    calls = [],
    controller = new AbortController();
  let reads = 0;
  const options = {
    now: () => now,
    observeDeployment: async () => ({
      hubSource: driver.RUN_SOURCE,
      ready: true,
      observedUtc: new Date(now).toISOString(),
    }),
    deadline: now + 120000,
    signal: controller.signal,
    sleep: async (ms) => {
      now += ms;
    },
    persist(value) {
      if (persistFailure) persistFailure(value);
      saved.push(clone(value));
    },
    async authenticatedFetch(path, init) {
      init.signal.throwIfAborted();
      const body = init.body && JSON.parse(init.body);
      const call = { path, method: init.method, body };
      calls.push(call);
      let value,
        status = 200;
      if (init.method === "POST") {
        const provider = Object.keys(driver.PROFILES).find(
          (p) =>
            path ===
            "/api/router-credentials/" + driver.PROFILES[p].binding + "/rotate",
        );
        assert(provider, "No non-rotation mutation permitted");
        const last = saved.at(-1).operations.at(-1);
        assert.equal(last.status, "sending");
        assert.equal(
          last.operationId,
          body.operationId,
          "Saved intent must precede transport",
        );
        assert.deepEqual(Object.keys(body).sort(), ["operationId", "revision"]);
        assert.equal(body.revision, 1);
        const b = inventory.bindings.find(
          (r) => r.id === driver.PROFILES[provider].binding,
        );
        Object.assign(
          b,
          clone(ready.inventory.bindings.find((r) => r.id === b.id)),
        );
        const credential = clone(
          ready.inventory.credentials.find(
            (r) => r.binding_id === b.id && r.generation === 2,
          ),
        );
        credential.creation_operation_id = body.operationId;
        inventory.credentials.push(credential);
        const detail = ready.details[provider];
        detail.credential.creationOperationId = body.operationId;
        detail.operations[0].id = body.operationId;
        if (post)
          await post({
            provider,
            body,
            inventory,
            credential,
            detail,
            calls,
            controller,
          });
        value = clone(inventory);
        status = 202;
      } else if (path === "/api/router-credentials") {
        reads++;
        if (observe) observe({ inventory, ready, calls, reads });
        value = clone(inventory);
      } else {
        const match = Object.values(ready.details).find(
          (d) => path === "/api/router-credentials/keys/" + d.credential.id,
        );
        assert(match, "Only selected newly issued credential may be inspected");
        value = clone(match);
      }
      return { ok: true, status, text: async () => JSON.stringify(value) };
    },
  };
  return {
    options,
    saved,
    calls,
    controller,
    create: () => createRenewalPrelude(options),
  };
}

test("two saved one-shot rotations require fresh acknowledged details and return actual driver projection", async () => {
  const h = harness(),
    prelude = h.create();
  const result = await prelude.run();
  assert.equal(result.status, "issued");
  assert.equal(result.operations.length, 2);
  assert(
    result.operations.every(
      (o) => o.status === "issued" && o.credentialState === "ready",
    ),
  );
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
  assert.equal(new Set(result.operations.map((o) => o.operationId)).size, 2);
  assert.equal(result.result.profiles.codex.generation, 2);
  assert.equal(result.result.profiles.qwen.generation, 2);
  assert.equal(
    result.result.expiresAt,
    Math.min(
      ...Object.values(fixture.bootstrap.details).map((d) =>
        Date.parse(d.credential.expiresAt),
      ),
    ),
  );
  await assert.rejects(prelude.run(), /single-use/);
  assert(
    !JSON.stringify(h.saved).includes("prefix"),
    "No unrelated inventory fields persisted",
  );
});

test("both bindings must be eligible before either rotation", async () => {
  for (const change of [
    (i) => {
      i.bindings[0].canRenew = false;
    },
    (i) => {
      i.bindings[0].grant_spec.routes.push("claude");
    },
    (i) => {
      i.credentials[0].cleanup_pending = true;
    },
    (i) => {
      i.consumers.push({
        binding_id: driver.PROFILES.qwen.binding,
        state: "active",
      });
    },
    (i) => {
      i.consumers.push({
        bindingId: driver.PROFILES.qwen.binding,
        state: "active",
      });
    },
  ]) {
    const h = harness({ initial: change });
    await assert.rejects(h.create().run(), /Renewal stopped/);
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 0);
  }
});

test("a failed durable intent prevents the first POST", async () => {
  const h = harness({
    persistFailure: (s) => {
      if (s.operations.length) throw new Error("disk full");
    },
  });
  await assert.rejects(h.create().run(), /disk full|Renewal stopped/);
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 0);
});

test("lost accepted response retains the exact uncertain operation and is never replayed", async () => {
  const h = harness({
      post: () => {
        throw new Error("lost after commit");
      },
    }),
    p = h.create();
  await assert.rejects(p.run(), /Renewal stopped/);
  const last = h.saved.at(-1);
  assert.equal(last.status, "stopped");
  assert.equal(last.operations.length, 1);
  assert.equal(last.operations[0].status, "uncertain");
  assert.equal(
    last.operations[0].operationId,
    h.calls.find((c) => c.method === "POST").body.operationId,
  );
  await assert.rejects(p.run(), /single-use/);
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
});

test("HTTP 202 cannot hide failed, unknown, unacknowledged or substituted issuance", async () => {
  for (const change of [
    ({ credential }) => {
      credential.state = "failed";
    },
    ({ credential }) => {
      credential.state = "unknown";
    },
    ({ credential }) => {
      credential.remote_acknowledged = false;
    },
    ({ credential }) => {
      credential.creation_operation_id = "11111111-1111-4111-8111-111111111111";
    },
    ({ detail }) => {
      detail.operations[0].state = "requested";
    },
    ({ detail }) => {
      detail.operations = [];
    },
  ]) {
    const h = harness({ post: change });
    await assert.rejects(h.create().run(), /Renewal stopped/);
    assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
    assert.equal(h.saved.at(-1).status, "stopped");
  }
});

test("requested issuance is polled read-only until the same operation is acknowledged", async () => {
  let waits = 0;
  const h = harness({
    post({ credential }) {
      credential.state = "requested";
      credential.remote_acknowledged = false;
    },
    observe({ inventory }) {
      const pending = inventory.credentials.find(
        (c) => c.state === "requested",
      );
      if (pending && ++waits % 2 === 0) {
        pending.state = "ready";
        pending.remote_acknowledged = true;
      }
    },
  });
  assert.equal((await h.create().run()).status, "issued");
  assert.equal(waits, 4);
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
});

test("a second failure preserves the first issued credential and both original operations", async () => {
  const h = harness({
    post({ provider }) {
      if (provider === "qwen") throw new Error("connection lost");
    },
  });
  await assert.rejects(h.create().run(), /Renewal stopped/);
  const operations = h.saved.at(-1).operations;
  assert.deepEqual(
    operations.map((o) => o.status),
    ["issued", "uncertain"],
  );
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 2);
});

test("fresh revision drift is refused before POST and an abort dispatches no request", async () => {
  const h = harness({
    observe({ inventory, reads }) {
      if (reads === 2) inventory.bindings.forEach((b) => b.revision++);
    },
  });
  await assert.rejects(h.create().run(), /Renewal stopped/);
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 0);
  const cancelled = harness(),
    p = cancelled.create();
  cancelled.controller.abort();
  await assert.rejects(p.run());
  assert.equal(cancelled.calls.length, 0);
});

test("unsettled issuance cannot extend the bootstrap deadline or dispatch the second rotation", async () => {
  const h = harness({
    post({ credential }) {
      credential.state = "requested";
      credential.remote_acknowledged = false;
    },
  });
  h.options.deadline = h.options.now() + 5000;
  await assert.rejects(h.create().run(), /Renewal stopped/);
  assert.equal(h.calls.filter((c) => c.method === "POST").length, 1);
  assert.equal(h.saved.at(-1).operations[0].status, "accepted");
});

test("wrong or stale deployed source is refused before credential requests", async () => {
  for (const deployment of [
    {
      hubSource: "f".repeat(40),
      ready: true,
      observedUtc: "2026-11-27T04:04:00Z",
    },
    {
      hubSource: driver.RUN_SOURCE,
      ready: true,
      observedUtc: "2026-11-27T04:03:59Z",
    },
    {
      hubSource: driver.RUN_SOURCE,
      ready: false,
      observedUtc: "2026-11-27T04:04:00Z",
    },
  ]) {
    const h = harness();
    h.options.observeDeployment = async () => deployment;
    await assert.rejects(h.create().run(), /Renewal stopped/);
    assert.equal(h.calls.length, 0);
  }
});
