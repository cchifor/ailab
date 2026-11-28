"use strict";
const test = require("node:test"),
  assert = require("node:assert/strict");
const fs = require("node:fs"),
  path = require("node:path"),
  os = require("node:os"),
  http = require("node:http"),
  Module = require("node:module");
const { createHash } = require("node:crypto");
const a = require("./owner-phase-a.js"),
  b = require("./owner-phase-b.js");
const production = require("./owner-finalization.cjs");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const hash = (v) => createHash("sha256").update(v).digest("hex"),
  clone = (v) => structuredClone(v);
const q = {
  // Synthetic cleanup deployments differ from every immutable historical pin.
  hubSource: "c".repeat(40),
  routerSource: "d".repeat(40),
  reportHubSources: { a: a.RUN_SOURCE, b: a.RUN_SOURCE, c: a.RUN_SOURCE },
  reportRouterSources: {
    a: a.ROUTER,
    b: "0bc835ab72fb9ea03b2b3ea51bba7a9789a5e171",
    c: "0bc835ab72fb9ea03b2b3ea51bba7a9789a5e171",
  },
  observerQualificationSha256: "a".repeat(64),
  activeDrainReportSources: {
    hubSource: "e".repeat(40),
    routerSource: "f".repeat(40),
  },
};
// Explicit synthetic qualification: change ONE source pin only, no Runner or
// validator overrides. Host/CLI bytes are unchanged; dependencies are private.
const proof = [];
function load(file, bindings = {}, substitution) {
  const filename = path.join(__dirname, file),
    before = fs.readFileSync(filename, "utf8");
  let after = before;
  if (substitution) {
    assert.equal(before.split(substitution[0]).length, 2);
    after = before.replace(...substitution);
  }
  const m = new Module(filename, module);
  m.filename = filename;
  m.paths = Module._nodeModulePaths(__dirname);
  const original = m.require.bind(m);
  m.require = (name) =>
    Object.hasOwn(bindings, name) ? bindings[name] : original(name);
  m._compile(after, filename);
  proof.push({
    file,
    beforeSha256: hash(before),
    afterSha256: hash(after),
    substitution: substitution || null,
    liveQualified: false,
  });
  return m.exports;
}
const candidate = load("owner-finalization.cjs", {}, [
  "const QUALIFIED_FINALIZATION = null;",
  "const QUALIFIED_FINALIZATION = " + JSON.stringify(q) + ";",
]);
const host = load("owner-finalization-automation.cjs", {
  "./owner-finalization.cjs": candidate,
});
const cli = load("owner-finalization-cli.cjs", {
  "./owner-finalization.cjs": candidate,
  "./owner-finalization-automation.cjs": host,
});

