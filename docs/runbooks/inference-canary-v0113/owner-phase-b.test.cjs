"use strict";
// Synthetic normal HTTP/typed projections only; actual public Claude Runner.
const test = require("node:test"),
  assert = require("node:assert/strict");
const { webcrypto, createHash } = require("node:crypto");
const fs = require("node:fs"),
  path = require("node:path");
// The checked-in public B bundle remains held. Only exact source-pin literals
// are changed in these isolated compiled modules; all Runner methods are real.
const sourceFixture = require("./owner-phase-b-fence-fixture.cjs");
const { a, b, proof: sourceProof } = sourceFixture.sourceFixture();
const NOW = Date.parse("2026-11-27T21:00:00Z");
const date = (x) => new Date(x).toISOString(),
  clone = (x) => JSON.parse(JSON.stringify(x));
const ID = (x) => `11111111-1111-4111-8111-${String(x).padStart(12, "0")}`;
const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171",
  CONNECTION = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8";
const HOLDER = ID(1),
  JOURNAL = ID(2),
  SID = ID(3),
  REQUEST = ID(4),
  CID = ID(5);
const KEY = "key_1234567890abcdef1234567890abcdef";
const INPUT = {
  version: 1,
  lifecycleRunId: ID(6),
  effortRunId: ID(7),
  account: "claude-selected-fixture",
  model: "claude-sonnet-fixture",
  window: { startsAt: NOW, expiresAt: NOW + 1500000 },
};
const command = a.markerCommand("claude", INPUT.lifecycleRunId, "effects", {
  claude: b.PROFILE,
});
function metadata() {
  const c = {
    id: CID,
    binding_id: b.PROFILE.binding,
    generation: 1,
    router_key_id: KEY,
    namespace: "default",
    remote_acknowledged: true,
    revoke_requested: false,
    cleanup_pending: false,
    failure_code: null,
    retire_after: null,
    state: "ready",
    expires_at: date(NOW + 3600000),
  };
  const binding = {
    id: b.PROFILE.binding,
    enabled: true,
    canRenew: false,
    state: "ready",
    connection_id: CONNECTION,
    host_id: HOST,
    execution_identity: "c4",
    grant_spec: {
      routes: ["canary-pi-claude"],
      access: ["subscription"],
      lifetimeSeconds: 86400,
    },
    revision: 1,
    generation: 1,
    current_credential_id: CID,
    pending_credential_id: null,
  };
  const d = {
    credential: {
      id: CID,
      bindingId: binding.id,
      connectionId: CONNECTION,
      generation: 1,
      routerKeyId: KEY,
      namespace: "default",
      state: "ready",
      remoteAcknowledged: true,
      revokeRequested: false,
      cleanupPending: false,
      failureCode: null,
      retireAfter: null,
      expiresAt: c.expires_at,
    },
    binding: {
      id: binding.id,
      hostId: HOST,
      executionIdentity: "c4",
      enabled: true,
      canRenew: false,
      state: "ready",
      revision: 1,
      generation: 1,
      currentCredentialId: CID,
      pendingCredentialId: null,
    },
    consumers: [],
  };
  return {
    inventory: { bindings: [binding], credentials: [c], consumers: [] },
    details: { claude: d },
  };
}
function ready(now) {
  return {
    version: 1,
    observedUtc: date(now),
    hubSource: b.PINS.hub,
    routerSource: b.PINS.router,
    holderSource: b.PINS.holder,
    holderBinarySha256: b.PINS.binary,
    piManifestSha256: b.PINS.piManifest,
    profilesSha256: b.PINS.profiles,
    holderId: HOLDER,
    journalId: JOURNAL,
    routerDeploymentId: "11111111-1111-4111-8111-999999999999",
    nativeRecovery: 2,
    nativeStateReady: true,
    activeSessions: 0,
    pendingCommands: 0,
    admissionPaused: false,
    route: {
      alias: "canary-pi-claude",
      account: INPUT.account,
      model: INPUT.model,
      protocol: "anthropic-messages",
      maxAttempts: 1,
      fallbacks: [],
      transport: "sdk-resume",
      cliVersion: "2.1.283",
      warmSessions: false,
      trustVerified: true,
      configRevision: 2883,
      deploymentId: "claude-selected-deployment",
      providerGeneration: 7,
    },
    cold: {
      reportSha256: "1".repeat(64),
      account: INPUT.account,
      source: b.PINS.router,
      passed: true,
    },
    projectionSha256: "2".repeat(64),
  };
}
function branch(ctx, now, native, router) {
  return {
    version: 1,
    observedUtc: date(now),
    hubSource: b.PINS.hub,
    routerSource: b.PINS.router,
    piSource: "2604c41b69c5d9c50a3324545a51f0bfa38435a0",
    ...Object.fromEntries(
      [
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
      ].map((k) => [k, ctx[k]]),
    ),
    native: clone(native),
    router: clone(router),
    routerStoreNamespace: "inference-session-v1",
    routerStoreKey: createHash("sha256")
      .update(
        JSON.stringify([
          ctx.namespace,
          ctx.principal,
          native.conversation,
          router.branch,
        ]),
      )
      .digest("hex"),
    provenance: {
      projectionSha256: "3".repeat(64),
      querySha256: "4".repeat(64),
      databaseIdentity: "fixture-router-db",
      nativeHistorySha256: "5".repeat(64),
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
function phaseServer({
  mutateCheckpoint,
  mutateResponse,
  extraReadyAttempts = 0,
  withRead = false,
  readInput = { limit: 10 },
  settledCancel = false,
  configurationNotice = a.CONFIGURATION_CHANGED_NOTICE,
  mutateFence,
  loseFence,
  failFence,
  meterUsage,
} = {}) {
  const renewal = metadata();
  const projection = {
    profiles: {
      claude: {
        ...b.credential(renewal.inventory, renewal.details.claude, NOW),
        run: INPUT.lifecycleRunId,
      },
    },
  };
  const profiles = projection.profiles;
  const holderId = HOLDER,
    journalId = JOURNAL;
  const clocksWithin5s = true,
    approvedRevision = 1;
  let clock = NOW,
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
    effects = new Map(),
    dispatchOrder = [];
  const managedFence = sourceFixture.transport({
    now: () => clock,
    keyId: KEY,
    tenantId: "00000000-0000-4000-8000-000000000001",
    saves,
    usage: () =>
      meterUsage
        ? meterUsage(attempts, commands)
        : {
            attempts: attempts.reduce((n, r) => n + r.attempts, 0),
            tokenUsageUnknown: [...commands.values()].filter(
              (c) =>
                c.kind === "prompt" && c.cancelled && c.state === "completed",
            ).length,
          },
    mutate: mutateFence,
    lose: loseFence,
    fail: failFence,
    observeCall: (c) => dispatchOrder.push("fence:" + c.method),
  });
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
      totalAttempts: rows.reduce((sum, r) => sum + r.attempts, 0),
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
        denied: a.markerCommand(provider, runId, "denied", profiles),
        effects: a.markerCommand(provider, runId, "effects", profiles),
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
            title: c.communication
              ? "mcp__relay__relay_messages_read"
              : JSON.parse(q.context.description).command,
            ...(s.provider === "codex" ? {} : { toolCallId: c.nativeToolId }),
            status: q.decision.optionId === "yes" ? "completed" : "failed",
          },
        });
      }
      if (
        s.coordination.pending &&
        (!c.cancelled || settledCancel) &&
        !c.refusal
      ) {
        s.coordination.parent = s.coordination.pending;
        delete s.coordination.pending;
        s.routerRecord.state = "complete";
        s.routerRecord.updatedAt = clock;
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
  const runner = b.createClaudePhaseBRunner(
    {
      origin: a.ORIGIN,
      crypto: webcrypto,
      now: () => clock,
      sleep: async (ms) => {
        clock += ms;
      },
      load: () => null,
      save: (state) => saves.push(clone(state)),
      executionDeadline: INPUT.window.expiresAt,
      attemptFence: managedFence.port,
      ask: async (title, instructions, validate, _timeout, context) => {
        checkpoints.push({ title, instructions, context });
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
            marker: observeMarker(
              context.provider,
              context.runId,
              context.phase,
            ),
            coverageConfirmed: true,
            holderReadyConfirmed: true,
            routeConfirmed: true,
            clocksWithin5s,
          };
        else if (context?.kind === "post-open")
          value = {
            marker: observeMarker(
              context.provider,
              context.runId,
              context.phase,
            ),
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
        if (context?.kind === "claude-readiness") value = ready(clock);
        if (context?.kind === "claude-attempt-drain")
          value = {
            version: 1,
            observedAt: clock,
            scope: context.scope,
            completedOperationIds: context.completedOperationIds,
            terminalPrompts: context.terminalPrompts,
            pendingPermissions: [...permissions.values()]
              .filter((p) => p.state === "pending")
              .map((p) => p.id),
            pendingTools: [],
            pendingControls: [...commands.values()]
              .filter((c) => c.state !== "completed")
              .map((c) => c.operation_id),
            nativeRequests: [],
            clientRequests: [],
            provenance: {
              nativeSha256: "6".repeat(64),
              clientSha256: "7".repeat(64),
              operationsSha256: "8".repeat(64),
            },
          };
        if (context?.kind === "claude-branch") {
          const s = sessions.get(context.sessionId);
          value = branch(
            context,
            clock,
            s.coordination,
            context.retiredBranch ? s.retired : s.routerRecord,
          );
        }
        if (mutateCheckpoint)
          value = mutateCheckpoint(context, clone(value)) ?? value;
        return validate(value);
      },
      fetch: async (url, init) => {
        clock += 5;
        const u = new URL(url, a.ORIGIN),
          pathName = u.pathname,
          body = init.body && JSON.parse(init.body);
        dispatchOrder.push(
          "http:" + pathName + ":" + (body?.kind || init.method),
        );
        if (init.method === "POST" && body?.operationId)
          assert(
            saves
              .at(-1)
              ?.operations.some(
                (o) =>
                  o.path === pathName &&
                  o.body?.operationId === body.operationId &&
                  ["sending", "cleanup-dispatch"].includes(o.status),
              ),
            "Durable dispatch intent must precede POST",
          );
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
            Object.keys(profiles).find(
              (k) => profiles[k].id === body.profileId,
            );
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
          s.coordination = previous
            ? clone(previous.coordination)
            : { conversation: id(), branch: id(), nativeSession: native };
          s.routerRecord = previous ? clone(previous.routerRecord) : null;
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
              if (
                req.decision.optionId === "yes" &&
                req.context.title === "Allow bash?"
              ) {
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
              (c) =>
                c.session_id === sid && c.counting && c.state === "accepted",
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
                .map((c) => ({
                  id: c.id,
                  state: c.state,
                  text: c.payload.text,
                })),
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
              if (text === "/relay-reconcile confirmed") {
                s.retired = clone(s.routerRecord);
                s.retired.successor = id();
                s.retired.updatedAt = clock;
                s.coordination = {
                  conversation: s.coordination.conversation,
                  branch: s.retired.successor,
                  nativeSession: s.native,
                  reconciledFrom: s.retired.branch,
                };
              } else if (
                text.startsWith("/relay-branch ") ||
                text === "/relay-compact"
              ) {
                s.coordination = {
                  conversation: s.coordination.conversation,
                  branch: id(),
                  nativeSession: s.native,
                };
                s.routerRecord = null;
              } else if (!text.startsWith("/relay-")) {
                s.coordination.pending = id();
                if (!c.refusal)
                  s.routerRecord = {
                    branch: s.coordination.branch,
                    epoch: id(),
                    operation: s.coordination.pending,
                    state: "uncertain",
                    updatedAt: clock,
                  };
              }
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
                c.nativeToolId = "native-bash-" + c.id;
                for (const event of [
                  { type: "tool_call", title: "bash", status: "pending" },
                  {
                    type: "tool_call_update",
                    title: cmd,
                    status: "in_progress",
                  },
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
                const expected = /^Reply exactly ([A-Z_]+)\./.exec(text);
                assert(expected, "Unexpected prompt " + text);
                c.response = c.refusal ? configurationNotice : expected[1];
              }
              if (withRead && text === b.TEXT.ready) {
                const q = detail({
                  id: id(),
                  session_id: sid,
                  expires_at: date(clock + 30000),
                });
                q.context.title = "Allow mcp__relay__relay_messages_read?";
                q.context.description = JSON.stringify(readInput);
                permissions.set(q.id, q);
                c.permission = q.id;
                c.communication = true;
                c.nativeToolId = "native-read-" + c.id;
                for (const status of ["pending", "in_progress"])
                  s.events.push({
                    sequence: String(++sequence),
                    createdAt: date(clock),
                    event: {
                      kind: "update",
                      type:
                        status === "pending" ? "tool_call" : "tool_call_update",
                      title: "mcp__relay__relay_messages_read",
                      status,
                      toolCallId: c.nativeToolId,
                    },
                  });
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
                  attempts:
                    text === b.TEXT.ready
                      ? 1 + extraReadyAttempts + (withRead ? 1 : 0)
                      : 1,
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
            throw Error(
              "Unimplemented endpoint " + pathName + " " + init.method,
            );
        }
        // Browser receives serialized values; no aliasing mutable server state.
        if (mutateResponse)
          result =
            mutateResponse(pathName, init.method, clone(result)) ?? result;
        const raw = JSON.stringify(result);
        return {
          ok: true,
          status: init.method === "POST" ? 202 : 200,
          headers: { get: () => "application/json" },
          text: async () => raw,
        };
      },
    },
    INPUT,
  );
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
    managedFence,
    dispatchOrder,
    advance: (ms) => {
      clock += ms;
    },
    setTrajectorySince: (since) => {
      trajectorySince = since;
    },
  };
}

test("synthetic source-pin fixture: actual public Claude entry fences lifecycle/recovery/effort without modifying A", async () => {
  const original = clone(a.PROFILES),
    f = phaseServer();
  await f.runner.start();
  assert.equal(f.runner.state.status, "phase-b-passed");
  if (process.env.PHASE_B_FENCE_EVIDENCE) {
    fs.writeFileSync(
      path.join(
        process.env.PHASE_B_FENCE_EVIDENCE,
        "synthetic-contract-transcript.json",
      ),
      JSON.stringify(
        {
          sourceProof,
          sourceSchemaSha256:
            "79316b299abc9b94262de060dda408c2e4f9f419a63f40731b148aaf02af22ee",
          requests: f.managedFence.calls.map((c) => c.body),
          receipts: [...f.managedFence.operations.values()],
        },
        null,
        2,
      ) + "\n",
      { flag: "wx" },
    );
  }
  assert.equal(require("./owner-phase-b.js").QUALIFIED_FENCE_ROUTER, null);
  assert.equal(sourceProof.qualifiedLiveBundle, false);
  assert.equal(sourceProof.files.length, 2);
  const phases = f.runner.state.attemptFence.phases;
  assert.equal(phases.length, 2);
  assert.deepEqual(
    phases.map((p) => p.rows.map((r) => r.maximum)),
    [
      [2, 3, 3, 2, 2, 2, 2, 2],
      [2, 2],
    ],
  );
  assert(phases.every((p) => p.closed && p.rows.every((r) => r.settled)));
  const mutations = f.managedFence.calls.filter((c) =>
    ["arm", "advance", "close"].includes(c.method),
  );
  assert.deepEqual(
    mutations.map((c) => c.method),
    ["arm", ...Array(7).fill("advance"), "close", "arm", "advance", "close"],
  );
  const arms = mutations.filter((c) => c.method === "arm");
  assert.deepEqual(
    arms.map((c) => c.body.phaseLimit),
    [20, 6],
  );
  assert.equal(arms[1].body.predecessorFenceId, arms[0].body.fenceId);
  assert.equal(
    arms[1].body.expectedUnknownUsage,
    1,
    "cancelled known admission retains unknown token usage",
  );
  assert.equal(f.runner.state.completed, false);
  assert.equal(f.runner.state.phaseACompleted, undefined);
  assert.deepEqual(a.PROFILES, original);
  const handoffs = f.checkpoints.filter(
    (v) => v.context?.kind === "pre-launch",
  );
  assert.equal(handoffs.length, 2);
  for (const checkpoint of handoffs) {
    assert.equal(checkpoint.context.provider, "claude");
    assert(
      checkpoint.instructions.includes(
        `Claude route alias canary-pi-claude, selected account ${INPUT.account}, model ${INPUT.model}.`,
      ),
    );
    assert.doesNotMatch(checkpoint.instructions, /canary-pi-codex|gpt-6-astra/);
  }
  assert.equal(f.launches.length, 3);
  const chronological = [...f.launches].reverse();
  assert.equal(
    chronological[1].receipt.result.nativeSessionId,
    chronological[0].receipt.result.nativeSessionId,
  );
  assert.notEqual(
    chronological[2].receipt.result.nativeSessionId,
    chronological[1].receipt.result.nativeSessionId,
  );
  assert(f.sessions.values().every((s) => s.state === "closed" && !s.lease));
  assert.deepEqual(
    f.runner.state.runs.map((r) => r.prompts),
    [11, 4],
  );
  assert(
    f.runner.state.runs.every((r) =>
      r.reservations.every(
        (x) => x.state === "settled" && x.attempts <= x.maximum,
      ),
    ),
  );
  const promptTexts = f.calls
    .filter((c) => c.body?.kind === "prompt")
    .map((c) => c.body.text);
  assert.equal(promptTexts.length, 15);
  assert.equal(promptTexts[0], b.TEXT.ready);
  assert.equal(promptTexts[9], "/relay-reconcile confirmed");
  assert.deepEqual(promptTexts.slice(-4), [
    b.TEXT.low,
    b.TEXT.highProbe,
    "/relay-reconcile confirmed",
    b.TEXT.high,
  ]);
  for (const call of f.calls.filter(
    (c) => c.method === "POST" && c.body?.operationId,
  ))
    assert(
      f.saves.some((s) =>
        s.operations.some(
          (o) =>
            o.path === call.path &&
            o.body?.operationId === call.body.operationId &&
            o.status === "sending",
        ),
      ),
      "Intent saved before dispatch",
    );
  assert.equal(f.runner.state.successorCredentials, undefined);
  assert.equal(
    f.calls.filter(
      (c) => c.method === "POST" && c.path === "/api/router-credentials",
    ).length,
    0,
  );
  await assert.rejects(f.runner.start(), /Fresh exclusive/);
});
test("public entry refuses wrong cold account before launch and retains stopped evidence", async () => {
  const f = phaseServer({
    mutateCheckpoint: (ctx, v) => {
      if (ctx.kind === "claude-readiness") v.cold.account = "different";
      return v;
    },
  });
  await assert.rejects(f.runner.start(), /Selected account/);
  assert.equal(f.calls.filter((c) => c.method === "POST").length, 0);
  assert.equal(f.runner.state.status, "stopped");
});
test("missing or mismatched branch evidence stops recovery without unknown-effect acknowledgement", async () => {
  for (const change of [
    (v) => {
      delete v.router;
    },
    (v) => {
      v.router.operation = "wrong-turn";
    },
    (v) => {
      v.native.pending = undefined;
      v.router.state = "uncertain";
    },
  ]) {
    const f = phaseServer({
      mutateCheckpoint: (ctx, v) => {
        if (ctx.kind === "claude-branch") change(v);
        return v;
      },
    });
    await assert.rejects(f.runner.start(), /Router branch|Native and router/);
    assert.equal(f.launches.length, 1);
    assert(!f.calls.some((c) => c.path.endsWith("/resume")));
    assert(!f.calls.some((c) => c.body?.acknowledgeUnknownToolEffects));
  }
});
test("imported native history cannot continue after the retained Router branch changes", async () => {
  for (const change of [
    (v, prior) => {
      v.router.epoch = prior.router.epoch + "-changed";
    },
    (v, prior) => {
      v.router.updatedAt = prior.router.updatedAt - 1;
    },
  ]) {
    let prior,
      observations = 0,
      callsAtChange;
    const f = phaseServer({
      mutateCheckpoint: (ctx, value) => {
        if (ctx.kind === "claude-branch" && !ctx.retiredBranch) {
          observations++;
          if (observations === 1) prior = clone(value);
          if (observations === 2) {
            change(value, prior);
            callsAtChange = f.calls.length;
          }
        }
        return value;
      },
    });
    await assert.rejects(f.runner.start(), /Imported Router branch changed/);
    assert.equal(observations, 2);
    assert.equal(f.launches.length, 2);
    assert.equal(
      f.calls.filter((call) => call.path.endsWith("/resume")).length,
      1,
    );
    assert(
      !f.calls
        .slice(callsAtChange)
        .some((call) => call.method === "POST" && call.body?.kind === "prompt"),
    );
    assert(!f.calls.some((call) => call.body?.acknowledgeUnknownToolEffects));
    assert.equal(f.runner.state.status, "stopped");
  }
});

test("generic effort failure never permits reconciliation/high successor", async () => {
  const f = phaseServer({ configurationNotice: "Backend unavailable" });
  await assert.rejects(f.runner.start(), /Specific configuration/);
  const texts = f.calls
    .filter((c) => c.body?.kind === "prompt")
    .map((c) => c.body.text);
  assert.equal(texts.at(-1), b.TEXT.highProbe);
  assert(!texts.includes(b.TEXT.high));
});
test("credential validation rejects malformed inventory, active work and cross-principal detail", () => {
  const fixture = metadata();
  assert.equal(
    b.credential(fixture.inventory, fixture.details.claude, NOW).generation,
    1,
  );
  for (const mutate of [
    (f) => f.inventory.consumers.push({ state: "active" }),
    (f) =>
      f.inventory.consumers.push({
        binding_id: b.PROFILE.binding,
        state: "active",
      }),
    (f) => (f.details.claude.credential.routerKeyId = "key_bad"),
    (f) => f.inventory.bindings[0].grant_spec.access.push("local"),
    (f) => (f.details.claude.binding.canRenew = true),
  ]) {
    const f = metadata();
    mutate(f);
    assert.throws(() => b.credential(f.inventory, f.details.claude, NOW));
  }
});
test("branch projection checks stale scope, active turns and exact retirement, not booleans", () => {
  const ctx = {
    sessionId: SID,
    generation: 1,
    tenantId: "00000000-0000-4000-8000-000000000001",
    bindingId: b.PROFILE.binding,
    nativeSessionId: ID(10),
    holderId: HOLDER,
    journalId: JOURNAL,
    principal: "key:" + KEY,
    namespace: "default",
    profileId: b.PROFILE.id,
    notBefore: NOW,
  };
  const n = {
    conversation: "conversation",
    branch: "branch",
    nativeSession: ctx.nativeSessionId,
    parent: "parent",
    pending: "pending",
  };
  const r = {
    branch: "branch",
    epoch: "epoch",
    operation: "pending",
    state: "uncertain",
    updatedAt: NOW,
  };
  const v = branch(ctx, NOW, n, r),
    p = b.branchEvidence(v, ctx, NOW);
  assert.equal(p.requiresReconcile, true);
  for (const mutate of [
    (x) => (x.principal = "key:other"),
    (x) => (x.observedUtc = date(NOW - 1)),
    (x) => (x.router.state = "running"),
    (x) => (x.router.successor = "unobserved"),
    (x) => delete x.provenance.querySha256,
  ]) {
    const x = clone(v);
    mutate(x);
    assert.throws(() => b.branchEvidence(x, ctx, NOW));
  }
  const next = branch(
    ctx,
    NOW,
    {
      conversation: "conversation",
      branch: "next",
      nativeSession: ctx.nativeSessionId,
      reconciledFrom: "branch",
    },
    { ...r, successor: "next" },
  );
  assert.equal(b.branchEvidence(next, ctx, NOW, p).native.branch, "next");
  next.router.epoch = "different";
  assert.throws(() => b.branchEvidence(next, ctx, NOW, p), /Reconstruction/);
  assert.throws(() => b.branchEvidence(true, ctx, NOW));
});

test("an impossible over-admitted synthetic meter stops after first prompt and does not claim a qualified cap", async () => {
  const f = phaseServer({ extraReadyAttempts: 2 });
  await assert.rejects(
    f.runner.start(),
    /admitted row\/phase ceiling exceeded/,
  );
  assert.equal(f.calls.filter((c) => c.body?.kind === "prompt").length, 1);
  assert.equal(f.runner.state.runs[0].reservations[0].state, "reserved");
  assert.match(f.runner.state.accounting, /publication held/);
  assert(
    f.sessions.values().every((s) => s.state === "closed"),
    JSON.stringify(f.runner.state.evidence.filter((e) => e.kind === "cleanup")),
  );
});
test("complete imported receipt continues without unnecessary reconciliation", async () => {
  const f = phaseServer({ settledCancel: true });
  await f.runner.start();
  assert.equal(f.runner.state.runs[0].prompts, 10);
  assert.equal(
    f.calls.filter((c) => c.body?.text === "/relay-reconcile confirmed").length,
    1,
  );
});
test("wrong approved profile revision and changed delivery stop before inference", async () => {
  for (const mutateResponse of [
    (p, m, v) => {
      if (p === "/api/agent-profiles") v.forEach((x) => (x.revision = 3));
      return v;
    },
    (p, m, v) => {
      if (p.endsWith("/router")) v.consumer.credentialState = "installing";
      return v;
    },
  ]) {
    const f = phaseServer({ mutateResponse });
    await assert.rejects(f.runner.start());
    assert.equal(f.calls.filter((c) => c.body?.kind === "prompt").length, 0);
  }
});
test("expired assigned window refuses construction and cannot reset a saved report", async () => {
  assert.throws(() =>
    b.inputs({ ...INPUT, window: { startsAt: NOW - 1, expiresAt: NOW } }, NOW),
  );
  const env = {
    crypto: webcrypto,
    origin: a.ORIGIN,
    now: () => NOW,
    load: () => "preserved-existing-report",
    save: () => {
      throw Error("must not save");
    },
  };
  const r = b.createClaudePhaseBRunner(env, INPUT);
  await assert.rejects(r.start(), /Fresh exclusive/);
  assert.throws(() => r.replaceStopped(), /cannot replace/);
  assert.throws(() => r.revokeFinal(), /separate/);
});

test("public Claude path accounts one scoped read independently of marker permissions", async () => {
  const f = phaseServer({ withRead: true });
  await f.runner.start();
  const read = f.runner.state.evidence.filter(
    (e) => e.purpose === "relay-message-read",
  );
  assert.equal(read.length, 1);
  assert.equal(read[0].kind, "allow_once");
  assert.equal(f.runner.state.runs[0].reservations[0].attempts, 2);
  assert.equal(f.effects.get(INPUT.lifecycleRunId), 1);
  const decisions = f.calls.filter(
    (c) =>
      c.path.endsWith("/decision") && c.body?.outcome.outcome === "selected",
  );
  assert.equal(decisions.length, 3);
});
test("out of scope read never receives allow-once and no marker follows", async () => {
  const f = phaseServer({ withRead: true, readInput: { channelId: ID(99) } });
  await assert.rejects(f.runner.start(), /outside the approved/);
  assert(
    !f.calls.some(
      (c) =>
        c.path.endsWith("/decision") && c.body?.outcome.outcome === "selected",
    ),
  );
  assert.equal(f.effects.size, 0);
});
test("reservation refuses before dispatch when remaining phase budget cannot fit continuation", async () => {
  let posts = 0;
  const r = b.createClaudePhaseBRunner(
    {
      crypto: webcrypto,
      origin: a.ORIGIN,
      now: () => NOW,
      load: () => null,
      save: () => {},
      fetch: () => {
        posts++;
        throw Error("must not dispatch");
      },
    },
    INPUT,
  );
  r.run = {
    effort: false,
    deadline: NOW + 10000,
    prompts: 0,
    lastSnapshot: { totalAttempts: 19 },
    firstSnapshot: { totalAttempts: 0 },
  };
  await assert.rejects(r.prompt(b.TEXT.ready), /exceeds remaining budget/);
  assert.equal(posts, 0);
  assert.equal(r.run.prompts, 0);
  assert.equal(r.reservation, undefined);
});

test("unchanged public bundle refuses missing port and unqualified release before any launch or observation", async () => {
  const production = require("./owner-phase-b.js");
  for (const withPort of [false, true]) {
    let io = 0;
    const blocked = () => {
      io++;
      throw Error("must not dispatch");
    };
    const runner = production.createClaudePhaseBRunner(
      {
        origin: a.ORIGIN,
        crypto: webcrypto,
        now: () => NOW,
        load: () => null,
        save: () => {},
        executionDeadline: NOW + 10000,
        fetch: blocked,
        ask: blocked,
        ...(withPort
          ? {
              attemptFence: Object.fromEntries(
                ["inspect", "arm", "advance", "close", "readOperation"].map(
                  (k) => [k, blocked],
                ),
              ),
            }
          : {}),
      },
      INPUT,
    );
    await assert.rejects(
      runner.start(),
      withPort ? /release is not assigned/ : /port missing/,
    );
    assert.equal(io, 0);
    assert.equal(runner.state.status, "stopped");
  }
});

test("signed inspection rejects wrong source, key, coverage, freshness and extra fields before launch", async () => {
  const { digest } = require("./owner-phase-b-fence.cjs");
  const changes = [
    (v) => {
      v.routerSource = "1".repeat(40);
    },
    (v) => {
      v.observedAt = NOW - 1;
    },
    (v) => {
      v.response.keyId = "key_aaaaaaaaaaaa";
    },
    (v) => {
      v.response.coveredFrom--;
    },
    (v) => {
      v.response.staleOpenRequests = 1;
    },
    (v) => {
      v.response.unattributed = 1;
    },
    (v) => {
      v.response.tokenUsageUnknown = 1;
    },
    (v) => {
      v.response.unrecognized = true;
    },
  ];
  for (const change of changes) {
    const f = phaseServer({
      mutateFence: (v) => {
        change(v);
        v.projectionSha256 = digest(v.response);
        return v;
      },
    });
    await assert.rejects(f.runner.start(), /Attempt fence:/);
    assert.equal(f.launches.length, 0);
    assert.equal(f.calls.filter((c) => c.method === "POST").length, 0);
  }
});

test("full arm receipt binds plan, principal, generation and digest before first prompt", async () => {
  const { digest } = require("./owner-phase-b-fence.cjs");
  for (const change of [
    (r) => {
      r.requestDigest = "1".repeat(64);
    },
    (r) => {
      r.fence.plan.selection.providerGeneration++;
    },
    (r) => {
      r.fence.plan.principalId = "key:key_aaaaaaaaaaaa";
    },
    (r) => {
      r.fence.plan.phaseLimit++;
    },
    (r) => {
      r.fence.plan.rows[0].limit++;
    },
    (r) => {
      r.fence.tenant = ID(99);
    },
    (r) => {
      r.attempts++;
    },
    (r) => {
      r.tokenUsageUnknown++;
    },
  ]) {
    const f = phaseServer({
      mutateFence: (v, c) => {
        if (c.method === "arm") {
          change(v.response);
          v.projectionSha256 = digest(v.response);
        }
        return v;
      },
    });
    await assert.rejects(f.runner.start(), /Attempt fence:/);
    assert.equal(f.calls.filter((c) => c.body?.kind === "prompt").length, 0);
    assert.equal(
      f.managedFence.calls.filter((c) => c.method === "arm").length,
      1,
    );
    assert(f.sessions.values().every((s) => s.state === "closed"));
  }
});

test("missing native/client drain fails despite zero Router opens and closes fence before Relay cleanup", async () => {
  let observations = 0;
  const f = phaseServer({
    mutateCheckpoint: (ctx, value) => {
      if (ctx.kind === "claude-attempt-drain" && ++observations === 2)
        value.clientRequests = ["unproved-late-request"];
      return value;
    },
  });
  await assert.rejects(f.runner.start(), /native\/client work remains/);
  assert.equal(
    f.managedFence.calls.filter((c) => c.method === "advance").length,
    0,
  );
  const fenceClose = f.dispatchOrder.indexOf("fence:close");
  const relayClose = f.dispatchOrder.findIndex(
    (x) => x.startsWith("http:") && x.endsWith(":close"),
  );
  assert(fenceClose >= 0 && relayClose > fenceClose);
  assert.equal(f.calls.filter((c) => c.body?.kind === "prompt").length, 1);
  assert(f.sessions.values().every((s) => s.state === "closed"));
});

for (const method of ["arm", "advance", "close"])
  test(`lost committed ${method} is resolved only by original-operation read, never mutation resend`, async () => {
    let lost = false;
    const f = phaseServer({
      loseFence: (called) => called === method && !lost && (lost = true),
    });
    await f.runner.start();
    assert.equal(f.runner.state.status, "phase-b-passed");
    const lookup = f.managedFence.calls.filter(
      (c) => c.method === "readOperation",
    );
    const arm = f.managedFence.calls.find((c) => c.method === method);
    assert.equal(lookup.length, 1);
    assert.equal(lookup[0].body.targetOperationId, arm.body.operationId);
    assert.notEqual(lookup[0].body.operationId, arm.body.operationId);
    assert.equal(lookup[0].request.deadline, arm.request.deadline);
    assert.equal(
      f.managedFence.calls.filter((c) => c.method === "arm").length,
      2,
      "one arm for each fixed phase",
    );
    assert(
      f.runner.state.attemptFence.operations.every(
        (o) => o.status === "committed",
      ),
    );
  });

test("unresolved arm stops before prompt; failed fence close still permits ordinary cleanup", async () => {
  const unresolved = phaseServer({
    loseFence: (m) => m === "arm",
    failFence: (m) => m === "readOperation",
  });
  await assert.rejects(unresolved.runner.start(), /unresolved/);
  assert.equal(
    unresolved.calls.filter((c) => c.body?.kind === "prompt").length,
    0,
  );
  assert.equal(
    unresolved.managedFence.calls.filter((c) => c.method === "arm").length,
    1,
  );
  assert(unresolved.sessions.values().every((s) => s.state === "closed"));
  const closeFailure = phaseServer({
    mutateCheckpoint: (ctx, v) => {
      if (ctx.kind === "observation" && ctx.phase === "denied")
        v.marker.files.denied.state = "present";
      return v;
    },
    failFence: (m) => m === "close" || m === "readOperation",
  });
  await assert.rejects(closeFailure.runner.start());
  assert.equal(
    closeFailure.managedFence.calls.filter((c) => c.method === "close").length,
    1,
  );
  assert(closeFailure.sessions.values().every((s) => s.state === "closed"));
  assert(
    closeFailure.runner.state.evidence.some(
      (e) =>
        e.kind === "attempt-fence-cleanup" && e.status.startsWith("unresolved"),
    ),
  );
});

test("raw denied N+1 remains visible and fails phase although only two admissions are charged", async () => {
  const f = phaseServer({
    extraReadyAttempts: 2,
    meterUsage: (rows) => ({
      attempts: Math.min(
        2,
        rows.reduce((n, r) => n + r.attempts, 0),
      ),
    }),
  });
  await assert.rejects(f.runner.start(), /raw trajectory\/admission mismatch/);
  assert.equal(f.runner.state.runs[0].lastSnapshot.totalAttempts, 3);
  assert.equal(f.managedFence.current().closedAttempts, 2);
  assert.equal(
    f.managedFence.calls.filter((c) => c.method === "advance").length,
    0,
  );
  assert.equal(f.calls.filter((c) => c.body?.kind === "prompt").length, 1);
});
