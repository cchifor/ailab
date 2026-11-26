"use strict";
// No real fetch, timers, browser state, credentials, provider or host operations.
const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const a = require("./owner-phase-a.js");
const NOW = Date.parse("2026-11-26T12:00:00Z");
const SID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const LEASE = "33333333-3333-4333-8333-333333333333";
const OP = "44444444-4444-4444-8444-444444444444";
const REQUEST = "55555555-5555-4555-8555-555555555555";
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
const clone = v => JSON.parse(JSON.stringify(v));
const date = n => new Date(n).toISOString();
const p = a.PROFILES.codex;
const command = a.markerCommand("codex", p.run, "effects");

function harness(handler = () => { throw Error("Unexpected fetch"); }) {
  let clock = NOW;
  const calls = [], saves = [];
  const runner = new a.Runner({ origin: a.ORIGIN, crypto: webcrypto,
    now: () => clock, sleep: async ms => { clock += ms; },
    load: () => null, save: s => saves.push(clone(s)),
    ask: async (_title, _text, validate) => validate(true),
    fetch: async (path, init) => {
      const call = { path, method: init.method, body: init.body && JSON.parse(init.body), init };
      calls.push(call);
      const result = await handler(call, runner, saves);
      return { ok: true, status: 200, headers: { get: () => "application/json" }, text: async () => JSON.stringify(result) };
    },
  });
  return { runner, calls, saves, advance: ms => { clock += ms; } };
}
function session(extra = {}) {
  return { id: SID, kind: "holder", connector_id: HOST, parent_session_id: null,
    generation: 1, rebinding: false, state: "open", control_epoch: 0, ...extra };
}
function detail(extra = {}) {
  return { id: REQUEST, session_id: SID, generation: 1, rebinding: false,
    state: "pending", expires_at: date(NOW + 30000), context: {
      title: "Allow bash?", description: JSON.stringify({ command, timeout: 30 }),
      toolCallId: "pi-ui-fixed", options: [
        { optionId: "yes", kind: "allow_once", name: "Allow once" },
        { optionId: "no", kind: "reject_once", name: "Reject once" },
      ],
    }, ...extra };
}
function marker(phase = "baseline") {
  return { version: 1, provider: "codex", profileId: p.id, runId: p.run,
    phase, state: "observed", phaseChecksPass: true, observedUtc: date(NOW),
    commands: { denied: a.markerCommand("codex", p.run, "denied"), effects: command },
    files: { denied: { state: "absent" }, effects: phase === "baseline" ? { state: "absent" } :
      { state: "present", exactlyOneMarker: true, markerOccurrences: 1, sha256: "a".repeat(64) } } };
}
function trajectory(time = NOW, rows = []) {
  return { version: 1, source: a.ROUTER, origin: "http://127.0.0.1:8787",
    scope: { namespace: "default", principal: "key:" + p.key, route: "canary-pi-codex", since: NOW - 1000 },
    requestedAt: time, observedAt: time + 1, rows, fetchedRows: rows.length,
    totalAttempts: rows.reduce((n, r) => n + r.attempts, 0) };
}
function trajectoryRow(attempts = 1) {
  return { id: "trajectory-1", requestId: "request-1", namespace: "default", principalId: "key:" + p.key,
    route: "canary-pi-codex", startedAt: NOW + 2, endedAt: NOW + 3,
    incomplete: false, status: "complete", attempts, configRevision: 2803 };
}