function fixture({
  live = false,
  activeDrain = false,
  profiles: suppliedProfiles,
  now: fixtureNow,
} = {}) {
  let clock = fixtureNow ?? Date.now(),
    seq = 10;
  const now = () => (live ? Date.now() : clock),
    date = (v = now()) => new Date(v).toISOString(),
    id = () => `11111111-1111-4111-8111-${String(seq++).padStart(12, "0")}`;
  const profiles = {
    codex: {
      ...a.PROFILES.codex,
      credential: id(),
      key: "key_" + "1".repeat(24),
      generation: 2,
      revision: 3,
      expiresAt: now() - 1,
    },
    qwen: {
      ...a.PROFILES.qwen,
      credential: id(),
      key: "key_" + "2".repeat(24),
      generation: 2,
      revision: 3,
      expiresAt: now() - 1,
    },
    claude: {
      ...b.PROFILE,
      credential: id(),
      key: "key_" + "3".repeat(24),
      generation: 1,
      revision: 1,
      expiresAt: now() - 1,
    },
  };
  // Test-only bridge for real-PG integration: actual issued metadata replaces
  // synthetic current generations before reports are built. Fixed profile and
  // original stopped-predecessor identities still pass the production parser.
  if (suppliedProfiles) {
    assert.deepEqual(Object.keys(suppliedProfiles).sort(), [
      "claude",
      "codex",
      "qwen",
    ]);
    for (const provider of Object.keys(profiles))
      profiles[provider] = {
        ...profiles[provider],
        ...clone(suppliedProfiles[provider]),
      };
  }
  function marker(provider, runId, phase = "recovered") {
    return {
      version: 1,
      provider,
      profileId: profiles[provider].id,
      runId,
      phase,
      state: "observed",
      phaseChecksPass: true,
      observedUtc: date(),
      commands: Object.fromEntries(
        ["denied", "effects"].map((k) => [
          k,
          a.markerCommand(provider, runId, k, profiles),
        ]),
      ),
      files: {
        denied: { state: "absent" },
        effects:
          phase === "baseline"
            ? { state: "absent" }
            : {
                state: "present",
                bytes: 88,
                sha256: "b".repeat(64),
                exactlyOneMarker: true,
                markerOccurrences: 1,
              },
      },
    };
  }
  const priorLaunch = {
    kind: "launch",
    at: date(now() - 10000),
    id: id(),
    operationId: id(),
    sessionId: a.STOPPED_SESSION,
    profileId: a.PROFILES.codex.id,
    revision: 1,
    journalId: a.HOLDER,
    holderId: id(),
    recovery: false,
  };
  const snapshot = {
    version: 1,
    source: a.STOPPED_SOURCE,
    origin: "http://127.0.0.1:8787",
    scope: {
      namespace: "default",
      principal: "key:" + a.PROFILES.codex.key,
      route: "canary-pi-codex",
      since: now() - 20000,
    },
    requestedAt: now() - 15000,
    observedAt: now() - 14999,
    fetchedRows: 0,
    rows: [],
    totalAttempts: 0,
  };
  const sessionPath = "/api/agent-sessions/" + a.STOPPED_SESSION;
  const prior = {
    version: 1,
    relay: a.SOURCE,
    router: a.STOPPED_SOURCE,
    status: "stopped",
    reason: "Unexpected tool permission",
    completed: false,
    runs: [
      {
        provider: "codex",
        runId: a.PROFILES.codex.run,
        effort: false,
        prompts: 1,
        deadline: now() - 1000,
        firstSnapshot: snapshot,
        lastSnapshot: clone(snapshot),
      },
    ],
    evidence: [
      priorLaunch,
      {
        kind: "pre-launch",
        marker: marker("codex", a.PROFILES.codex.run, "baseline"),
      },
      {
        kind: "cleanup",
        status: "closed-and-released",
        at: date(now() - 8000),
      },
    ],
    operations: [
      {
        status: "received",
        path: "/api/agent-launches",
        body: { operationId: priorLaunch.operationId },
      },
      {
        status: "received",
        kind: "lease-acquisition",
        path: sessionPath + "/control",
      },
      {
        status: "received",
        path: "/api/agent-permissions/" + a.STOPPED_PERMISSION + "/decision",
        body: { outcome: { outcome: "cancelled" } },
      },
      ...["prompt", "cancel", "close"].map((kind) => ({
        status: "received",
        path: sessionPath + "/commands",
        body: {
          kind,
          generation: 1,
          operationId: id(),
          ...(kind === "prompt"
            ? { text: "Reply exactly READY. Do not use tools or modify files." }
            : {}),
        },
      })),
    ],
  };
  function evidence(providers) {
    return providers.flatMap((provider) => {
      const p = profiles[provider],
        s = id();
      return [
        {
          kind: "launch",
          id: id(),
          operationId: id(),
          sessionId: s,
          profileId: p.id,
          revision: 1,
          at: date(now() - 7000),
        },
        {
          kind: "delivery",
          sessionId: s,
          credentialId: p.credential,
          credentialGeneration: p.generation,
          principal: "key:" + p.key,
        },
        { kind: "closed", sessionId: s },
      ];
    });
  }
  const ar = {
    version: 1,
    relay: q.reportHubSources.a,
    router: q.reportRouterSources.a,
    status: "phase-a-passed",
    phaseACompleted: true,
    completed: false,
    replacement: { carriedPrompts: 1, carriedAttempts: 2 },
    predecessor: { raw: JSON.stringify(prior) },
    successorCredentials: {
      profiles: { codex: profiles.codex, qwen: profiles.qwen },
    },
    runs: [
      ["codex", false, a.PROFILES.codex.run],
      ["qwen", false, a.PROFILES.qwen.run],
      ["codex", true, id()],
    ].map(([provider, effort, runId]) => ({
      provider,
      effort,
      runId,
      status: "passed-with-live-wire-unobserved",
      deadline: now() - 1,
    })),
    evidence: evidence(["codex", "codex", "qwen", "qwen", "codex"]),
  };
  ar.evidence.push(
    ...["codex", "qwen"].map((provider) => ({
      kind: "operator-observation",
      marker: marker(provider, profiles[provider].run),
    })),
  );
  const br = {
    version: 1,
    kind: "claude-phase-b",
    relay: q.reportHubSources.b,
    router: q.reportRouterSources.b,
    status: "phase-b-passed",
    phaseBCompleted: true,
    completed: false,
    inputs: {
      version: 1,
      lifecycleRunId: id(),
      effortRunId: id(),
      account: "synthetic-seat",
      model: "synthetic-model",
      window: { startsAt: now() - 50000, expiresAt: now() - 1 },
    },
    claudeCredential: profiles.claude,
    runs: [],
    evidence: evidence(["claude", "claude", "claude"]),
    attemptFence: {
      phases: [false, true].map(() => ({
        closed: true,
        closeReceipt: {
          fence: { closedAt: now() - 1 },
          openRequests: 0,
          staleOpenRequests: 0,
          unattributed: 0,
        },
      })),
    },
  };
  br.runs = [false, true].map((effort) => ({
    provider: "claude",
    effort,
    runId: effort ? br.inputs.effortRunId : br.inputs.lifecycleRunId,
    status: "passed-with-admission-fence-live-wire-unobserved",
  }));
  br.evidence.push({
    kind: "operator-observation",
    marker: marker("claude", br.inputs.lifecycleRunId),
  });
  const cr = {
    version: 1,
    kind: "claude-phase-c",
    relay: q.reportHubSources.c,
    router: q.reportRouterSources.c,
    status: "phase-c-passed",
    phaseCCompleted: true,
    completed: false,
    inputs: {
      version: 1,
      runId: id(),
      account: br.inputs.account,
      model: br.inputs.model,
    },
    claudeCredential: profiles.claude,
    runs: [],
    evidence: evidence(["claude"]),
    rows: [2, 2, 3, 2].map((cap, i) => ({
      rowId: "P" + (i + 1),
      cap,
      generations: cap,
      state: "passed",
    })),
    fences: [
      {
        state: "closed",
        openRequests: 0,
        staleOpenRequests: 0,
        unattributed: 0,
      },
      {
        state: "closed",
        openRequests: 0,
        staleOpenRequests: 0,
        unattributed: 0,
      },
    ],
    originalSettings: { values: { warmSessions: false, warmIdleMs: 300000 } },
    capacity: "unexercised",
    activeDrain: "unexercised",
  };
  cr.runs = [
    {
      provider: "claude",
      effort: false,
      runId: cr.inputs.runId,
      status: "passed-local-process-observations",
    },
  ];
  cr.evidence.push({
    kind: "warm-launch-baselines",
    post: marker("claude", cr.inputs.runId, "baseline"),
  });
  // Explicit synthetic report contract fixture, not an executed D producer or
  // evidence of actual Router timing, renderer authority or native drainage.
  let dr;
  if (activeDrain) {
    cr.inputs.phaseId = id();
    cr.inputs.coldReportSha256 = "1".repeat(64);
    cr.inputs.pairedReportSha256 = "2".repeat(64);
    const source = q.activeDrainReportSources.routerSource,
      runtime = id();
    const setting = (warmSessions, generation, tick, revision) => ({
      source,
      revision: revision.repeat(64),
      projectionSha256: "3".repeat(64),
      pending: false,
      values: { warmSessions, warmIdleMs: 300000 },
      providerRuntime: {
        runtimeInstanceId: runtime,
        providerGeneration: generation,
        observedAt: now(),
        observedMonotonicNs: tick,
      },
    });
    const original = setting(false, 1, "1", "4"),
      before = setting(true, 2, "20", "5"),
      after = setting(false, 3, "30", "6"),
      final = setting(false, 3, "60", "6");
    dr = {
      version: 1,
      kind: "claude-active-drain",
      relay: q.activeDrainReportSources.hubSource,
      router: source,
      status: "active-drain-passed",
      activeDrainCompleted: true,
      completed: false,
      activeDrain: "observed",
      capacity: "unexercised",
      inputs: {
        ...clone(cr.inputs),
        runId: id(),
        phaseId: id(),
        phaseCRouterSource: cr.router,
        exclusiveWindowSha256: "7".repeat(64),
      },
      claudeCredential: clone(profiles.claude),
      evidence: evidence(["claude"]),
      originalSettings: original,
      finalSettings: final,
      settingsOperations: [],
      fenceOperations: [],
      drainBaseline: { attempts: 100, tokenUsageUnknown: 2 },
      drainUsage: { attempts: 4, tokenUsageUnknown: 1 },
      process: { processes: [] },
    };
    dr.scope = {
      runId: dr.inputs.runId,
      phaseId: id(),
      hubSource: dr.relay,
      routerSource: source,
      profileId: b.PROFILE.id,
      profileRevision: 1,
      bindingId: b.PROFILE.binding,
      credentialId: profiles.claude.credential,
      credentialGeneration: profiles.claude.generation,
      keyId: profiles.claude.key,
      namespace: "default",
      principalId: "key:" + profiles.claude.key,
      account: dr.inputs.account,
      model: dr.inputs.model,
      runtimeInstanceId: runtime,
      deploymentId: id(),
      providerGeneration: 3,
    };
    dr.runs = [
      {
        provider: "claude",
        effort: false,
        runId: dr.inputs.runId,
        status: "passed-local-process-observations",
      },
    ];
    dr.evidence.push({
      kind: "warm-launch-baselines",
      pre: marker("claude", dr.inputs.runId, "baseline"),
      post: marker("claude", dr.inputs.runId, "baseline"),
    });
    dr.subplans = [0, 1].map((i) => ({
      kind: i ? "cold" : "warm",
      phaseLimit: 2,
      rows: [{ id: "D" + (i + 1), limit: 2 }],
      phaseId: i ? dr.scope.phaseId : dr.inputs.phaseId,
      fenceId: id(),
      planDigest: "8".repeat(64),
    }));
    dr.fences = dr.subplans.map((p, i) => ({
      ...clone(p),
      runId: dr.inputs.runId,
      keyId: dr.scope.keyId,
      namespace: dr.scope.namespace,
      principalId: dr.scope.principalId,
      deploymentId: dr.scope.deploymentId,
      selection: { providerGeneration: i + 2 },
      activeRowId: "D" + (i + 1),
      state: "closed",
      revision: 2 + i * 2,
      baselineAttempts: 100 + i * 2,
      rowStartAttempts: 100 + i * 2,
      attempts: 102 + i * 2,
      baselineTokenUsageUnknown: 2,
      rowStartTokenUsageUnknown: 2,
      tokenUsageUnknown: i ? 3 : 2,
      openRequests: 0,
      staleOpenRequests: 0,
      unattributed: 0,
      ...(i ? { predecessorFenceId: dr.subplans[0].fenceId } : {}),
    }));
    dr.rows = dr.subplans.map((p, i) => {
      const event = {
        requestId: id(),
        attemptId: id(),
        mode: i ? "cold" : "warm",
        turn: 1,
        reused: false,
        accountId: dr.scope.account,
        providerGeneration: i + 2,
        identity: { id: id(), pid: 100 + i, ppid: 50, startTicks: "1234" },
      };
      return {
        rowId: "D" + (i + 1),
        state: "passed",
        cap: 2,
        generations: 2,
        phaseId: p.phaseId,
        fenceId: p.fenceId,
        scopeDigest: "9".repeat(64),
        admittedBefore: 100 + i * 2,
        settledTokenUsageUnknown: i ? 3 : 2,
        terminalEvidenceSha256: "a".repeat(64),
        final: {
          dispatch: {
            ...event,
            phase: "dispatch",
            sequence: 1,
            observedMonotonicNs: "5",
          },
          terminal: {
            ...event,
            phase: "terminal",
            sequence: 4,
            observedMonotonicNs: "50",
            outcome: "reaped",
          },
          ...(!i
            ? {
                fetch: {
                  ...event,
                  phase: "fetch-invoked",
                  sequence: 2,
                  observedMonotonicNs: "10",
                },
                native: {
                  ...event,
                  phase: "native-complete",
                  sequence: 3,
                  observedMonotonicNs: "40",
                },
              }
            : {}),
        },
      };
    });
    dr.rows[0].overlap = {
      fetchNs: "10",
      beforeNs: "20",
      afterNs: "30",
      nativeCompleteNs: "40",
    };
    dr.activeAttempt = {
      dispatch: dr.rows[0].final.dispatch,
      fetch: dr.rows[0].final.fetch,
    };
    dr.settingsOperations = [
      [original, before, "enable"],
      [before, after, "active-disable"],
    ].map(([from, to, kind]) => ({
      intentId: id(),
      kind,
      state: "confirmed",
      expectedRevision: from.revision,
      afterRevision: to.revision,
      before: from.values,
      values: to.values,
      readbackSha256: to.projectionSha256,
      providerRuntime: to.providerRuntime,
    }));
    dr.activeSettings = {
      before,
      after,
      intentId: dr.settingsOperations[1].intentId,
    };
    dr.fenceOperations = dr.fences.flatMap((f) =>
      ["arm", "close"].map((method) => ({
        operationId: id(),
        method,
        state: "received",
        receiptSha256: "b".repeat(64),
        body: { fenceId: f.fenceId },
      })),
    );
    dr.processEpochs = [0, 1, 2].map((i) => ({
      scope: {
        ...dr.scope,
        phaseId: i < 2 ? dr.inputs.phaseId : dr.scope.phaseId,
        providerGeneration: i + 1,
      },
      process: {
        processes: [],
        events: i === 1 ? Object.values(dr.rows[0].final) : [],
      },
      transition: i === 2 ? "final-drained-and-joined" : "drained-and-joined",
    }));
    dr.rows[0].scopeDigest = hash(a.canonical(dr.processEpochs[1].scope));
  }
  let reports, manifest;
  const repin = () => {
    reports = { a: JSON.stringify(ar), b: JSON.stringify(br) };
    reports.bInputs = JSON.stringify({
      version: 1,
      kind: "claude-phase-b-inputs",
      phaseAReportSha256: hash(reports.a),
      inputs: br.inputs,
    });
    cr.inputs.phaseAReportSha256 = hash(reports.a);
    cr.inputs.phaseBReportSha256 = hash(reports.b);
    reports.c = JSON.stringify(cr);
    if (activeDrain) {
      dr.inputs.phaseAReportSha256 = hash(reports.a);
      dr.inputs.phaseBReportSha256 = hash(reports.b);
      dr.inputs.phaseCReportSha256 = hash(reports.c);
      reports.d = JSON.stringify(dr);
    }
    manifest = {
      version: 1,
      pins: Object.fromEntries(
        Object.entries(reports).map(([k, v]) => [k, hash(v)]),
      ),
      cleanupDeadline: now() + 90000,
      decisionSha256: "c".repeat(64),
      ...(activeDrain ? { activeDrain: true } : {}),
    };
  };
  repin();
  const bindings = Object.entries(profiles).map(([provider, p]) => ({
    id: p.binding,
    enabled: true,
    state: "ready",
    host_id: "b3b1be1a-275c-4a2e-b515-428b39c43171",
    connection_id: "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8",
    execution_identity: "c4",
    current_credential_id: p.credential,
    pending_credential_id: null,
    generation: p.generation,
    revision: p.revision,
    grant_spec: {
      routes: ["canary-pi-" + provider],
      access: [provider === "qwen" ? "local" : "subscription"],
      lifetimeSeconds: 86400,
    },
  }));
  const credentials = Object.entries(profiles).flatMap(([provider, p]) =>
    Array.from({ length: p.generation }, (_, i) => ({
      id:
        i + 1 === p.generation ? p.credential : a.PROFILES[provider].credential,
      binding_id: p.binding,
      generation: i + 1,
      router_key_id: i + 1 === p.generation ? p.key : a.PROFILES[provider].key,
      namespace: "default",
      state: i + 1 === p.generation ? "ready" : "revoked",
      revoke_requested: i + 1 !== p.generation,
      cleanup_pending: false,
      failure_code: null,
      expires_at: date(now() - 1),
    })),
  );
  const operations = new Map(
    credentials.map((c) => [
      c.id,
      [{ id: id(), kind: "create", state: c.state, failureCode: null }],
    ]),
  );
  const requests = [],
    saves = [];
  let consumers = [],
    onPost = () => {},
    onObserve = () => {},
    onDetail = () => {},
    onSave = () => {},
    neverCommit = false,
    lose = false;
  const detail = (c) => {
    const t = bindings.find((x) => x.id === c.binding_id);
    const d = {
      credential: {
        id: c.id,
        bindingId: c.binding_id,
        connectionId: t.connection_id,
        generation: c.generation,
        routerKeyId: c.router_key_id,
        namespace: c.namespace,
        state: c.state,
        revokeRequested: c.revoke_requested,
        cleanupPending: c.cleanup_pending,
        failureCode: c.failure_code,
      },
      binding: {
        id: t.id,
        hostId: t.host_id,
        executionIdentity: t.execution_identity,
        enabled: t.enabled,
        state: t.state,
        revision: t.revision,
        generation: t.generation,
        currentCredentialId: t.current_credential_id,
        pendingCredentialId: t.pending_credential_id,
      },
      consumers: [],
      operations: clone(operations.get(c.id)),
    };
    onDetail(d);
    return d;
  };
  async function fetch(url, init = {}) {
    const method = init.method || "GET",
      body = init.body ? JSON.parse(init.body) : undefined;
    requests.push({ url, method, body });
    let value;
    if (method === "POST") {
      const target = bindings.find(
        (v) => url === `/api/router-credentials/${v.id}/revoke`,
      );
      assert.ok(target, "Only fixed revoke writes");
      onPost(target, body);
      if (!neverCommit) {
        target.revision += target.enabled ? 1 : 0;
        target.enabled = false;
        target.state = "revoked";
        target.current_credential_id = null;
        const cs = credentials.filter((c) => c.binding_id === target.id);
        cs.forEach((c) => {
          c.state = "revoked";
          c.revoke_requested = true;
        });
        operations.get(cs[0].id).push({
          id: body.operationId,
          kind: "revoke-binding",
          state: "revoked",
          failureCode: null,
        });
      }
      if (lose) {
        lose = false;
        throw Error("synthetic lost response");
      }
      value = {};
    } else if (url === "/api/router-credentials")
      value = {
        bindings: clone(bindings),
        credentials: clone(credentials),
        consumers: clone(consumers),
      };
    else if (url.startsWith("/api/router-credentials/keys/"))
      value = detail(credentials.find((c) => c.id === url.split("/").at(-1)));
    else if (url.endsWith("/control")) value = { lease: null };
    else value = { id: url.split("/").at(-1), state: "closed", generation: 1 };
    return {
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      text: async () => JSON.stringify(value),
    };
  }
  async function observe(context) {
    if (live && now() < context.notBefore)
      await new Promise((r) => setTimeout(r, context.notBefore - now()));
    if (!live) clock = Math.max(clock, context.notBefore);
    const v = {
      version: 1,
      stage: context.stage,
      reportPins: clone(context.reportPins),
      scope: clone(context.scope),
      hubSource: q.hubSource,
      routerSource: q.routerSource,
      decisionSha256: context.decisionSha256,
      noFurtherUse: true,
      observedUtc: date(),
      sessions: context.launches.map((l) => ({
        sessionId: l.sessionId,
        state: "closed",
        pendingCommands: 0,
        pendingPermissions: 0,
      })),
      bindings: context.scope.bindingIds.map((bindingId) => ({
        bindingId,
        activeConsumers: 0,
        openRequests: 0,
        staleOpenRequests: 0,
        unattributed: 0,
        fenceState:
          bindingId === b.PROFILE.binding ? "closed" : "not-applicable",
      })),
      router: {
        warmSessions: false,
        warmIdleMs: context.originalIdle,
        settingsRevision: "d".repeat(64),
        processes: [],
      },
      provenance: {
        querySha256: "e".repeat(64),
        projectionSha256: "f".repeat(64),
        databaseIdentity: "synthetic-db",
      },
    };
    if (context.stage === "renderer") {
      v.launches = context.launches.map((l) => ({
        ...l,
        commandId: id(),
        status: "absent",
        fileAbsent: true,
        cleanedAt: date(now() - 1000),
      }));
      v.markers = context.markers.map((m) => ({
        ...clone(m),
        observedUtc: date(),
      }));
    }
    Object.assign(v, {
      unlistedLaunches: 0,
      unlistedSessions: 0,
      unresolvedOperations: 0,
    });
    onObserve(v, context);
    return v;
  }
  return {
    ar,
    br,
    cr,
    dr,
    profiles,
    bindings,
    credentials,
    operations,
    requests,
    saves,
    repin,
    now,
    fetch,
    observe,
    get reports() {
      return reports;
    },
    get manifest() {
      return manifest;
    },
    set consumers(v) {
      consumers = v;
    },
    set onPost(v) {
      onPost = v;
    },
    set onObserve(v) {
      onObserve = v;
    },
    set onDetail(v) {
      onDetail = v;
    },
    set onSave(v) {
      onSave = v;
    },
    set lose(v) {
      lose = v;
    },
    set neverCommit(v) {
      neverCommit = v;
    },
    options() {
      return {
        reports,
        manifest,
        observer: {
          qualificationSha256: q.observerQualificationSha256,
          observe,
        },
        authenticatedFetch: fetch,
        save(s) {
          onSave(s);
          saves.push(clone(s));
        },
        now,
        sleep: async (ms) => {
          clock += ms;
        },
      };
    },
  };
}
const posts = (f) => f.requests.filter((r) => r.method === "POST");

