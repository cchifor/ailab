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
const clone = (v) => JSON.parse(JSON.stringify(v));
const date = (n) => new Date(n).toISOString();
const p = a.PROFILES.codex;
const command = a.markerCommand("codex", p.run, "effects");

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

test("requiring the browser script does not dispatch work", () => {
  const h = harness();
  assert.equal(h.calls.length, 0);
  assert.equal(h.saves.length, 0);
  assert.equal(h.runner.state.completed, false);
});
test("unqualified draft start refuses before network, checkpoints or saved-run mutation", async () => {
  const h = harness();
  h.runner.env.ask = () => {
    throw Error("Draft must not ask to activate");
  };
  await assert.rejects(h.runner.start(), /draft|qualified|publication|pins/i);
  assert.equal(h.calls.length, 0);
  assert.equal(h.saves.length, 0);
  assert.equal(h.runner.state.completed, false);
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
  await assert.rejects(h.runner.start(), /saved run/);
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
} = {}) {
  let clock = NOW,
    serial = 100,
    sequence = 0;
  const sessions = new Map(),
    launches = [],
    commands = new Map(),
    permissions = new Map();
  const calls = [],
    checkpoints = [],
    attempts = [],
    effects = new Map();
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
  const profile = (sid) => a.PROFILES[sessions.get(sid).provider];
  function observeTrajectory(provider) {
    const pp = a.PROFILES[provider],
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
        since: NOW,
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
      profileId: a.PROFILES[provider].id,
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
    origin: a.ORIGIN,
    crypto: webcrypto,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    load: () => null,
    save: () => {},
    ask: async (title, instructions, validate) => {
      checkpoints.push(title);
      clock += 20;
      const boundary = runner.state.evidence
        .filter((e) => e.kind === "collection-boundary")
        .at(-1);
      if (boundary) clock = Math.max(clock, boundary.notBefore + 1);
      if (runner.lease && Date.parse(runner.lease.expires_at) - clock < 30000)
        await runner.renew();
      let value = true;
      if (title === "Inspect retained branch before continuation")
        value = { reconcile: true };
      else if (title.includes("pre-launch handoff"))
        value = {
          trajectory: observeTrajectory(runner.run.provider),
          marker: observeMarker(
            runner.run.provider,
            runner.run.runId,
            "baseline",
          ),
          coverageConfirmed: true,
          holderReadyConfirmed: true,
          routeConfirmed: true,
          clocksWithin5s,
        };
      else if (title.startsWith("Open complete:"))
        value = {
          marker: observeMarker(
            runner.run.provider,
            runner.run.runId,
            "baseline",
          ),
          deliveryConfirmed: true,
        };
      else if (instructions.startsWith("Paste operator JSON:")) {
        const phase = /Marker phase=(\w+)/.exec(instructions)?.[1];
        value = {
          trajectory: observeTrajectory(runner.run.provider),
          coverageConfirmed: true,
          ...(phase
            ? {
                marker: observeMarker(
                  runner.run.provider,
                  runner.run.runId,
                  phase,
                ),
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
      if (pathName === "/api/agent-profiles")
        result = definitions.map((d) => ({
          id: d.holder.id,
          revision: 2,
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
          Object.keys(a.PROFILES).find(
            (k) => a.PROFILES[k].id === body.profileId,
          );
        assert(provider);
        const pp = a.PROFILES[provider];
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
          profile_revision: 2,
          cwd: "/home/c4/.local/state/relay-holder/workspaces/" + pp.id,
          state: "pending",
          session_id: sid,
          holder_id: a.HOLDER,
          holder_journal_id: a.HOLDER,
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
        const pp = Object.values(a.PROFILES).find(
          (x) => x.credential === pathName.split("/").at(-1),
        );
        assert(pp);
        result = {
          credential: {
            id: pp.credential,
            bindingId: pp.binding,
            routerKeyId: pp.key,
            namespace: "default",
            state: "ready",
          },
          binding: { currentCredentialId: pp.credential },
        };
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
            connection: { enabled: true },
            binding: { id: pp.binding, enabled: true, state: "ready" },
            credential: { id: pp.credential, state: "ready" },
            consumer: {
              credentialId: pp.credential,
              state: "active",
              credentialState: "ready",
              remoteAcknowledged: true,
              revokeRequested: false,
              credentialExpiresAt: date(a.EXPIRES),
              leaseExpiresAt: date(clock + 60000),
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
            else if (text.startsWith("Request permission")) {
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
              c.response = "Decision handled.";
            } else if (c.counting) c.response = "";
            else {
              const expected = /Reply exactly ([A-Z_]+)\./.exec(text);
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
  };
}

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
        "Reply exactly READY",
        "Request permission to run exactly the following shell command once",
        "Request permission to run exactly the following shell command once",
        "/relay-history",
        "/relay-branch 1234abcd",
        "Reply exactly BRANCH_OK",
        "/relay-compact",
        "Reply exactly COMPACT_OK",
        "Without using tools or modifying files, count upward from 1, one integer per line, until 500",
        "/relay-reconcile confirmed",
        "Reply exactly RECOVERED_OK",
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
  let serial = 500;
  const uid = () =>
    `00000000-0000-4000-8000-${String(++serial).padStart(12, "0")}`;
  const rows = Object.entries(a.PROFILES).map(([provider, pp]) => ({
    provider,
    pp,
    binding: {
      id: pp.binding,
      host_id: HOST,
      execution_identity: "c4",
      revision: 3,
      generation: 2,
      enabled: true,
      state: "ready",
      current_credential_id: pp.credential,
      pending_credential_id: null,
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
      assert.equal(c.body.revision, 3);
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
        revision: revoked ? (options.changedRevision ? 6 : 4) : 3,
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
        revision: 3,
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
        credentialId: row.pp.credential,
        generation: 2,
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
