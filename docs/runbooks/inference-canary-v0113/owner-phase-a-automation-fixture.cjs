"use strict";
// Standalone synthetic HTTP/observer state. No Runner instance, clock override,
// provider, real holder, authentication or imported test registrations.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { createHash } = require("node:crypto");
const { setTimeout: delay } = require("node:timers/promises");
const a = require("./owner-phase-a.js");
const NOW = Date.parse("2026-11-26T12:00:00Z");
const SID = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const LEASE = "33333333-3333-4333-8333-333333333333";
const OP = "44444444-4444-4444-8444-444444444444";
const REQUEST = "55555555-5555-4555-8555-555555555555";
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
const p = a.PROFILES.codex;
const command = a.markerCommand("codex", p.run, "effects");
const clone = (v) => JSON.parse(JSON.stringify(v));
const date = (n) => new Date(n).toISOString();
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
function phaseApi({
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
    a.renewedCredentials(renewal.inventory, renewal.details, Date.now());
  const profiles = projection ? projection.profiles : a.PROFILES;
  let clock = Date.now(),
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
    clock = Date.now();
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
    clock = Date.now();
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
  const dispatch = async (url, init) => {
    clock = Date.now();
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
  };
  return {
    dispatch,
    calls,
    sessions,
    launches,
    commands,
    permissions,
    effects,
    attempts,
    consumerReads,
    observeTrajectory,
    observeMarker,
    profiles,
    setTrajectorySince: (value) => {
      trajectorySince = value;
    },
  };
}

async function createFixture(directory, outputDirectory) {
  const raw = JSON.stringify(stoppedReport(), null, 2) + "\n";
  const prior = a.stoppedPredecessor(raw);
  const projection = {
    kind: "relay-predecessor-command-projection",
    version: 1,
    source: a.RUN_SOURCE,
    schemaVersion: 93,
    observedUtc: date(Date.now()),
    tenantId: OTHER,
    databaseIdentity: "synthetic-host-test",
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
  const projectionRaw = JSON.stringify(projection);
  const projectionPin = {
    sha256: createHash("sha256").update(projectionRaw).digest("hex"),
    tenantId: projection.tenantId,
    databaseIdentity: projection.databaseIdentity,
    querySha256: projection.querySha256,
    commands: projection.commands.map((row) => ({
      operationId: row.operation_id,
      commandId: row.id,
      kind: row.kind,
    })),
  };
  // Historical captured field names; these modified dates/transitions are ONLY
  // synthetic server data, not a recapture or a current production observation.
  const ready = clone(
    require("./owner-phase-a.hub-74d1.fixture.json").bootstrap,
  );
  for (const provider of ["codex", "qwen"]) {
    const detail = ready.details[provider];
    detail.credential.expiresAt = date(Date.now() + 3600000);
    ready.inventory.credentials.find(
      (r) => r.id === detail.credential.id,
    ).expires_at = detail.credential.expiresAt;
  }
  const inventory = clone(ready.inventory);
  inventory.credentials = inventory.credentials.filter(
    (c) => c.generation === 1,
  );
  for (const binding of inventory.bindings)
    Object.assign(binding, {
      revision: 1,
      generation: 1,
      state: "failed",
      canRenew: true,
      current_credential_id: null,
      pending_credential_id: null,
    });
  const requests = [],
    heartbeats = [],
    failures = [],
    contexts = [],
    rotations = [];
  const pendingIssuance = new Map();
  let phase,
    heartbeatWaited = false;
  function latestSnapshot() {
    const names = fs
      .readdirSync(outputDirectory)
      .filter((n) => /^state-\d+\.json$/.test(n))
      .sort();
    return JSON.parse(
      fs.readFileSync(path.join(outputDirectory, names.at(-1))),
    );
  }
  function assertDurable(pathname, body) {
    const snapshot = latestSnapshot();
    if (pathname.endsWith("/rotate")) {
      const intent = snapshot.renewal.operations.at(-1);
      assert.equal(intent.status, "sending");
      assert.equal(intent.operationId, body.operationId);
      assert.equal(intent.revision, body.revision);
    } else if (body.operationId) {
      const intent = snapshot.operations.find(
        (o) => o.body?.operationId === body.operationId,
      );
      assert(intent, "actual durable operation before socket dispatch");
      // Runner.cleanup records this separate status before its once-only POST.
      // The operation ID, complete body and path must still match exactly.
      assert(["sending", "cleanup-dispatch"].includes(intent.status));
      assert.equal(intent.path, pathname);
      assert.deepEqual(intent.body, body);
    } else if (!body.leaseId) {
      assert.equal(snapshot.controlAcquisition.status, "sending");
      assert.equal(snapshot.controlAcquisition.path, pathname);
    }
  }
  function finishIssuance(provider) {
    const info = pendingIssuance.get(provider);
    const id = a.PROFILES[provider].binding;
    Object.assign(
      inventory.bindings.find((b) => b.id === id),
      clone(ready.inventory.bindings.find((b) => b.id === id)),
    );
    const credential = clone(
      ready.inventory.credentials.find(
        (c) => c.binding_id === id && c.generation === 2,
      ),
    );
    credential.creation_operation_id = info.operationId;
    Object.assign(
      inventory.credentials.find((c) => c.id === credential.id),
      credential,
    );
    ready.details[provider].credential.creationOperationId = info.operationId;
    ready.details[provider].operations = [
      {
        id: info.operationId,
        kind: "rotate",
        state: credential.state,
        failureCode: null,
      },
    ];
    pendingIssuance.delete(provider);
    if (rotations.length === 2 && pendingIssuance.size === 0) {
      ready.inventory = clone(inventory);
      phase = phaseApi({ renewal: ready, holderId: SID, journalId: SID });
    }
  }
  async function dispatch(pathname, init) {
    const body = init.body ? JSON.parse(init.body) : undefined;
    requests.push({ pathname, method: init.method, body, at: Date.now() });
    if (init.method === "POST") assertDurable(pathname, body);
    if (pathname.endsWith("/rotate")) {
      const provider = ["codex", "qwen"].find(
        (name) =>
          pathname ===
          "/api/router-credentials/" + a.PROFILES[name].binding + "/rotate",
      );
      assert(provider && !rotations.some((r) => r.provider === provider));
      assert.equal(body.revision, 1);
      rotations.push({ provider, operationId: body.operationId });
      pendingIssuance.set(provider, {
        operationId: body.operationId,
        reads: 0,
      });
      const id = a.PROFILES[provider].binding;
      const requested = clone(
        ready.inventory.credentials.find(
          (c) => c.binding_id === id && c.generation === 2,
        ),
      );
      Object.assign(requested, {
        state: "requested",
        remote_acknowledged: false,
        creation_operation_id: body.operationId,
      });
      inventory.credentials.push(requested);
      Object.assign(
        inventory.bindings.find((b) => b.id === id),
        {
          generation: 2,
          revision: 2,
          state: "requested",
          canRenew: false,
          current_credential_id: null,
          pending_credential_id: requested.id,
        },
      );
      // The new generation remains unacknowledged until later reads; a 202
      // acceptance cannot itself satisfy the real prelude's issuance checks.
      return { status: 202, value: { accepted: true } };
    }
    if (!phase && pathname === "/api/router-credentials") {
      for (const [provider, info] of pendingIssuance)
        if (++info.reads >= 2) finishIssuance(provider);
      return { status: 200, value: clone(inventory) };
    }
    if (!phase && pathname.startsWith("/api/router-credentials/keys/")) {
      const value = Object.values(ready.details).find(
        (d) => d.credential.id === pathname.split("/").at(-1),
      );
      assert(value && value.credential.creationOperationId);
      return { status: 200, value };
    }
    assert(phase, "No successor work before both rotations are terminal");
    if (
      pathname.startsWith("/api/agent-sessions/" + a.STOPPED_SESSION) ||
      pathname.startsWith("/api/agent-permissions/" + a.STOPPED_PERMISSION)
    ) {
      assert(
        !pathname.includes("/operations/"),
        "actor-scoped old operations must use the pinned projection",
      );
      return {
        status: 200,
        value: predecessorResponse(prior, {
          path: pathname,
          method: init.method,
        }),
      };
    }
    if (
      pathname.endsWith("/control") &&
      init.method === "POST" &&
      body.leaseId
    ) {
      const session = phase.sessions.get(pathname.split("/")[3]);
      assert.equal(session.lease.id, body.leaseId);
      assert.equal(session.state, "open");
      heartbeats.push({
        sessionId: session.id,
        leaseId: body.leaseId,
        at: Date.now(),
      });
    }
    const result = await phase.dispatch(pathname, init);
    return { status: result.status, value: JSON.parse(await result.text()) };
  }
  const socketPath = path.join(directory, "session.sock");
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", async () => {
      try {
        assert.equal(req.headers.cookie, undefined);
        assert.equal(req.headers.authorization, undefined);
        const result = await dispatch(req.url, {
          method: req.method,
          body: body || undefined,
        });
        res.writeHead(result.status, { "content-type": "application/json" });
        res.end(JSON.stringify(result.value));
      } catch (error) {
        failures.push(error.stack);
        res.writeHead(500, { "content-type": "application/json" });
        res.end(
          JSON.stringify({ error: "synthetic fixture assertion failed" }),
        );
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  async function collectCheckpoint({ context, signal }) {
    assert.equal(context.version, 1);
    assert(Object.isFrozen(context));
    contexts.push(clone(context));
    const replacementBoundary =
      context.kind === "pre-launch" && context.sinceAfter != null
        ? context.sinceAfter
        : null;
    const notBefore = Math.max(
      context.notBefore || 0,
      replacementBoundary === null ? 0 : replacementBoundary + 1,
    );
    // A duration timer does not prove a wall-clock boundary was reached.
    // Recheck after every wake; retain cancellation and real observed timestamps.
    while (Date.now() < notBefore)
      await delay(Math.max(1, notBefore - Date.now() + 2), undefined, {
        signal,
      });
    const now = Date.now();
    switch (context.kind) {
      case "renewal-deployment":
        return {
          confirmMetadataReview: true,
          reportDownloaded: true,
          operatorVerified: true,
          deploymentVerified: true,
          observedUtc: date(now),
          hubSource: a.RUN_SOURCE,
        };
      case "renewed-credentials":
        return {
          confirmRenewedCredentials: true,
          ...a.renewedCredentials(ready.inventory, ready.details, now),
        };
      case "stopped-replacement": {
        const rows = ["338869f5-complete", "811cab9f-cancelled"].map(
          (id, i) => ({
            ...trajectoryRow(),
            id,
            status: i ? "cancelled" : "complete",
            startedAt: NOW - 50 + i,
            endedAt: NOW - 20 + i,
          }),
        );
        return {
          confirmReplacement: true,
          reportDownloaded: true,
          operatorVerified: true,
          priorReportCanonicalSha256: projection.priorReportCanonicalSha256,
          windowStartUtc: date(now),
          windowEndUtc: date(now + 900000),
          trajectory: trajectory(now, rows),
          marker: { ...marker(), observedUtc: date(now) },
        };
      }
      case "preserving-holder": {
        phase.setTrajectorySince(now);
        const inputs = require("./holder-inputs.json");
        return {
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
          observedUtc: date(now),
          qualifiedRelease: "v0.13.4",
          holderSource: "64dc50f47a4867a6d9a7afc2e7d5ae24068c59af",
          holderBinarySha256:
            "d406b1f363f590b386ab3eaea4a8cb350c5ac9c49a3dcb26acc34034dfcf6926",
          profilesSha256: inputs.relay_holder_profiles_sha256,
          nativeRecovery: 2,
          nativeStateReady: true,
          activeSessions: 0,
          pendingCommands: 0,
          admissionPaused: false,
          preservedOldStateConfirmed: true,
          operatorVerified: true,
        };
      }
      case "pre-launch":
        // The new scope starts at this observation, strictly after the prior
        // collector finished; holder handoff can occur in the same millisecond.
        if (replacementBoundary !== null) phase.setTrajectorySince(now);
        return {
          trajectory: phase.observeTrajectory(context.provider),
          marker: phase.observeMarker(
            context.provider,
            context.runId,
            "baseline",
          ),
          coverageConfirmed: true,
          holderReadyConfirmed: true,
          routeConfirmed: true,
          clocksWithin5s: true,
        };
      case "post-open":
        return {
          marker: phase.observeMarker(
            context.provider,
            context.runId,
            "baseline",
          ),
          deliveryConfirmed: true,
        };
      case "observation":
        return {
          trajectory: phase.observeTrajectory(context.provider),
          coverageConfirmed: true,
          ...(context.phase
            ? {
                marker: phase.observeMarker(
                  context.provider,
                  context.runId,
                  context.phase,
                ),
              }
            : {}),
        };
      case "permission": {
        assert.equal(context.purpose, "marker");
        assert(phase.permissions.has(context.permission.id));
        if (!heartbeatWaited) {
          heartbeatWaited = true;
          const since = heartbeats.length,
            deadline = Date.now() + 25000;
          while (
            !heartbeats
              .slice(since)
              .some((h) => h.sessionId === context.sessionId)
          ) {
            assert(
              Date.now() < deadline,
              "real 20s heartbeat must renew the held lease",
            );
            await delay(25, undefined, { signal });
          }
        }
        return true;
      }
      case "recover-history":
        assert.equal(phase.sessions.get(context.sessionId).state, "closed");
        return true;
      case "retire-branch":
        assert.equal(phase.sessions.get(context.sessionId).state, "open");
        return true;
      case "reconcile-choice":
        assert.equal(phase.sessions.get(context.sessionId).state, "open");
        return { reconcile: true };
      default:
        throw Error("Unknown typed checkpoint " + context.kind);
    }
  }
  return {
    raw,
    prior,
    projectionPin,
    projectionRaw,
    socketPath,
    requests,
    rotations,
    heartbeats,
    failures,
    contexts,
    phase: () => phase,
    collectCheckpoint,
    observeDeployment: async () => ({
      hubSource: a.RUN_SOURCE,
      ready: true,
      observedUtc: date(Date.now()),
    }),
    readPredecessorEvidence: async (context) => {
      assert.equal(context.sessionId, a.STOPPED_SESSION);
      assert.equal(
        context.priorReportCanonicalSha256,
        projection.priorReportCanonicalSha256,
      );
      return projectionRaw;
    },
    close: async () => {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

module.exports = { createFixture };