test("public unqualified entry and missing adapter refuse before I/O", () => {
  const f = fixture();
  assert.throws(
    () => production.createFinalization(f.options()),
    /not qualified/,
  );
  assert.equal(f.requests.length, 0);
  assert.throws(
    () => candidate.createFinalization({ ...f.options(), observer: null }),
    /observer/,
  );
  assert.equal(f.requests.length, 0);
});
test("synthetic cleanup deployments finalize unchanged historical reports after inference expiry", async () => {
  const f = fixture();
  f.onPost = (_t, body) =>
    assert.ok(
      f.saves
        .at(-1)
        .intents.some(
          (i) =>
            i.status === "sending" && i.body.operationId === body.operationId,
        ),
    );
  const raw = clone(f.reports),
    result = await candidate.createFinalization(f.options()).run();
  assert.equal(result.status, "canary-finalization-passed");
  assert.equal(result.completed, false);
  assert.equal(posts(f).length, 3);
  assert.equal(result.capacity, "unexercised");
  assert.equal(result.activeDrain, "unexercised");
  assert.deepEqual(f.reports, raw);
  for (const name of ["a", "b", "c"]) {
    const report = JSON.parse(raw[name]);
    assert.equal(report.relay, q.reportHubSources[name]);
    assert.equal(report.router, q.reportRouterSources[name]);
    assert.notEqual(report.relay, result.sources.hubSource);
    assert.notEqual(report.router, result.sources.routerSource);
  }
  assert.deepEqual(result.reportPins, f.manifest.pins);
  assert.equal(result.evidence.at(-1).launches.length, 10);
  assert.equal(result.evidence.at(-1).markers.length, 4);
  assert.equal(
    result.evidence
      .at(-1)
      .launches.find((l) => l.sessionId === a.STOPPED_SESSION).generation,
    1,
  );
});