test("requiring the browser script does not dispatch work", () => {
  const h = harness(); assert.equal(h.calls.length, 0); assert.equal(h.saves.length, 0);
  assert.equal(h.runner.state.completed, false);
});
test("profile hashes bind reviewed definitions after deployed schema defaults", async () => {
  const definitions = JSON.parse(fs.readFileSync(path.join(__dirname, "launch-definitions.json"), "utf8"));
  // Independently checked against ApproveLaunchProfile in deployed e843 OCI021a8f:
  // these four communication defaults are the ONLY parsed-definition changes.
  for (const profile of Object.values(a.PROFILES)) {
    const definition = clone(definitions.find(d => d.holder.id === profile.id));
    definition.session.communication = { directWake: true, broadcastWake: false,
      maxWakesPerHour: 10, maxCausalHops: 4, ...definition.session.communication };
    assert.equal(await a.digest(webcrypto, definition), profile.definitionHash);
    definition.holder.revision++;
    assert.notEqual(await a.digest(webcrypto, definition), profile.definitionHash);
  }
});
test("mutation is persisted before a lost response and cannot silently restart", async () => {
  const h = harness((_c, _r, saves) => {
    assert.equal(saves.at(-1).operations[0].status, "sending");
    assert.deepEqual(saves.at(-1).operations[0].body, { operationId: OP, kind: "prompt", text: "READY" });
    throw Error("lost response after possible acceptance");
  });
  await assert.rejects(h.runner.mutate("/api/agent-sessions/" + SID + "/commands", { operationId: OP, kind: "prompt", text: "READY" }), /lost response/);
  assert.equal(h.calls.length, 1); assert.equal(h.saves.at(-1).operations[0].status, "uncertain");
  assert.equal(h.saves.at(-1).operations[0].body.operationId, OP);
  h.runner.env.load = () => JSON.stringify(h.saves.at(-1));
  await assert.rejects(h.runner.start(), /saved run/); assert.equal(h.calls.length, 1);
});
test("operation lookup preserves ID and refuses null or mismatched receipts", async () => {
  let response = { command: null };
  const h = harness(() => response);
  const op = { sessionId: SID, body: { operationId: OP, kind: "prompt", generation: 1 } };
  await assert.rejects(h.runner.operation(op), /not visible/);
  response = { command: { operation_id: OTHER, session_id: SID, kind: "prompt", generation: 1, state: "completed", receipt: { status: "completed" } } };
  await assert.rejects(h.runner.operation(op), /identity/);
  assert(h.calls.every(c => c.method === "GET" && c.path.endsWith("/operations/" + OP)));
});
test("completed is not accepted, unknown, rejected or withdrawn", () => {
  const expected = { operationId: OP, sessionId: SID, kind: "prompt", generation: 1 };
  const row = { operation_id: OP, session_id: SID, kind: "prompt", generation: 1, state: "accepted" };
  assert.equal(a.completed(row, expected), false);
  for (const state of ["unknown", "rejected", "withdrawn", "other"]) assert.throws(() => a.completed({ ...row, state }, expected));
  assert.throws(() => a.completed({ ...row, state: "completed" }, expected), /receipt/);
  assert.equal(a.completed({ ...row, state: "completed", receipt: { status: "completed" } }, expected), true);
});
test("acquire reads flat session and refuses existing lease without takeover", async () => {
  const h = harness(c => c.path.endsWith("/control") ? { lease: { owned: false, id: OTHER } } : session());
  h.runner.session = session();
  await assert.rejects(h.runner.acquire(), /Existing control lease/);
  assert(h.calls.every(c => c.method === "GET"));
});
test("lease renewal keeps exact ID; identity change is rejected", async () => {
  let changed = false;
  const h = harness(c => { assert.equal(c.body.leaseId, LEASE); return { id: changed ? OTHER : LEASE, generation: 1, expires_at: date(NOW + 60000) }; });
  h.runner.session = session(); h.runner.lease = { id: LEASE, generation: 1, expires_at: date(NOW + 20000) };
  await h.runner.renew(); assert.equal(h.runner.lease.id, LEASE);
  changed = true; await assert.rejects(h.runner.renew(), /identity changed/);
  assert.equal(h.runner.lease.id, LEASE);
});
test("expired lease and changed session generation block command dispatch", async () => {
  const h = harness(() => session({ generation: 2 }));
  h.runner.session = session(); h.runner.lease = { id: LEASE, expires_at: date(NOW - 1) };
  await assert.rejects(h.runner.command("prompt", { text: "READY" }), /lease expired/);
  assert.equal(h.calls.length, 0);
  h.runner.lease.expires_at = date(NOW + 60000);
  await assert.rejects(h.runner.command("prompt", { text: "READY" }), /identity\/generation/);
  assert(h.calls.every(c => c.method === "GET"));
});
test("deployed empty permission description fails before approval checkpoint or POST", async () => {
  const d = detail(); d.context.description = "";
  const h = harness(c => c.path.endsWith("/permissions") ? [{ id: REQUEST, state: "pending" }] : d);
  h.runner.session = session(); h.runner.env.ask = () => { throw Error("Must not ask owner to approve unverifiable input"); };
  await assert.rejects(h.runner.decide(command, "allow_once"), /Full tool input unavailable/);
  assert(h.calls.every(c => c.method === "GET"));
});
test("once-only permission validates exact input, expiry, options and identity", () => {
  assert.equal(a.permission(detail(), SID, 1, command, "allow_once", NOW), "yes");
  assert.equal(a.permission(detail(), SID, 1, command, "reject_once", NOW), "no");
  for (const mutation of [
    d => { d.context.description = JSON.stringify({ command: command + "; unexpected" }); },
    d => { d.context.description = JSON.stringify({ command, cwd: "/elsewhere" }); },
    d => { d.context.description = JSON.stringify({ command, timeout: 121 }); },
    d => { d.context.options[0].kind = "allow_always"; },
    d => { d.context.options.push(clone(d.context.options[0])); },
    d => { d.session_id = OTHER; }, d => { d.generation = 2; },
    d => { d.expires_at = date(NOW); }, d => { d.rebinding = true; },
  ]) { const d = detail(); mutation(d); assert.throws(() => a.permission(d, SID, 1, command, "allow_once", NOW)); }
});
test("permission changes during owner review do not dispatch a decision", async () => {
  let reads = 0, asked = 0;
  const h = harness(c => {
    if (c.path.endsWith("/permissions")) return [{ id: REQUEST, state: "pending" }];
    const d = detail(); if (++reads > 1) d.state = "expired"; return d;
  });
  h.runner.session = session(); h.runner.env.ask = async () => { asked++; return true; };
  await assert.rejects(h.runner.decide(command, "allow_once"), /expired/);
  assert.equal(asked, 1); assert(h.calls.every(c => c.method === "GET"));
});
test("multiple pending permissions are not chosen by title or order", async () => {
  const h = harness(() => [{ id: REQUEST, state: "pending" }, { id: OTHER, state: "pending" }]);
  h.runner.session = session(); await assert.rejects(h.runner.decide(command, "allow_once"), /Concurrent/);
  assert.equal(h.calls.length, 1);
});
test("baseline preserves absence, exact commands, freshness and unchanged open observation", () => {
  const before = marker(); a.marker(before, "codex", p.run, "baseline", NOW);
  a.unchangedBaseline(before, { ...before, observedUtc: date(NOW + 1000) });
  const changed = clone(before); changed.files.effects = { state: "present" };
  assert.throws(() => a.marker(changed, "codex", p.run, "baseline", NOW), /Unexpected effect/);
  assert.throws(() => a.unchangedBaseline(before, changed), /changed/);
  assert.throws(() => a.marker(before, "codex", p.run, "baseline", NOW + 120001), /fresh/);
  const duplicate = marker("approved"); duplicate.files.effects.markerOccurrences = 2;
  assert.throws(() => a.marker(duplicate, "codex", p.run, "approved", NOW), /exactly once/);
});
test("paired trajectory delta rejects truncation, moving baseline and wrong scope", () => {
  const before = trajectory(), after = trajectory(NOW + 5, [trajectoryRow()]);
  assert.equal(a.delta(before, after, "codex"), 1);
  assert.throws(() => a.snapshot({ ...after, fetchedRows: 500 }, "codex"), /Incomplete/);
  const wrong = clone(after); wrong.scope.principal = "key:other";
  assert.throws(() => a.delta(before, wrong, "codex"), /scope/);
  const lost = trajectory(NOW + 10); assert.throws(() => a.delta(after, lost, "codex"), /disappeared/);
  const changed = clone(after); changed.rows[0].incomplete = true;
  assert.throws(() => a.snapshot(changed, "codex"), /Unsettled/);
});
test("lifecycle and effort prompt budgets stop before any fetch", async () => {
  for (const [effort, prompts] of [[false, 12], [true, 4]]) {
    const h = harness(); h.runner.run = { effort, prompts, deadline: NOW + 60000 };
    await assert.rejects(h.runner.prompt("should never dispatch"), /Prompt budget/);
    assert.equal(h.calls.length, 0);
  }
});
test("observed generation ceiling and zero-attempt checkpoints are enforced", async () => {
  for (const [effort, attempts, zero] of [[false, 21, false], [true, 7, false], [false, 1, true]]) {
    const h = harness(); const before = trajectory(NOW - 10);
    h.runner.run = { provider: "codex", effort, deadline: NOW + 60000, firstSnapshot: before, lastSnapshot: before };
    h.runner.env.ask = async (_t, _i, validate) => validate({ coverageConfirmed: true, trajectory: trajectory(NOW + 5, [trajectoryRow(attempts)]) });
    await assert.rejects(h.runner.evidence("bounded", null, zero), /budget|zero recorded/);
    assert.deepEqual(h.runner.run.lastSnapshot, before); assert.equal(h.calls.length, 0);
  }
});
test("recovery receipt cannot bypass profile revision or workspace identity", async () => {
  const h = harness(c => {
    if (c.method === "POST") return { id: REQUEST, expires_at: date(NOW + 60000) };
    if (c.path === "/api/agent-launches") return [{ id: REQUEST, operation_id: h.runner.state.operations[0].body.operationId,
      state: "open", session_id: OTHER, resume_from: SID, profile_id: OTHER, profile_revision: 99, cwd: "/wrong",
      holder_id: a.HOLDER, holder_journal_id: a.HOLDER, native_recovery_version: 2,
      receipt: { status: "completed", result: { sessionId: OTHER, nativeSessionId: OP, nativeRecoveryVersion: 2 } } }];
    return session({ id: OTHER });
  });
  h.runner.run = { provider: "codex", deadline: NOW + 120000 };
  h.runner.session = session(); h.runner.launchRow = { session_id: SID, receipt: { result: { nativeSessionId: OP } } };
  let delivery = 0;
  h.runner.checkDelivery = async () => { delivery++; };
  h.runner.acquire = async () => {};
  await assert.rejects(h.runner.launch(true), /profile|workspace|identity/i);
  assert.equal(delivery, 0);
});
test("cleanup cannot take over another owner's lease", async () => {
  const h = harness(c => c.path.endsWith("/control") ? { lease: { owned: false, id: LEASE } } : session());
  h.runner.session = session(); h.runner.lease = { id: LEASE };
  await h.runner.cleanup();
  assert(h.calls.every(c => c.method === "GET"));
  assert.equal(h.runner.state.evidence.at(-1).status, "unresolved-owner-inspection-required");
  assert.equal(h.runner.state.completed, false);
});
test("sanitized projections discard unrelated pasted fields at every level", () => {
  const m = marker(); m.unrelated = "not-for-export"; m.files.effects.unrelated = "not-for-export";
  const t = trajectory(NOW + 5, [trajectoryRow()]);
  t.unrelated = "not-for-export"; t.scope.unrelated = "not-for-export"; t.rows[0].captured = { private: "not-for-export" };
  assert(!JSON.stringify(a.marker(m, "codex", p.run, "baseline", NOW)).includes("not-for-export"));
  assert(!JSON.stringify(a.snapshot(t, "codex")).includes("not-for-export"));
});
test("closed session may release an already retired lease; open session may not", async () => {
  for (const state of ["closed", "open"]) {
    const h = harness(c => c.path.endsWith("/control") ? { lease: null } : session({ state }));
    h.runner.session = session(); h.runner.lease = { id: LEASE };
    if (state === "closed") { await h.runner.release(); assert.equal(h.runner.lease, null); }
    else { await assert.rejects(h.runner.release(), /disappeared/); assert.equal(h.runner.lease.id, LEASE); }
    assert(h.calls.every(c => c.method === "GET"));
  }
});
test("timeline drains independent cursors and deduplicates shared rows", async () => {
  const cursor = "2026-11-26 12:00:00+00|" + OP;
  let page = 0;
  const h = harness(c => {
    const q = new URL(c.path, a.ORIGIN).searchParams;
    if (page++) { assert.equal(q.get("after"), "2"); assert.equal(q.get("promptsAfter"), cursor); }
    return { sessionId: SID, generation: 1, text: true, state: "open",
      events: [{ sequence: page === 1 ? "2" : "3", createdAt: date(NOW), event: { kind: "update", type: "agent_message_chunk", text: "READY" } }],
      prompts: [{ id: OP, state: "completed" }], nextAfter: null, nextPromptsAfter: page === 1 ? cursor : null };
  });
  h.runner.session = session(); const result = await h.runner.timeline();
  assert.deepEqual(result.events.map(e => e.sequence), ["2", "3"]);
  assert.equal(result.prompts.length, 1); assert.equal(h.calls.length, 2);
});
test("bounded prompt requires actual terminal receipt and exact new assistant text", async () => {
  for (const stopReason of ["end_turn", "max_tokens", "cancelled"]) {
    let posted, timelines = 0;
    const h = harness(c => {
      if (c.path.includes("/timeline?")) return { sessionId: SID, generation: 1, text: true,
        events: timelines++ ? [{ sequence: "1", event: { kind: "update", type: "agent_message_chunk", text: "READY" } }] : [],
        prompts: posted ? [{ id: REQUEST, state: "completed" }] : [], nextAfter: null, nextPromptsAfter: null };
      if (c.path.endsWith("/permissions")) return [];
      if (c.path.endsWith("/commands")) { posted = c.body; return { id: REQUEST }; }
      if (c.path.includes("/operations/")) return { command: { operation_id: posted.operationId,
        session_id: SID, kind: "prompt", generation: 1, state: "completed", receipt: { status: "completed", result: { stopReason } } } };
      return session();
    });
    h.runner.session = session(); h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
    h.runner.run = { effort: false, prompts: 0, deadline: NOW + 60000 };
    if (stopReason === "end_turn") assert.equal(await h.runner.prompt("Reply READY", { expectedText: "READY" }), "READY");
    else await assert.rejects(h.runner.prompt("Reply READY", { expectedText: "READY" }), /stop reason/);
    assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
    assert.equal(h.runner.run.prompts, 1);
  }
});
test("queued permission decision waits for resolved accepted delivery, never execution proof", async () => {
  for (const outcome of ["accepted", "unknown"]) {
    let submitted = false, polls = 0;
    const h = harness(c => {
      if (c.path.endsWith("/permissions")) return [{ id: REQUEST, state: "pending" }];
      if (c.method === "POST") { submitted = true; return { commandId: OP, requestId: REQUEST, delivery: "pending", execution: "unknown" }; }
      if (!submitted) return detail();
      if (++polls === 1) return detail({ state: "decided", delivery: "offered" });
      return detail({ state: outcome === "accepted" ? "resolved" : "decided", delivery: outcome,
        decision: { outcome: "selected", optionId: "yes" } });
    });
    h.runner.session = session();
    if (outcome === "accepted") {
      assert.equal(await h.runner.decide(command, "allow_once"), true);
      assert.match(h.runner.state.evidence.at(-1).execution, /unknown/);
    } else {
      await assert.rejects(h.runner.decide(command, "allow_once"), /delivery failed\/uncertain/);
      assert.equal(h.runner.state.evidence.length, 0);
    }
    assert.equal(polls, 2); assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
  }
});
test("lost prompt POST cleanup observes original operation and narrows once", async () => {
  const commands = new Map(); let closed = false;
  const h = harness(c => {
    if (c.path.endsWith("/control")) return { lease: closed ? null : { id: LEASE, owned: true, generation: 1 } };
    if (c.path.endsWith("/permissions")) return [];
    if (c.path.endsWith("/commands")) {
      commands.set(c.body.operationId, c.body);
      if (c.body.kind === "prompt") throw Error("response lost after POST");
      if (c.body.kind === "close") closed = true;
      return { id: c.body.operationId };
    }
    if (c.path.includes("/operations/")) {
      const body = commands.get(c.path.split("/").at(-1)); assert(body, "Only saved operations may be queried");
      return { command: { operation_id: body.operationId, session_id: SID, generation: 1,
        kind: body.kind, state: "completed", receipt: { status: "completed",
          result: body.kind === "prompt" ? { stopReason: "cancelled" } : { cancelSent: true, execution: "unknown" } } } };
    }
    return session({ state: closed ? "closed" : "open" });
  });
  h.runner.session = session(); h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
  await assert.rejects(h.runner.command("prompt", { text: "READY" }), /response lost/);
  const original = h.runner.pending.body.operationId;
  assert.equal(original, h.runner.state.operations[0].body.operationId);
  await h.runner.cleanup();
  assert.deepEqual([...commands.values()].map(c => c.kind), ["prompt", "cancel", "close"]);
  assert(h.calls.some(c => c.path.endsWith("/operations/" + original)));
  assert.equal(h.runner.state.evidence.at(-1).status, "closed-and-released");
  const count = h.calls.length; await h.runner.cleanup(); assert.equal(h.calls.length, count);
  assert.equal(h.runner.state.completed, false);
});
test("lost close POST cleanup inspects same close and never creates replacement", async () => {
  let body, closed = false;
  const h = harness(c => {
    if (c.path.endsWith("/control")) return { lease: closed ? null : { id: LEASE, owned: true, generation: 1 } };
    if (c.path.endsWith("/permissions")) return [];
    if (c.path.endsWith("/commands")) { body = c.body; throw Error("close response lost"); }
    if (c.path.includes("/operations/")) {
      assert(c.path.endsWith(body.operationId)); closed = true;
      return { command: { operation_id: body.operationId, session_id: SID, generation: 1, kind: "close",
        state: "completed", receipt: { status: "completed", result: { cancelSent: true, execution: "unknown" } } } };
    }
    return session({ state: closed ? "closed" : "open" });
  });
  h.runner.session = session(); h.runner.lease = { id: LEASE, expires_at: date(NOW + 60000) };
  await assert.rejects(h.runner.command("close"), /close response lost/);
  await h.runner.cleanup();
  assert.equal(h.calls.filter(c => c.method === "POST").length, 1);
  assert.equal(h.runner.state.evidence.at(-1).status, "closed-and-released");
});
