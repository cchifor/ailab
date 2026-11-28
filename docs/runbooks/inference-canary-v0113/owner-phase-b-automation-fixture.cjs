"use strict";
// Synthetic service/collector adapted from the reviewed B public-entry fixture.
// No Runner instance, virtual clock, manual renew or test import lives here.
const assert = require("node:assert/strict");
const http = require("node:http");
const { setTimeout: delay } = require("node:timers/promises");
const { createHash } = require("node:crypto");
const fs = require("node:fs"),
  path = require("node:path");
const sourceFixture = require("./owner-phase-b-fence-fixture.cjs");
const { a, b } = sourceFixture.sourceFixture();
const NOW = Date.now();
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
function phaseServer(outputDirectory) {
  const extraReadyAttempts = 0,
    withRead = false,
    readInput = { limit: 10 },
    settledCancel = false;
  const configurationNotice = a.CONFIGURATION_CHANGED_NOTICE;
  const heartbeats = [];
  let heartbeatWaited = false;
  function latestSnapshot() {
    const names = fs
      .readdirSync(outputDirectory)
      .filter((n) => /^state-\d+\.json$/.test(n))
      .sort();
    assert(names.length, "Durable state must exist before dispatch");
    return JSON.parse(
      fs.readFileSync(path.join(outputDirectory, names.at(-1)), "utf8"),
    );
  }
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
  const approvedRevision = 1;
  const trajectorySince = NOW;
  let serial = 100,
    sequence = 0;
  const sessions = new Map(),
    launches = [],
    commands = new Map(),
    permissions = new Map();
  const calls = [],
    checkpoints = [],
    attempts = [],
    consumerReads = [],
    effects = new Map();
  const managedFence = sourceFixture.transport({
    now: Date.now,
    keyId: KEY,
    tenantId: "00000000-0000-4000-8000-000000000001",
    // Read the actual fsynced host snapshot at each signer boundary.
    saves: { at: () => latestSnapshot() },
    usage: () => ({
      attempts: attempts.reduce((n, row) => n + row.attempts, 0),
      tokenUsageUnknown: [...commands.values()].filter(
        (c) => c.kind === "prompt" && c.cancelled && c.state === "completed",
      ).length,
    }),
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
        created_at: date(Date.now()),
        last_used_at: date(Date.now()),
        lease_expires_at: date(Date.now() + 60000),
        ended_at: s.state === "closed" ? date(Date.now()) : null,
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
      requestedAt: Date.now(),
      observedAt: Date.now() + 1,
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
      observedUtc: date(Date.now()),
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
      createdAt: date(Date.now()),
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
          createdAt: date(Date.now()),
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
        s.routerRecord.updatedAt = Date.now();
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
  async function collectCheckpoint({ context, signal }) {
    assert.equal(context.version, 1);
    assert(Object.isFrozen(context));
    checkpoints.push(clone(context));
    // Duration timers may wake before the required wall-clock boundary.
    while (context.notBefore && Date.now() < context.notBefore)
      await delay(Math.max(1, context.notBefore - Date.now() + 2), undefined, {
        signal,
      });
    switch (context.kind) {
      case "claude-readiness":
        assert.equal(context.account, INPUT.account);
        assert.equal(context.model, INPUT.model);
        assert.equal(sessions.size, 0);
        return ready(Date.now());
      case "pre-launch":
        assert.equal(context.provider, "claude");
        assert([...sessions.values()].every((s) => s.state === "closed"));
        return {
          trajectory: observeTrajectory("claude"),
          marker: observeMarker("claude", context.runId, "baseline"),
          coverageConfirmed: true,
          holderReadyConfirmed: true,
          routeConfirmed: true,
          clocksWithin5s: true,
        };
      case "post-open":
        assert.equal(sessions.get(context.sessionId).state, "open");
        return {
          marker: observeMarker("claude", context.runId, "baseline"),
          deliveryConfirmed: true,
        };
      case "observation":
        return {
          trajectory: observeTrajectory("claude"),
          coverageConfirmed: true,
          ...(context.phase
            ? { marker: observeMarker("claude", context.runId, context.phase) }
            : {}),
        };
      case "permission": {
        assert.equal(context.purpose, "marker");
        assert(permissions.has(context.permission.id));
        if (!heartbeatWaited) {
          heartbeatWaited = true;
          const since = heartbeats.length,
            end = Date.now() + 25000;
          while (
            !heartbeats
              .slice(since)
              .some((h) => h.sessionId === context.sessionId)
          ) {
            assert(
              Date.now() < end,
              "Actual 20s host timer must renew the held lease",
            );
            await delay(25, undefined, { signal });
          }
        }
        return true;
      }
      case "claude-branch": {
        const s = sessions.get(context.sessionId);
        assert(s);
        assert.equal(context.generation, 1);
        assert.equal(context.nativeSessionId, s.native);
        assert.equal(context.holderId, HOLDER);
        assert.equal(context.journalId, JOURNAL);
        assert.equal(context.principal, "key:" + KEY);
        assert.equal(context.bindingId, b.PROFILE.binding);
        assert.equal(context.profileId, b.PROFILE.id);
        return branch(
          context,
          Date.now(),
          s.coordination,
          context.retiredBranch ? s.retired : s.routerRecord,
        );
      }
      case "claude-attempt-drain": {
        const s = sessions.get(context.scope.sessionId);
        assert(s);
        assert.equal(context.scope.nativeSessionId, s.native);
        assert.equal(context.scope.routerSource, b.PINS.router);
        assert.equal(
          context.scope.routerDeploymentId,
          managedFence.deploymentId,
        );
        const owned = [...commands.values()].filter(
          (c) => c.session_id === s.id,
        );
        assert.deepEqual(
          owned.map((c) => c.operation_id),
          context.completedOperationIds,
        );
        const terminalPrompts = owned
          .filter((c) => c.kind === "prompt" && c.state === "completed")
          .map((c) => ({
            operationId: c.operation_id,
            stopReason: c.receipt.result.stopReason,
          }));
        assert.deepEqual(terminalPrompts, context.terminalPrompts);
        return {
          version: 1,
          observedAt: Date.now(),
          scope: context.scope,
          completedOperationIds: owned
            .filter((c) => c.state === "completed")
            .map((c) => c.operation_id),
          terminalPrompts,
          pendingPermissions: [...permissions.values()]
            .filter((p) => p.session_id === s.id && p.state === "pending")
            .map((p) => p.id),
          pendingTools: [],
          pendingControls: owned
            .filter((c) => c.state !== "completed")
            .map((c) => c.operation_id),
          nativeRequests: owned
            .filter((c) => c.kind === "prompt" && c.state !== "completed")
            .map((c) => c.operation_id),
          clientRequests: [],
          provenance: {
            nativeSha256: "6".repeat(64),
            clientSha256: "7".repeat(64),
            operationsSha256: "8".repeat(64),
          },
        };
      }
      default:
        throw Error("Unknown B checkpoint " + context.kind);
    }
  }
  async function dispatch(url, init) {
    const u = new URL(url, a.ORIGIN),
      pathName = u.pathname,
      body = init.body && JSON.parse(init.body);
    if (init.method === "POST" && body?.operationId)
      assert(
        latestSnapshot()?.operations.some(
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
        expires_at: date(Date.now() + 120000),
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
            leaseExpiresAt: date(Date.now() + 60000),
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
          if (body.leaseId) {
            assert.equal(s.state, "open");
            assert.equal(s.lease.id, body.leaseId);
            heartbeats.push({
              sessionId: sid,
              leaseId: body.leaseId,
              at: Date.now(),
            });
          }
          s.lease = {
            id: body.leaseId || id(),
            generation: 1,
            expires_at: date(Date.now() + 60000),
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
            s.retired.updatedAt = Date.now();
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
                updatedAt: Date.now(),
              };
          }
          if (Object.hasOwn(fixed, text)) c.response = fixed[text];
          else if (text.startsWith("Request permission")) {
            const cmd = text.split("COMMAND: ")[1];
            assert(cmd);
            const q = detail({
              id: id(),
              session_id: sid,
              expires_at: date(Date.now() + 120000),
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
                createdAt: date(Date.now()),
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
              expires_at: date(Date.now() + 30000),
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
                createdAt: date(Date.now()),
                event: {
                  kind: "update",
                  type: status === "pending" ? "tool_call" : "tool_call_update",
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
              startedAt: Date.now(),
              endedAt: Date.now() + 1,
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
        throw Error("Unimplemented endpoint " + pathName + " " + init.method);
    }

    return { status: init.method === "POST" ? 202 : 200, value: clone(result) };
  }
  return {
    dispatch,
    collectCheckpoint,
    heartbeats,
    calls,
    sessions,
    launches,
    commands,
    permissions,
    checkpoints,
    effects,
    attempts,
    consumerReads,
    managedFence,
    attemptFence: managedFence.port,
  };
}

async function createFixture(directory, outputDirectory) {
  const phase = phaseServer(outputDirectory),
    failures = [];
  const socketPath = path.join(directory, "phase-b.sock");
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 65536) req.destroy();
    });
    req.on("end", async () => {
      try {
        assert.equal(req.headers.cookie, undefined);
        assert.equal(req.headers.authorization, undefined);
        assert(
          !/\/(rotate|revoke)$/.test(req.url),
          "B must never mutate grants",
        );
        const result = await phase.dispatch(req.url, {
          method: req.method,
          body: raw || undefined,
        });
        res.writeHead(result.status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(result.value));
      } catch (e) {
        failures.push(e.stack);
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "synthetic B service assertion" }));
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  fs.chmodSync(socketPath, 0o600);
  // Explicit synthetic completed-A structure, NOT an authenticated execution receipt.
  const phaseARaw =
    JSON.stringify(
      {
        version: 1,
        status: "phase-a-passed",
        phaseACompleted: true,
        replacement: { carriedPrompts: 1, carriedAttempts: 2 },
        predecessor: { raw: "synthetic-original-accounting-only" },
        runs: [
          ["codex", false],
          ["qwen", false],
          ["codex", true],
        ].map(([provider, effort]) => ({
          provider,
          effort,
          status: "passed-with-live-wire-unobserved",
        })),
      },
      null,
      2,
    ) + "\n";
  return {
    ...phase,
    socketPath,
    failures,
    phaseARaw,
    inputs: clone(INPUT),
    close: async () => {
      server.closeAllConnections();
      await new Promise((r) => server.close(r));
    },
  };
}
module.exports = { createFixture };