for (const field of ["reportHubSources", "reportRouterSources"])
  for (const [name, mutate] of [
    ["missing map", (pins) => delete pins[field]],
    ["missing artifact", (pins) => delete pins[field].b],
    ["extra artifact", (pins) => (pins[field].other = "1".repeat(40))],
    ["malformed source", (pins) => (pins[field].c = "invalid")],
  ])
    test(field + " " + name + " refuses before I/O", () => {
      const pins = clone(q),
        f = fixture();
      mutate(pins);
      const invalid = load("owner-finalization.cjs", {}, [
        "const QUALIFIED_FINALIZATION = null;",
        "const QUALIFIED_FINALIZATION = " + JSON.stringify(pins) + ";",
      ]);
      assert.throws(
        () => invalid.createFinalization(f.options()),
        /not qualified/,
      );
      assert.equal(f.requests.length, 0);
    });

for (const name of ["a", "b", "c"])
  for (const field of ["relay", "router"])
    test(
      name + " wrong historical " + field + " refuses before transport",
      () => {
        const f = fixture();
        f[name + "r"][field] = field === "relay" ? q.hubSource : q.routerSource;
        f.repin();
        assert.throws(
          () => candidate.createFinalization(f.options()),
          /Report source/,
        );
        assert.equal(f.requests.length, 0);
      },
    );
test("pure fixed input validator refuses the public NULL gate without invoking authority", () => {
  const f = fixture();
  assert.throws(
    () =>
      production.validateFinalizationInputs({
        reports: f.reports,
        manifest: f.manifest,
      }),
    /not qualified/,
  );
  assert.equal(f.requests.length, 0);
});
for (const activeDrain of [false, true])
  test("pure selected plan is cloned and complete: " + activeDrain, () => {
    const f = fixture({ activeDrain });
    const raw = clone(f.reports),
      manifest = clone(f.manifest);
    const plan = candidate.validateFinalizationInputs({
      reports: raw,
      manifest,
    });
    assert.equal(plan.purpose, "canary-finalization");
    assert.equal(plan.launches.length, activeDrain ? 11 : 10);
    assert.equal(plan.markers.length, activeDrain ? 5 : 4);
    assert.equal(plan.scope.bindingIds.length, 3);
    assert.deepEqual(plan.reportPins, f.manifest.pins);
    assert.equal(f.requests.length + f.saves.length, 0);
    plan.scope.bindingIds.pop();
    plan.sources.reportHubSources.a = "0".repeat(40);
    plan.markers[0].files.effects.sha256 = "0".repeat(64);
    assert.deepEqual(raw, f.reports);
    assert.deepEqual(manifest, f.manifest);
    const again = candidate.validateFinalizationInputs({
      reports: raw,
      manifest,
    });
    assert.equal(again.scope.bindingIds.length, 3);
    assert.equal(again.sources.reportHubSources.a, q.reportHubSources.a);
    assert.notDeepEqual(again.markers, plan.markers);
  });
for (const [name, change] of [
  [
    "false selector",
    (f) => {
      f.manifest.activeDrain = false;
    },
  ],
  [
    "missing selector",
    (f) => {
      delete f.manifest.activeDrain;
    },
  ],
  [
    "missing D",
    (f) => {
      delete f.reports.d;
      delete f.manifest.pins.d;
    },
  ],
  [
    "changed raw D",
    (f) => {
      f.reports.d += " ";
    },
  ],
  [
    "unsolicited report",
    (f) => {
      f.reports.extra = "{}";
      f.manifest.pins.extra = hash("{}");
    },
  ],
])
  test("selected D refuses " + name + " before authority", () => {
    const f = fixture({ activeDrain: true });
    change(f);
    assert.throws(() =>
      candidate.validateFinalizationInputs({
        reports: f.reports,
        manifest: f.manifest,
      }),
    );
    assert.throws(() => candidate.createFinalization(f.options()));
    assert.equal(f.requests.length + f.saves.length, 0);
  });
for (const [name, change] of [
  [
    "historical Hub changed",
    (d) => {
      d.relay = q.hubSource;
    },
  ],
  [
    "historical Router changed",
    (d) => {
      d.router = q.routerSource;
    },
  ],
  [
    "unfinished D",
    (d) => {
      d.activeDrainCompleted = false;
    },
  ],
  [
    "wrong C hash",
    (d) => {
      d.inputs.phaseCReportSha256 = "0".repeat(64);
    },
  ],
  [
    "changed credential",
    (d) => {
      d.claudeCredential.generation++;
    },
  ],
  [
    "open session",
    (d) => {
      d.evidence = d.evidence.filter((e) => e.kind !== "closed");
    },
  ],
  [
    "additional launch",
    (d) => {
      d.evidence.push(clone(d.evidence.find((e) => e.kind === "launch")));
    },
  ],
  [
    "missing delivery",
    (d) => {
      d.evidence = d.evidence.filter((e) => e.kind !== "delivery");
    },
  ],
  [
    "missing pre marker",
    (d) => {
      delete d.evidence.find((e) => e.kind === "warm-launch-baselines").pre;
    },
  ],
  [
    "open fence",
    (d) => {
      d.fences[0].state = "active";
    },
  ],
  [
    "borrowed row",
    (d) => {
      d.rows[0].generations = 3;
    },
  ],
  [
    "reset accounting",
    (d) => {
      d.fences[1].baselineAttempts = 0;
    },
  ],
  [
    "lost settings receipt",
    (d) => {
      d.settingsOperations[1].state = "uncertain";
    },
  ],
  [
    "lost close receipt",
    (d) => {
      d.fenceOperations[3].state = "uncertain";
    },
  ],
  [
    "warm remains enabled",
    (d) => {
      d.finalSettings.values.warmSessions = true;
    },
  ],
  [
    "remaining child",
    (d) => {
      d.process.processes.push({ pid: 1 });
    },
  ],
  [
    "unfinished observer",
    (d) => {
      d.processEpochs[2].transition = "running";
    },
  ],
  [
    "late CAS",
    (d) => {
      d.rows[0].overlap.afterNs = "41";
    },
  ],
  [
    "unrelated active attempt",
    (d) => {
      const requestId = "11111111-1111-4111-8111-111111111111";
      d.activeAttempt.dispatch.requestId = requestId;
      d.processEpochs[1].process.events.find(
        (e) => e.phase === "native-complete",
      ).requestId = requestId;
    },
  ],
  [
    "fetch identity differs",
    (d) => {
      d.activeAttempt.fetch.attemptId = "other-attempt";
    },
  ],
  [
    "native child identity differs",
    (d) => {
      d.processEpochs[1].process.events.find(
        (e) => e.phase === "native-complete",
      ).identity.startTicks = "9999";
    },
  ],
  [
    "warm epoch generation differs",
    (d) => {
      d.processEpochs[1].scope.providerGeneration++;
    },
  ],
  [
    "warm epoch runtime differs",
    (d) => {
      d.processEpochs[1].scope.runtimeInstanceId =
        "11111111-1111-4111-8111-111111111111";
    },
  ],
  [
    "warm epoch account differs despite repinned scope",
    (d) => {
      d.processEpochs[1].scope.account = "other-account";
      d.rows[0].scopeDigest = hash(a.canonical(d.processEpochs[1].scope));
    },
  ],
  [
    "warm row scope digest differs",
    (d) => {
      d.rows[0].scopeDigest = "0".repeat(64);
    },
  ],
  [
    "duplicate native witness",
    (d) => {
      d.processEpochs[1].process.events.push(clone(d.rows[0].final.native));
    },
  ],
  [
    "missing D namespace",
    (d) => {
      delete d.scope.namespace;
      d.fences.forEach((f) => delete f.namespace);
    },
  ],
  [
    "missing D deployment",
    (d) => {
      delete d.scope.deploymentId;
      d.fences.forEach((f) => delete f.deploymentId);
    },
  ],
  [
    "wrong native clock",
    (d) => {
      d.processEpochs[1].process.events.find(
        (e) => e.phase === "native-complete",
      ).observedMonotonicNs = "45";
    },
  ],
])
  test("independently pinned D still refuses " + name, () => {
    const f = fixture({ activeDrain: true }),
      d = JSON.parse(f.reports.d);
    change(d);
    f.reports.d = JSON.stringify(d);
    f.manifest.pins.d = hash(f.reports.d);
    assert.throws(() => candidate.createFinalization(f.options()));
    assert.equal(f.requests.length + f.saves.length, 0);
  });
test("D historical pins are separately required while legacy ABC still uses its exact pins", () => {
  const pins = clone(q);
  delete pins.activeDrainReportSources;
  const old = load("owner-finalization.cjs", {}, [
    "const QUALIFIED_FINALIZATION = null;",
    "const QUALIFIED_FINALIZATION = " + JSON.stringify(pins) + ";",
  ]);
  const f = fixture(),
    d = fixture({ activeDrain: true });
  assert.doesNotThrow(() =>
    old.validateFinalizationInputs({
      reports: f.reports,
      manifest: f.manifest,
    }),
  );
  assert.throws(
    () =>
      old.validateFinalizationInputs({
        reports: d.reports,
        manifest: d.manifest,
      }),
    /historical source pins/,
  );
  assert.equal(f.requests.length + d.requests.length, 0);
});
function historicalRenderer(f, v) {
  if (v.stage !== "renderer") return;
  const old = v.launches.find(
    (l) => l.credentialId === a.PROFILES.codex.credential,
  );
  old.status = "not-current";
  old.reference = {
    credentialId: old.credentialId,
    generation: old.generation,
    keyId: a.PROFILES.codex.key,
    secretVersion: 1,
    creationOperationId: "11111111-1111-4111-8111-111111111111",
  };
}
test("historical not-current receipt remains distinct from fresh current-file absence", async () => {
  const f = fixture({ activeDrain: true });
  f.onObserve = (v) => historicalRenderer(f, v);
  const result = await candidate.createFinalization(f.options()).run();
  assert.equal(result.activeDrain, "unexercised");
  assert.deepEqual(result.selectedActiveDrain, {
    reportSha256: f.manifest.pins.d,
    hubSource: f.dr.relay,
    routerSource: f.dr.router,
    status: "active-drain-passed",
    activeDrain: "observed",
  });
  const rows = result.evidence.find((v) => v.stage === "renderer").launches;
  const old = rows.find((l) => l.credentialId === a.PROFILES.codex.credential);
  assert.equal(old.status, "not-current");
  assert.equal(old.fileAbsent, true);
  assert.equal(old.reference.keyId, a.PROFILES.codex.key);
  assert.ok(
    rows.some(
      (l) =>
        l.status === "absent" && l.credentialId === f.profiles.codex.credential,
    ),
  );
  assert.equal(posts(f).length, 3);
});
test("latest renderer not-current refuses even with exact reference and fresh file absence", async () => {
  const f = fixture({ activeDrain: true });
  f.onObserve = (v) => {
    if (v.stage !== "renderer") return;
    const last = v.launches.find(
      (l) => l.credentialId === f.profiles.claude.credential,
    );
    last.status = "not-current";
    last.reference = {
      credentialId: last.credentialId,
      generation: last.generation,
      keyId: f.profiles.claude.key,
      secretVersion: 1,
      creationOperationId: "11111111-1111-4111-8111-111111111111",
    };
  };
  await assert.rejects(
    candidate.createFinalization(f.options()).run(),
    /Renderer/,
  );
  assert.equal(f.saves.at(-1).status, "finalization-unresolved");
  assert.equal(posts(f).length, 3);
});
for (const [name, change] of [
  [
    "file still present",
    (v) => {
      v.fileAbsent = false;
    },
  ],
  [
    "missing immutable reference",
    (v) => {
      delete v.reference;
    },
  ],
  [
    "wrong credential",
    (v) => {
      v.reference.credentialId = "11111111-1111-4111-8111-111111111111";
    },
  ],
  [
    "wrong generation",
    (v) => {
      v.reference.generation++;
    },
  ],
  [
    "wrong key",
    (v) => {
      v.reference.keyId = "key_" + "a".repeat(24);
    },
  ],
  [
    "missing version",
    (v) => {
      delete v.reference.secretVersion;
    },
  ],
  [
    "missing creation operation",
    (v) => {
      delete v.reference.creationOperationId;
    },
  ],
])
  test(
    "not-current alone never completes renderer cleanup: " + name,
    async () => {
      const f = fixture();
      f.onObserve = (v) => {
        historicalRenderer(f, v);
        if (v.stage === "renderer")
          change(v.launches.find((l) => l.status === "not-current"));
      };
      await assert.rejects(
        candidate.createFinalization(f.options()).run(),
        /Renderer/,
      );
      assert.equal(f.saves.at(-1).status, "finalization-unresolved");
      assert.equal(posts(f).length, 3);
    },
  );
for (const [name, change] of [
  [
    "unlisted D session",
    (v) => {
      v.unlistedSessions = 1;
    },
  ],
  [
    "missing D session",
    (v) => {
      v.sessions.pop();
    },
  ],
  [
    "live consumer",
    (v) => {
      v.bindings[2].activeConsumers = 1;
    },
  ],
  [
    "running process",
    (v) => {
      v.router.processes.push({ pid: 1 });
    },
  ],
])
  test("current D inventory refuses " + name + " before revoke", async () => {
    const f = fixture({ activeDrain: true });
    f.onObserve = (v) => change(v);
    await assert.rejects(candidate.createFinalization(f.options()).run());
    assert.equal(posts(f).length, 0);
  });
test("retired enabled FAILED/null-pointer binding uses normal revoke; already disabled/revoked is verified without POST", async () => {
  const f = fixture();
  for (const [i, t] of f.bindings.entries())
    if (i < 2) {
      t.state = i ? "revoked" : "failed";
      t.enabled = !i;
      t.current_credential_id = null;
      t.revision += i;
      for (const c of f.credentials.filter((c) => c.binding_id === t.id)) {
        c.state = "revoked";
        c.revoke_requested = true;
      }
    }
  const r = await candidate.createFinalization(f.options()).run();
  assert.equal(posts(f).length, 2);
  assert.equal(r.intents[0].expectedRevision, 4);
  assert.equal(f.bindings[1].revision, 4);
});
test("lost committed response reads original operation exactly once without retransmission", async () => {
  const f = fixture();
  f.lose = true;
  const r = await candidate.createFinalization(f.options()).run();
  assert.equal(posts(f).length, 3);
  assert.equal(r.intents[0].status, "confirmed");
  assert.equal(
    f.requests.filter(
      (x) => x.body?.operationId === r.intents[0].body.operationId,
    ).length,
    1,
  );
});
test("Stop after first dispatch finishes its readback but never sends another binding", async () => {
  const f = fixture(),
    runner = candidate.createFinalization(f.options());
  f.onPost = () => runner.stop();
  await assert.rejects(runner.run());
  assert.equal(posts(f).length, 1);
  assert.equal(f.saves.at(-1).intents[0].status, "confirmed");
  await assert.rejects(runner.run(), /single-use/);
});
test("uncommitted reply remains unresolved with original ID and no second binding", async () => {
  const f = fixture();
  f.neverCommit = true;
  f.lose = true;
  await assert.rejects(candidate.createFinalization(f.options()).run());
  assert.equal(posts(f).length, 1);
  assert.equal(f.saves.at(-1).intents[0].status, "dispatch-uncertain");
  const saved = f.saves.at(-1);
  assert.equal(saved.reason.code, "settlement-unresolved");
  assert.equal(saved.stage, "revocation-readback");
  assert.equal(saved.intentIndex, 0);
  assert.equal(saved.intents[0].lastReadback.generations.length, 2);
  assert.equal(
    saved.intents[0].lastReadback.generations[0].operationMatches,
    0,
  );
});
test("durable intent save failure prevents POST", async () => {
  const f = fixture();
  f.onSave = (s) => {
    if (s.intents.some((i) => i.status === "sending"))
      throw Error("disk failed");
  };
  await assert.rejects(candidate.createFinalization(f.options()).run());
  assert.equal(posts(f).length, 0);
});
test("Stop at durable intent boundary prevents first POST", async () => {
  const f = fixture(),
    runner = candidate.createFinalization(f.options());
  f.onSave = (s) => {
    if (s.intents.length) runner.stop();
  };
  await assert.rejects(runner.run());
  assert.equal(posts(f).length, 0);
  assert.equal(f.saves.at(-1).intents[0].status, "not-dispatched");
});
test("wrong original operation cannot confirm revocation or advance to another binding", async () => {
  const f = fixture();
  f.onDetail = (d) => {
    const op = d.operations.find((o) => o.kind === "revoke-binding");
    if (op) op.kind = "rotate";
  };
  await assert.rejects(candidate.createFinalization(f.options()).run());
  assert.equal(posts(f).length, 1);
  const saved = f.saves.at(-1);
  assert.equal(
    saved.intents[0].lastReadback.generations[0].operation.kind,
    "rotate",
  );
  assert.equal(saved.reason.reason, "Revocation operation changed");
});

test("HTTP refusal retains status and one original-operation readback without polling or resend", async () => {
  const f = fixture();
  f.neverCommit = true;
  await assert.rejects(
    candidate
      .createFinalization({
        ...f.options(),
        authenticatedFetch: async (url, init) => {
          const response = await f.fetch(url, init);
          return init.method === "POST"
            ? {
                ...response,
                ok: false,
                status: 409,
                text: async () => {
                  throw Error("SENSITIVE_HTTP_BODY_MUST_NOT_BE_READ");
                },
              }
            : response;
        },
      })
      .run(),
  );
  const saved = f.saves.at(-1),
    intent = saved.intents[0];
  assert.equal(posts(f).length, 1);
  assert.equal(intent.httpStatus, 409);
  assert.equal(intent.status, "http-refused");
  assert.equal(intent.dispatchDisposition, "http-response");
  assert.equal(intent.dispatchError.code, "http-refused");
  assert.equal(intent.lastReadback.generations.length, 2);
  assert.equal(
    f.requests.slice(f.requests.findIndex((r) => r.method === "POST") + 1)
      .length,
    2,
  );
  assert.ok(!JSON.stringify(saved).includes("SENSITIVE_"));
});

test("typed adapter pre-dispatch refusal records no dispatch and never starts settlement", async () => {
  const f = fixture();
  await assert.rejects(
    candidate
      .createFinalization({
        ...f.options(),
        authenticatedFetch: async (url, init) => {
          if (init.method === "POST")
            throw candidate.failure("dispatch-blocked");
          return f.fetch(url, init);
        },
      })
      .run(),
  );
  assert.equal(posts(f).length, 0);
  const intent = f.saves.at(-1).intents[0];
  assert.equal(intent.status, "not-dispatched");
  assert.equal(intent.dispatchDisposition, "not-dispatched");
  assert.equal(intent.dispatchError.code, "dispatch-blocked");
  assert.equal(intent.lastReadback, undefined);
});

test("failed validation saves bounded readback before refusal without raw server fields", async () => {
  const f = fixture();
  f.onDetail = (d) => {
    if (posts(f).length) {
      d.credential.bindingId = "22222222-2222-4222-8222-222222222222";
      d.credential.state = "SENSITIVE_STATE";
      d.credential.failureCode = "SENSITIVE_FAILURE";
    }
  };
  await assert.rejects(candidate.createFinalization(f.options()).run());
  const saved = f.saves.at(-1),
    last = saved.intents[0].lastReadback.generations[0];
  assert.equal(last.bindingId, "22222222-2222-4222-8222-222222222222");
  assert.equal(last.state, "invalid");
  assert.equal(last.failureCodePresent, true);
  assert.equal(saved.reason.reason, "Revocation readback identity changed");
  assert.ok(!JSON.stringify(f.saves).includes("SENSITIVE_"));
});

test("external callback secrets stay redacted and a second save failure preserves the primary reason", async () => {
  const f = fixture();
  f.onObserve = () => {
    throw Error("SENSITIVE_CALLBACK");
  };
  f.onSave = (s) => {
    if (s.status === "finalization-unresolved")
      throw Error("SENSITIVE_STORAGE");
  };
  await assert.rejects(
    candidate.createFinalization(f.options()).run(),
    (error) => {
      assert.deepEqual(candidate.diagnostic(error), {
        code: "external-failure",
        reason: "External callback or input failed",
        failureEvidencePersisted: false,
      });
      assert.ok(!error.message.includes("SENSITIVE_"));
      return true;
    },
  );
  assert.equal(posts(f).length, 0);
  assert.ok(!JSON.stringify(f.saves).includes("SENSITIVE_"));
});

for (const [name, mutate] of [
  [
    "pins",
    (v, context) => {
      context.reportPins.a = "0".repeat(64);
      v.reportPins.a = context.reportPins.a;
    },
  ],
  [
    "scope",
    (v, context) => {
      context.scope.profileIds.pop();
      v.scope.profileIds = clone(context.scope.profileIds);
    },
  ],
  [
    "launch",
    (v, context) => {
      context.launches[0].sessionId = "22222222-2222-4222-8222-222222222222";
      v.sessions[0].sessionId = context.launches[0].sessionId;
    },
  ],
])
  test(
    "observer cannot change retained " + name + " through its context",
    async () => {
      const f = fixture(),
        raw = clone(f.reports),
        pins = clone(f.manifest.pins);
      f.onObserve = mutate;
      await assert.rejects(candidate.createFinalization(f.options()).run());
      assert.equal(posts(f).length, 0);
      assert.deepEqual(f.reports, raw);
      assert.deepEqual(f.manifest.pins, pins);
    },
  );

test("observer cannot alter retained marker expectations to approve changed effects", async () => {
  const f = fixture();
  f.onObserve = (v, context) => {
    if (context.stage === "renderer") {
      context.markers[0].files.effects.bytes++;
      v.markers[0].files.effects.bytes = context.markers[0].files.effects.bytes;
    }
  };
  await assert.rejects(candidate.createFinalization(f.options()).run());
  assert.equal(posts(f).length, 3);
  assert.equal(f.saves.at(-1).reason.reason, "Preserved marker changed");
});
for (const [name, mutate] of [
  [
    "report hash",
    (f) => {
      f.manifest.pins.a = "0".repeat(64);
    },
  ],
  [
    "original accounting",
    (f) => {
      f.ar.replacement.carriedAttempts = 0;
      f.repin();
    },
  ],
  [
    "incomplete C",
    (f) => {
      f.cr.rows[2].state = "stopped";
      f.repin();
    },
  ],
  [
    "deadline",
    (f) => {
      f.manifest.cleanupDeadline = f.now();
    },
  ],
  [
    "deadline ceiling",
    (f) => {
      f.manifest.cleanupDeadline = f.now() + 900001;
    },
  ],
  [
    "B input hash linkage",
    (f) => {
      const x = JSON.parse(f.reports.bInputs);
      x.phaseAReportSha256 = "0".repeat(64);
      f.reports.bInputs = JSON.stringify(x);
      f.manifest.pins.bInputs = hash(f.reports.bInputs);
    },
  ],
  [
    "C report linkage",
    (f) => {
      const x = JSON.parse(f.reports.c);
      x.inputs.phaseBReportSha256 = "0".repeat(64);
      f.reports.c = JSON.stringify(x);
      f.manifest.pins.c = hash(f.reports.c);
    },
  ],
])
  test(name + " refuses before transport", () => {
    const f = fixture();
    mutate(f);
    assert.throws(() => candidate.createFinalization(f.options()));
    assert.equal(f.requests.length, 0);
  });
for (const [name, mutate] of [
  [
    "active consumer",
    (f) => {
      f.consumers = [
        {
          id: "22222222-2222-4222-8222-222222222222",
          binding_id: f.profiles.codex.binding,
          state: "active",
        },
      ];
    },
  ],
  [
    "malformed consumer",
    (f) => {
      f.consumers = [{ state: "released" }];
    },
  ],
  [
    "changed grant",
    (f) => {
      f.bindings[0].grant_spec.routes.push("other");
    },
  ],
  [
    "new generation",
    (f) => {
      f.bindings[0].generation++;
    },
  ],
  [
    "unknown expired enum",
    (f) => {
      f.bindings[0].state = "expired";
    },
  ],
  [
    "pending revoke",
    (f) => {
      f.bindings[0].state = "revocation_pending";
    },
  ],
  [
    "truncated bindings",
    (f) => {
      while (f.bindings.length < 500) f.bindings.push(clone(f.bindings[0]));
    },
  ],
  [
    "unresolved credential operation",
    (f) => {
      f.operations.get(f.credentials[0].id)[0].state = "unknown";
    },
  ],
  [
    "missing no-further-use",
    (f) => {
      f.onObserve = (v) => {
        v.noFurtherUse = false;
      };
    },
  ],
  ...["hubSource", "routerSource"].map((field) => [
    "historical source substituted for current " + field,
    (f) => {
      f.onObserve = (v) => {
        v[field] =
          field === "hubSource"
            ? q.reportHubSources.c
            : q.reportRouterSources.c;
      };
    },
  ]),
  [
    "pending permission",
    (f) => {
      f.onObserve = (v) => {
        v.sessions[0].pendingPermissions = 1;
      };
    },
  ],
  [
    "unreported launch",
    (f) => {
      f.onObserve = (v) => {
        v.unlistedLaunches = 1;
      };
    },
  ],
  [
    "open fence",
    (f) => {
      f.onObserve = (v) => {
        v.bindings[2].fenceState = "active";
      };
    },
  ],
  [
    "warm still enabled",
    (f) => {
      f.onObserve = (v) => {
        v.router.warmSessions = true;
      };
    },
  ],
  [
    "stale observation",
    (f) => {
      f.onObserve = (v) => {
        v.observedUtc = new Date(f.now() - 120001).toISOString();
      };
    },
  ],
])
  test(name + " refuses before revoke", async () => {
    const f = fixture();
    mutate(f);
    await assert.rejects(candidate.createFinalization(f.options()).run());
    assert.equal(posts(f).length, 0);
  });
for (const [name, mutate] of [
  ["missing original renderer", (v) => v.launches.pop()],
  [
    "not-current renderer",
    (v) => {
      v.launches[0].status = "not-current";
    },
  ],
  [
    "changed marker bytes",
    (v) => {
      v.markers[0].files.effects.bytes++;
    },
  ],
])
  test(
    name + " keeps completed revocations but refuses final PASS",
    async () => {
      const f = fixture();
      f.onObserve = (v) => {
        if (v.stage === "renderer") mutate(v);
      };
      await assert.rejects(candidate.createFinalization(f.options()).run());
      assert.equal(posts(f).length, 3);
      assert.equal(f.saves.at(-1).status, "finalization-unresolved");
    },
  );

for (const kind of ["response-body", "observer"])
  test(
    "host bounds an uncooperative " + kind + " without a cleanup-success claim",
    { timeout: 5000 },
    async (t) => {
      const root = fs.mkdtempSync(
        path.join(os.tmpdir(), "finalization-abort-"),
      );
      fs.chmodSync(root, 0o700);
      t.after(() => fs.rmSync(root, { recursive: true, force: true }));
      const f = fixture({ live: true });
      f.manifest.cleanupDeadline = Date.now() + 250;
      const never = new Promise(() => {});
      const h = host.createFinalizationAutomation({
        directory: path.join(root, "evidence"),
        reports: f.reports,
        manifest: f.manifest,
        observer: {
          qualificationSha256: q.observerQualificationSha256,
          observe: kind === "observer" ? () => never : f.observe,
        },
        authenticatedFetch:
          kind === "response-body"
            ? async () => ({
                ok: true,
                status: 200,
                headers: new Headers({ "content-type": "application/json" }),
                text: () => never,
              })
            : f.fetch,
      });
      await assert.rejects(h.run(), { code: "callbacks-unresolved" });
      assert.equal(posts(f).length, 0);
      assert.notEqual(h.snapshot()?.status, "canary-finalization-passed");
    },
  );

test(
  "host retains a typed stop after the actual Runner durably completes",
  { timeout: 10000 },
  async (t) => {
    const root = fs.mkdtempSync(
      path.join(os.tmpdir(), "finalization-final-stop-"),
    );
    fs.chmodSync(root, 0o700);
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const f = fixture({ live: true });
    let h,
      stoppedAtCompletion = false;
    // Observe an actual durable write and schedule the stop at the next microtask.
    // The Runner, clock, store/fsync and all filesystem operations remain real.
    const observedFs = {
      ...fs,
      writeFileSync(fd, raw, ...rest) {
        const result = fs.writeFileSync(fd, raw, ...rest);
        if (
          typeof raw === "string" &&
          raw.startsWith("{") &&
          JSON.parse(raw).status === "canary-finalization-passed"
        ) {
          assert.equal(stoppedAtCompletion, false);
          stoppedAtCompletion = true;
          queueMicrotask(() => h.stop());
        }
        return result;
      },
    };
    const boundaryHost = load("owner-finalization-automation.cjs", {
      "./owner-finalization.cjs": candidate,
      "node:fs": observedFs,
    });
    h = boundaryHost.createFinalizationAutomation({
      directory: path.join(root, "evidence"),
      reports: f.reports,
      manifest: f.manifest,
      observer: {
        qualificationSha256: q.observerQualificationSha256,
        observe: f.observe,
      },
      authenticatedFetch: f.fetch,
    });
    await assert.rejects(h.run(), {
      code: "host-stopped",
      message: "Finalization host stopped; preserve evidence",
    });
    assert.equal(stoppedAtCompletion, true);
    assert.equal(posts(f).length, 3);
    assert.equal(h.snapshot().status, "canary-finalization-passed");
    assert.equal(
      h.snapshot().intents.every((i) => i.status === "confirmed"),
      true,
    );
  },
);

for (const fd of [1, 2])
  for (const mode of ["zero-progress", "exception"])
    test(`actual CLI output failure is typed for fd${fd} ${mode}`, async () => {
      const f = fixture(),
        writes = [];
      const pipeCli = load("owner-finalization-cli.cjs", {
        "./owner-finalization.cjs": candidate,
        "./owner-finalization-automation.cjs": host,
        "node:fs": {
          ...fs,
          writeSync(target, raw, offset, length) {
            writes.push({
              target,
              text: raw.subarray(offset, offset + length).toString(),
            });
            if (target === fd) {
              if (mode === "exception") throw Error("SENSITIVE_PIPE_FAILURE");
              return 0;
            }
            return length;
          },
        },
      });
      // Exercise the CLI's actual default writers after an input refusal. A
      // failed pipe must supersede that diagnosis without retry or secret text.
      await assert.rejects(
        pipeCli.runFinalizationCli({
          argv: [],
          observer: {
            qualificationSha256: q.observerQualificationSha256,
            observe: f.observe,
          },
        }),
        {
          code: "output-failed",
          message: "Finalization diagnostic output failed",
        },
      );
      assert.equal(writes.filter((w) => w.target === fd).length, 1);
      assert.ok(writes.length <= 2);
      assert.ok(!JSON.stringify(writes).includes("SENSITIVE_PIPE_FAILURE"));
      assert.equal(f.requests.length, 0);
    });

test(
  "actual CLI distinguishes preparation refusal from an unresolved dispatched operation without leaking errors",
  { skip: process.platform !== "linux", timeout: 10000 },
  async (t) => {
    const root = fs.mkdtempSync(
      path.join(os.tmpdir(), "finalization-diagnostics-"),
    );
    fs.chmodSync(root, 0o700);
    const f = fixture({ live: true }),
      socket = path.join(root, "broker.sock");
    f.neverCommit = true;
    const server = http.createServer(async (req, res) => {
      try {
        let raw = "";
        for await (const chunk of req) raw += chunk;
        const response = await f.fetch(req.url, {
          method: req.method,
          ...(raw ? { body: raw } : {}),
        });
        res.writeHead(req.method === "POST" ? 409 : response.status, {
          "content-type": "application/json",
        });
        res.end(
          req.method === "POST"
            ? '{"error":"SENSITIVE_SERVER_BODY"}'
            : await response.text(),
        );
      } catch {
        req.socket.destroy();
      }
    });
    await new Promise((r) => server.listen(socket, r));
    t.after(async () => {
      server.closeAllConnections();
      await new Promise((r) => server.close(r));
      fs.rmSync(root, { recursive: true, force: true });
    });
    const lines = [],
      errors = [],
      observer = {
        qualificationSha256: q.observerQualificationSha256,
        observe: f.observe,
      };
    const env = {
      RUNNER_DEADLINE: String(f.manifest.cleanupDeadline),
      RELAY_SESSION_SOCKET: socket,
    };
    assert.equal(
      await cli.runFinalizationCli({
        argv: [],
        env,
        observer,
        writeLine: (s) => lines.push(JSON.parse(s)),
        writeError: (s) => errors.push(s),
      }),
      1,
    );
    assert.equal(lines.at(-1).status, "preparation-refused");
    assert.equal(lines.at(-1).stage, "arguments");
    assert.equal(lines.at(-1).intents, 0);
    assert.equal(f.requests.length, 0);
    const args = [];
    for (const [flag, name, raw] of [
      ["--manifest", "manifest", JSON.stringify(f.manifest)],
      ["--phase-a", "a", f.reports.a],
      ["--phase-b", "b", f.reports.b],
      ["--phase-b-inputs", "bInputs", f.reports.bInputs],
      ["--phase-c", "c", f.reports.c],
    ]) {
      const file = path.join(root, name + ".json");
      fs.writeFileSync(file, raw, { mode: 0o600 });
      args.push(flag, file);
    }
    const out = path.join(root, "evidence");
    args.push("--output", out);
    assert.equal(
      await cli.runFinalizationCli({
        argv: args,
        env,
        observer,
        writeLine: (s) => lines.push(JSON.parse(s)),
        writeError: (s) => errors.push(s),
      }),
      1,
    );
    const final = lines.at(-1);
    assert.equal(final.status, "finalization-unresolved");
    assert.equal(final.snapshotStatus, "finalization-unresolved");
    assert.equal(final.stage, "revocation-readback");
    assert.equal(final.reason.code, "settlement-unresolved");
    assert.equal(final.intents, 1);
    assert.equal(final.unconfirmedIntents, 1);
    assert.equal(final.potentiallyDispatchedIntents, 1);
    assert.equal(posts(f).length, 1);
    const snapshots = fs
      .readdirSync(out)
      .filter((v) => v.startsWith("state-"))
      .sort();
    const saved = JSON.parse(
      fs.readFileSync(path.join(out, snapshots.at(-1)), "utf8"),
    );
    assert.equal(saved.intents[0].httpStatus, 409);
    assert.equal(saved.intents[0].lastReadback.generations.length, 2);
    assert.ok(!JSON.stringify([lines, errors, saved]).includes("SENSITIVE_"));
  },
);

for (const activeDrain of [false, true])
  test(
    "actual Unix socket CLI/host preserves " +
      (activeDrain ? "ABC+D" : "ABC") +
      " raw reports and fsynced intent before each fixed POST",
    { skip: process.platform !== "linux", timeout: 30000 },
    async (t) => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "finalization-"));
      fs.chmodSync(root, 0o700);
      const socket = path.join(root, "broker.sock"),
        out = path.join(root, "evidence"),
        f = fixture({ live: true, activeDrain }),
        server = http.createServer(async (req, res) => {
          try {
            let raw = "";
            for await (const chunk of req) raw += chunk;
            const r = await f.fetch(req.url, {
              method: req.method,
              ...(raw ? { body: raw } : {}),
            });
            res.writeHead(r.status, { "content-type": "application/json" });
            res.end(await r.text());
          } catch {
            req.socket.destroy();
          }
        });
      await new Promise((r) => server.listen(socket, r));
      t.after(async () => {
        server.closeAllConnections();
        await new Promise((r) => server.close(r));
        fs.rmSync(root, { recursive: true, force: true });
      });
      const args = [];
      for (const [flag, name, value] of [
        ["--manifest", "manifest", JSON.stringify(f.manifest)],
        ["--phase-a", "a", f.reports.a],
        ["--phase-b", "b", f.reports.b],
        ["--phase-b-inputs", "bInputs", f.reports.bInputs],
        ["--phase-c", "c", f.reports.c],
        ...(activeDrain ? [["--active-drain", "d", f.reports.d]] : []),
      ]) {
        const file = path.join(root, name + ".json");
        fs.writeFileSync(file, value, { mode: 0o600 });
        args.push(flag, file);
      }
      args.push("--output", out);
      f.onPost = (_target, body) => {
        const files = fs
          .readdirSync(out)
          .filter((s) => s.startsWith("state-"))
          .sort();
        const saved = JSON.parse(
          fs.readFileSync(path.join(out, files.at(-1)), "utf8"),
        );
        assert.ok(
          saved.intents.some(
            (i) =>
              i.status === "sending" && i.body.operationId === body.operationId,
          ),
        );
      };
      f.lose = true;
      const lines = [];
      const result = await cli.runFinalizationCli({
        argv: args,
        env: {
          RUNNER_DEADLINE: String(f.manifest.cleanupDeadline),
          RELAY_SESSION_SOCKET: socket,
        },
        observer: {
          qualificationSha256: q.observerQualificationSha256,
          observe: f.observe,
        },
        writeLine: (s) => lines.push(JSON.parse(s)),
      });
      assert.equal(result, 0);
      assert.equal(posts(f).length, 3);
      assert.equal(lines.at(-1).completed, false);
      const last = JSON.parse(
        fs.readFileSync(
          path.join(
            out,
            fs
              .readdirSync(out)
              .filter((name) => name.startsWith("state-"))
              .sort()
              .at(-1),
          ),
          "utf8",
        ),
      );
      const renderer = last.evidence.find((e) => e.stage === "renderer");
      assert.equal(last.activeDrain, "unexercised");
      assert.equal(Object.hasOwn(last, "selectedActiveDrain"), activeDrain);
      if (activeDrain)
        assert.deepEqual(last.selectedActiveDrain, {
          reportSha256: f.manifest.pins.d,
          hubSource: f.dr.relay,
          routerSource: f.dr.router,
          status: "active-drain-passed",
          activeDrain: "observed",
        });
      assert.equal(renderer.launches.length, activeDrain ? 11 : 10);
      assert.equal(renderer.markers.length, activeDrain ? 5 : 4);
      for (const [name, raw] of Object.entries(f.reports))
        assert.equal(
          fs.readFileSync(path.join(out, name + ".json"), "utf8"),
          raw,
        );
      const before = f.requests.length;
      assert.throws(
        () =>
          host.createFinalizationAutomation({
            directory: out,
            reports: f.reports,
            manifest: f.manifest,
            authenticatedFetch: createSessionFetch(socket),
            observer: {
              qualificationSha256: q.observerQualificationSha256,
              observe: f.observe,
            },
          }),
        /EEXIST/,
      );
      assert.equal(f.requests.length, before);
      assert.ok(proof.every((p) => p.liveQualified === false));
      t.diagnostic(
        JSON.stringify({
          kind: "synthetic-finalization-source-pin-qualification",
          files: proof,
        }),
      );
    },
  );
