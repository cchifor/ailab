/* Paste this entire reviewed file in the owner's console at https://relay.chifor.me.
 * No network request or mutation occurs until Start is clicked. No credentials are read.
 * CommonJS exports support offline tests; the browser branch installs the control panel.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else api.mount(root);
})(typeof window === "object" ? window : globalThis, function () {
  "use strict";
  const SOURCE = "e843921b7cff76406b7da0b5b35cf653c7e458c5";
  const ROUTER = "4af66b8c7c3cc83ddd10e326c554ad728caad72e";
  const ORIGIN = "https://relay.chifor.me";
  const EXPIRES = Date.parse("2026-11-26T21:27:37Z");
  const CLOCK_SKEW_MS = 5000;
  const HOLDER = "bbe0a6fb-25ea-44f8-8c21-5cba89f07aeb";
  const APPROVED_PROFILE_REVISION = 1;
  const PROFILES_SHA256 =
    "928d41f0b24c0d1d2493458e7fac23cfd686c045b1aa9daaee480684a0aea9a4";
  const HOLDER_BINARY_SHA256 =
    "0b1a7907f6da2ef6b5794621d87c55c2e0ee6d5aeb1c4e0fdf35c9031a127003";
  // First-registration revision-1 bytes passed fresh published-holder root,
  // deployed-hub schema/normalization and offline registration-boundary checks.
  // Start still requires actual installed hashes and separate owner approval.
  const QUALIFIED_INPUTS = true;
  const CONFIGURATION_CHANGED_NOTICE =
    "Router refused changed session configuration (SESSION_CONFIGURATION_CHANGED). Explicit reconciliation is required. No provider or model fallback was used.";
  const HOST = "b3b1be1a-275c-4a2e-b515-428b39c43171";
  const STORE = "relay-pi-phase-a-20261126-v1";
  const PROFILES = {
    codex: {
      id: "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae",
      run: "9120da55-d870-431a-aba7-0ba4fe5b84f8",
      binding: "a0f57684-747f-4c3e-8d90-e684b7262590",
      credential: "87482892-42ab-40a5-aa03-01c41ec5f6df",
      key: "key_d72ca9b617b83281d4a2706393693b61",
      protocol: "openai-responses",
      definitionHash:
        "0e15b7e39b0241c692b305c6c8d2f4579843c8e7fcfb86ea37534ab996fd194d",
    },
    qwen: {
      id: "f25c187b-1e50-4c7c-860e-52947bfdf39e",
      run: "b69505b2-f61a-42b8-ad08-ce183b2ebc2a",
      binding: "d2871eec-8f5a-4afe-a917-014f0ae8e14d",
      credential: "5e7019ce-8fae-4468-941a-7d0b8c02a93c",
      key: "key_9b01832ad0609853be340e03968b7d50",
      protocol: "openai-chat",
      definitionHash:
        "4ceab5d4122eab6b54ae04196feea258d4d290be381d6c018186f982c8081928",
    },
  };
  const EFFORT_RUN = "45027fc9-083b-41ff-8cc7-a46a98ae510e";
  const uuid = (s) =>
    typeof s === "string" &&
    /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(s);
  const integer = (n) => Number.isSafeInteger(n) && n >= 0;
  function need(ok, message) {
    if (!ok) throw new Error(message);
  }
  function canonical(v) {
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    if (v && typeof v === "object")
      return (
        "{" +
        Object.keys(v)
          .sort()
          .map((k) => JSON.stringify(k) + ":" + canonical(v[k]))
          .join(",") +
        "}"
      );
    return JSON.stringify(v);
  }
  async function digest(crypto, v) {
    return Array.from(
      new Uint8Array(
        await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(canonical(v)),
        ),
      ),
      (b) => b.toString(16).padStart(2, "0"),
    ).join("");
  }
  function markerCommand(provider, run, kind) {
    need(
      PROFILES[provider] && uuid(run) && ["denied", "effects"].includes(kind),
      "Invalid marker selection",
    );
    return (
      "umask 077; printf '%s\\n' 'relay-pi-" +
      provider +
      "-" +
      run +
      "' >> '/home/c4/.local/state/relay-holder/workspaces/" +
      PROFILES[provider].id +
      "/acceptance-" +
      run +
      "." +
      kind +
      "'"
    );
  }
  function marker(report, provider, run, phase, now) {
    need(
      report?.version === 1 &&
        report.provider === provider &&
        report.profileId === PROFILES[provider].id &&
        report.runId === run &&
        report.phase === phase &&
        report.state === "observed" &&
        report.phaseChecksPass === true,
      "Marker report identity/phase failed",
    );
    need(
      Number.isFinite(Date.parse(report.observedUtc)) &&
        now - Date.parse(report.observedUtc) >= -5000 &&
        now - Date.parse(report.observedUtc) <= 120000,
      "Marker observation must be fresh",
    );
    for (const k of ["denied", "effects"])
      need(
        report.commands?.[k] === markerCommand(provider, run, k),
        "Marker command changed",
      );
    need(report.files?.denied?.state === "absent", "Denied marker exists");
    if (["baseline", "denied"].includes(phase))
      need(
        report.files?.effects?.state === "absent",
        "Unexpected effect marker",
      );
    else
      need(
        report.files?.effects?.state === "present" &&
          report.files.effects.exactlyOneMarker === true &&
          report.files.effects.markerOccurrences === 1 &&
          /^[a-f0-9]{64}$/.test(report.files.effects.sha256),
        "Effect was not exactly once",
      );
    // Project operator input; never export unrelated pasted data or credentials.
    const file = (v) =>
      v.state === "absent"
        ? { state: "absent" }
        : {
            state: v.state,
            bytes: v.bytes,
            sha256: v.sha256,
            exactlyOneMarker: v.exactlyOneMarker,
            markerOccurrences: v.markerOccurrences,
          };
    return {
      version: 1,
      observedUtc: report.observedUtc,
      provider,
      profileId: PROFILES[provider].id,
      runId: run,
      phase,
      state: "observed",
      phaseChecksPass: true,
      commands: {
        denied: report.commands.denied,
        effects: report.commands.effects,
      },
      files: {
        denied: file(report.files.denied),
        effects: file(report.files.effects),
      },
    };
  }
  function unchangedBaseline(before, after) {
    const a = { ...before },
      b = { ...after };
    delete a.observedUtc;
    delete b.observedUtc;
    need(
      canonical(a) === canonical(b),
      "Open changed the retained marker baseline",
    );
  }
  function snapshot(v, provider) {
    const p = PROFILES[provider];
    need(
      v?.version === 1 &&
        v.source === ROUTER &&
        v.origin === "http://127.0.0.1:8787" &&
        v.scope?.namespace === "default" &&
        v.scope.principal === "key:" + p.key &&
        v.scope.route === "canary-pi-" + provider &&
        integer(v.scope.since),
      "Trajectory scope/source mismatch",
    );
    need(
      integer(v.requestedAt) &&
        integer(v.observedAt) &&
        v.scope.since <= v.requestedAt &&
        v.requestedAt <= v.observedAt &&
        Array.isArray(v.rows) &&
        integer(v.fetchedRows) &&
        v.fetchedRows < 500 &&
        v.fetchedRows >= v.rows.length,
      "Incomplete trajectory envelope",
    );
    const ids = new Set();
    let total = 0;
    for (const r of v.rows) {
      need(
        typeof r.id === "string" &&
          /^[A-Za-z0-9_.:-]{1,200}$/.test(r.id) &&
          typeof r.requestId === "string" &&
          /^[A-Za-z0-9_.:-]{1,200}$/.test(r.requestId) &&
          !ids.has(r.id) &&
          r.namespace === "default" &&
          r.principalId === v.scope.principal &&
          r.route === v.scope.route &&
          integer(r.startedAt) &&
          integer(r.endedAt) &&
          r.startedAt >= v.scope.since &&
          r.endedAt >= r.startedAt &&
          r.endedAt <= v.observedAt &&
          r.incomplete === false &&
          ["complete", "failed", "deferred", "cancelled", "abandoned"].includes(
            r.status,
          ) &&
          integer(r.attempts) &&
          integer(r.configRevision),
        "Unsettled/invalid trajectory",
      );
      ids.add(r.id);
      total += r.attempts;
    }
    need(total === v.totalAttempts, "Trajectory total changed");
    const keys = [
      "id",
      "requestId",
      "namespace",
      "principalId",
      "route",
      "startedAt",
      "endedAt",
      "status",
      "incomplete",
      "attempts",
      "configRevision",
    ];
    return {
      version: 1,
      source: ROUTER,
      origin: v.origin,
      scope: {
        namespace: "default",
        principal: "key:" + p.key,
        route: "canary-pi-" + provider,
        since: v.scope.since,
      },
      requestedAt: v.requestedAt,
      observedAt: v.observedAt,
      fetchedRows: v.fetchedRows,
      rows: v.rows.map((r) => Object.fromEntries(keys.map((k) => [k, r[k]]))),
      totalAttempts: total,
    };
  }
  function delta(a, b, provider) {
    a = snapshot(a, provider);
    b = snapshot(b, provider);
    need(
      canonical(a.scope) === canonical(b.scope) && b.requestedAt > a.observedAt,
      "Trajectory chronology/scope changed",
    );
    const old = new Map(a.rows.map((r) => [r.id, r])),
      current = new Map(b.rows.map((r) => [r.id, r]));
    for (const [id, r] of old)
      need(
        canonical(r) === canonical(current.get(id)),
        "Baseline trajectory changed/disappeared",
      );
    need(
      b.rows
        .filter((r) => !old.has(r.id))
        .every((r) => r.startedAt > a.observedAt),
      "Late baseline trajectory",
    );
    return b.totalAttempts - a.totalAttempts;
  }
  function completed(row, expected) {
    need(
      row &&
        row.operation_id === expected.operationId &&
        row.session_id === expected.sessionId &&
        row.kind === expected.kind &&
        row.generation === expected.generation,
      "Operation identity changed",
    );
    if (["unknown", "withdrawn", "rejected"].includes(row.state))
      throw new Error(
        "Operation " +
          row.state +
          "; inspect the saved ID, never retry blindly",
      );
    if (row.state !== "completed") {
      need(
        ["pending", "offered", "accepted"].includes(row.state),
        "Unknown operation state",
      );
      return false;
    }
    need(row.receipt?.status === "completed", "Missing native receipt");
    return true;
  }
  function permission(detail, session, generation, expected, kind, now) {
    need(
      detail?.session_id === session &&
        detail.generation === generation &&
        !detail.rebinding &&
        detail.state === "pending" &&
        Date.parse(detail.expires_at) > now,
      "Permission changed or expired",
    );
    const c = detail.context;
    need(
      c?.title === "Allow bash?" && typeof c.description === "string",
      "Unexpected/unverifiable tool",
    );
    let input;
    try {
      input = JSON.parse(c.description);
    } catch {
      throw new Error("Full tool input unavailable; do not approve");
    }
    need(
      input &&
        input.command === expected &&
        Object.keys(input).every((k) => ["command", "timeout"].includes(k)) &&
        (input.timeout === undefined ||
          (Number.isFinite(input.timeout) &&
            input.timeout > 0 &&
            input.timeout <= 120)),
      "Tool command/options differ from the approved marker",
    );
    const choices = c.options?.filter((o) => o.kind === kind);
    need(
      choices?.length === 1 && typeof choices[0].optionId === "string",
      "Missing unambiguous once-only option",
    );
    return choices[0].optionId;
  }
  class Runner {
    constructor(env) {
      this.env = env;
      this.crypto = env.crypto;
      this.now = env.now || Date.now;
      this.sleep = env.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
      this.state = {
        version: 1,
        relay: SOURCE,
        router: ROUTER,
        completed: false,
        runs: [],
        operations: [],
        evidence: [],
        status: "prepared",
      };
      this.session = null;
      this.holderId = HOLDER;
      this.journalId = HOLDER;
      this.lease = null;
      this.pending = null;
      this.stopped = false;
      this.cleaning = false;
    }
    save() {
      this.env.save(this.state);
    }
    record(kind, value) {
      this.state.evidence.push({
        at: new Date(this.now()).toISOString(),
        kind,
        ...value,
      });
      this.save();
    }
    collectionBoundary(label) {
      // This runs after the assessed action has settled. A collection must
      // begin beyond the full allowed clock offset, never merely be fresh.
      const completedBefore = this.now();
      const boundary = {
        completedBefore,
        notBefore: completedBefore + CLOCK_SKEW_MS,
        maxClockSkewMs: CLOCK_SKEW_MS,
      };
      this.record("collection-boundary", { label, ...boundary });
      return boundary;
    }
    collectedAfter(boundary, trajectory, markerReport) {
      if (trajectory)
        need(
          trajectory.requestedAt >= boundary.notBefore,
          "Trajectory collection predates action completion/skew boundary",
        );
      if (markerReport)
        need(
          Date.parse(markerReport.observedUtc) >= boundary.notBefore,
          "Marker observation predates action completion/skew boundary",
        );
    }
    check() {
      need(
        !this.stopped && this.now() < EXPIRES,
        "Stopped or credential window expired",
      );
      if (this.run)
        need(this.now() < this.run.deadline, "Phase deadline reached");
      if (this.lease)
        need(
          Date.parse(this.lease.expires_at) > this.now() + 1000,
          "Control lease expired",
        );
    }
    async ask(
      title,
      instructions,
      validate = (v) => {
        need(v === true, "Confirmation required");
        return v;
      },
      timeout = 120000,
    ) {
      this.check();
      return this.env.ask(
        title,
        instructions,
        validate,
        Math.min(
          timeout,
          EXPIRES - this.now(),
          this.run ? this.run.deadline - this.now() : timeout,
        ),
      );
    }
    async api(path, method = "GET", body) {
      need(
        /^\/api\//.test(path) && !path.includes("\\") && !path.includes("#"),
        "Invalid API path",
      );
      const result = await this.env.fetch(path, {
        method,
        credentials: "same-origin",
        redirect: "error",
        cache: "no-store",
        headers: { "content-type": "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: AbortSignal.timeout(15000),
      });
      need(
        result.ok,
        "API refused " + method + " " + path + " (HTTP " + result.status + ")",
      );
      need(
        result.headers.get("content-type")?.includes("application/json"),
        "Non-JSON API response",
      );
      const raw = await result.text();
      need(raw.length <= 2 * 1024 * 1024, "Oversized API response");
      return JSON.parse(raw);
    }
    async mutate(path, body) {
      this.check();
      const item = { path, body, at: this.now(), status: "sending" };
      this.state.operations.push(item);
      this.save(); // Save BEFORE dispatch, including uncertain operations.
      try {
        const r = await this.api(path, "POST", body);
        item.status = "received";
        if (uuid(r?.id)) item.id = r.id;
        this.save();
        return r;
      } catch (e) {
        item.status = "uncertain";
        this.save();
        throw e;
      }
    }
    async sessionRead() {
      const s = await this.api("/api/agent-sessions/" + this.session.id);
      need(
        s.id === this.session.id &&
          s.kind === "holder" &&
          s.connector_id === HOST &&
          !s.parent_session_id &&
          !s.rebinding &&
          s.generation === this.session.generation,
        "Session identity/generation changed",
      );
      this.session = s;
      return s;
    }
    async acquire() {
      const s = await this.sessionRead();
      need(s.state === "open", "Session is not open");
      const current = await this.api(
        "/api/agent-sessions/" + s.id + "/control",
      );
      need(
        current.lease === null,
        "Existing control lease; inspect it instead of taking over",
      );
      this.check(); // Stop may have arrived during the read-only preflight.
      const intent = {
        kind: "lease-acquisition",
        path: "/api/agent-sessions/" + s.id + "/control",
        body: { generation: s.generation },
        sessionId: s.id,
        at: this.now(),
        status: "sending",
      };
      this.state.controlAcquisition = intent;
      this.state.operations.push(intent);
      this.save();
      try {
        const lease = await this.api(intent.path, "POST", intent.body);
        need(
          uuid(lease.id) &&
            lease.generation === s.generation &&
            Date.parse(lease.expires_at) > this.now(),
          "Invalid lease",
        );
        this.lease = lease;
        intent.status = "received";
        intent.id = lease.id;
        this.save();
      } catch (error) {
        intent.status = "uncertain";
        this.save();
        await this.inspectAcquisition();
        throw error;
      }
      this.state.control = {
        sessionId: s.id,
        generation: s.generation,
        leaseId: this.lease.id,
      };
      this.save();
    }
    async inspectAcquisition() {
      const intent = this.state.controlAcquisition;
      need(
        intent?.status === "uncertain" && this.session?.id === intent.sessionId,
        "No matching uncertain lease acquisition",
      );
      let observation = { status: "readback-unavailable" };
      try {
        const current = await this.api(intent.path);
        if (current.lease === null)
          observation = { status: "no-lease-observed" };
        else {
          const lease = current.lease;
          need(
            uuid(lease?.id) &&
              lease.generation === intent.body.generation &&
              typeof lease.owned === "boolean" &&
              Number.isFinite(Date.parse(lease.expires_at)),
            "Uncertain lease readback identity changed",
          );
          observation = {
            status: "lease-observed",
            id: lease.id,
            generation: lease.generation,
            owned: lease.owned,
            expiresAt: lease.expires_at,
          };
        }
      } catch {
        /* Preserve uncertainty; never replace the original request. */
      }
      intent.observation = observation;
      intent.inspectedAt = this.now();
      // 'owned' identifies this user, not this tab's lost POST. A concurrent
      // owner tab may have acquired it. Do not adopt, release or reacquire it.
      this.record("uncertain-lease-acquisition", {
        sessionId: intent.sessionId,
        generation: intent.body.generation,
        observation,
        resolution: "owner-inspection-required",
      });
    }
    async renew() {
      if (!this.lease || this.renewing || this.cleaning || this.closing) return;
      this.renewing = true;
      try {
        const s = this.session,
          l = this.lease;
        const next = await this.api(
          "/api/agent-sessions/" + s.id + "/control",
          "POST",
          { generation: s.generation, leaseId: l.id },
        );
        need(
          next.id === l.id && next.generation === s.generation,
          "Lease identity changed",
        );
        if (this.lease === l) this.lease = next;
      } finally {
        this.renewing = false;
      }
    }
    async release() {
      if (!this.lease) return;
      const path = "/api/agent-sessions/" + this.session.id;
      const current = await this.api(path + "/control");
      if (current.lease === null) {
        const session = await this.api(path);
        need(
          session.id === this.session.id &&
            session.generation === this.session.generation &&
            session.state === "closed",
          "Lease unexpectedly disappeared",
        );
      } else {
        need(
          current.lease.owned === true &&
            current.lease.id === this.lease.id &&
            current.lease.generation === this.session.generation,
          "Lease ownership changed",
        );
        await this.api(path + "/control/" + this.lease.id, "DELETE");
      }
      this.lease = null;
      delete this.state.control;
      this.save();
    }
    async timeline() {
      let after = "0",
        promptsAfter;
      const events = new Map(),
        prompts = new Map(),
        seen = new Set();
      for (let page = 0; page < 40; page++) {
        const key = after + "|" + (promptsAfter || "");
        need(!seen.has(key), "Timeline cursor stalled");
        seen.add(key);
        const q = new URLSearchParams({
          after,
          limit: "200",
          ...(promptsAfter ? { promptsAfter } : {}),
        });
        const v = await this.api(
          "/api/conversations/sessions/" + this.session.id + "/timeline?" + q,
        );
        need(
          v.sessionId === this.session.id &&
            v.generation === this.session.generation &&
            v.text === true &&
            Array.isArray(v.events) &&
            Array.isArray(v.prompts),
          "Timeline identity/text unavailable",
        );
        for (const e of v.events) {
          need(/^\d+$/.test(e.sequence), "Invalid sequence");
          events.set(e.sequence, e);
        }
        for (const p of v.prompts) prompts.set(p.id, p);
        if (v.nextAfter === null && v.nextPromptsAfter === null)
          return {
            events: [...events.values()].sort((a, b) =>
              BigInt(a.sequence) < BigInt(b.sequence) ? -1 : 1,
            ),
            prompts: [...prompts.values()],
          };
        after =
          v.nextAfter ||
          (events.size
            ? [...events.keys()].reduce((a, b) =>
                BigInt(a) > BigInt(b) ? a : b,
              )
            : after);
        promptsAfter = v.nextPromptsAfter || promptsAfter;
      }
      throw new Error("Timeline exceeds bounded pagination");
    }
    async checkDelivery() {
      const p = PROFILES[this.run.provider],
        s = await this.sessionRead();
      need(s.state === "open", "Session not open");
      const r = await this.api("/api/agent-sessions/" + s.id + "/router");
      need(
        r.sessionId === s.id &&
          r.generation === s.generation &&
          r.connection?.enabled === true &&
          r.binding?.id === p.binding &&
          r.binding.enabled === true &&
          r.binding.state === "ready" &&
          r.credential?.id === p.credential &&
          r.credential.state === "ready" &&
          r.consumer?.credentialId === p.credential &&
          r.consumer.state === "active" &&
          r.consumer.credentialState === "ready" &&
          r.consumer.remoteAcknowledged === true &&
          r.consumer.revokeRequested === false &&
          Date.parse(r.consumer.credentialExpiresAt) > this.now() &&
          Date.parse(r.consumer.leaseExpiresAt) > this.now(),
        "Managed delivery is not ready",
      );
      const c = await this.api("/api/router-credentials/keys/" + p.credential);
      need(
        c.credential?.id === p.credential &&
          c.credential.bindingId === p.binding &&
          c.credential.routerKeyId === p.key &&
          c.credential.namespace === "default" &&
          c.credential.state === "ready" &&
          c.binding?.currentCredentialId === p.credential,
        "Credential/principal changed",
      );
      const m = await this.api(
        "/api/agent-sessions/" + s.id + "/communication",
      );
      need(
        m.sessionId === s.id && m.status === "ready",
        "Communication unavailable",
      );
      this.record("delivery", {
        sessionId: s.id,
        credentialId: p.credential,
        namespace: "default",
        principal: "key:" + p.key,
      });
    }
    async launch(recovery = false) {
      const p = PROFILES[this.run.provider],
        operationId = this.crypto.randomUUID(),
        previous = recovery ? this.launchRow : null;
      const path = recovery
        ? "/api/agent-sessions/" + this.session.id + "/resume"
        : "/api/agent-launches";
      const body = recovery
        ? { operationId, generation: this.session.generation }
        : {
            operationId,
            profileId: p.id,
            revision: APPROVED_PROFILE_REVISION,
            cwd: "/home/c4/.local/state/relay-holder/workspaces/" + p.id,
          };
      const first = await this.mutate(path, body);
      need(uuid(first.id), "Missing launch ID; inspect operation");
      this.state.activeLaunch = { id: first.id, operationId };
      this.save();
      const deadline = Math.min(
        this.now() + 120000,
        Date.parse(first.expires_at),
      );
      need(Number.isFinite(deadline), "Launch expiry missing");
      while (this.now() < deadline) {
        this.check();
        const rows = await this.api("/api/agent-launches");
        need(Array.isArray(rows), "Launch inventory unavailable");
        const l = rows.find(
          (r) => r.id === first.id && r.operation_id === operationId,
        );
        need(l, "Launch left newest100; inspect without relaunch");
        need(
          !["withdrawn", "rejected", "unknown"].includes(l.state),
          "Launch refused/uncertain; no relaunch",
        );
        if (l.state === "open") {
          need(
            l.profile_id === p.id &&
              l.profile_revision === APPROVED_PROFILE_REVISION &&
              l.cwd === "/home/c4/.local/state/relay-holder/workspaces/" + p.id,
            "Launch profile/workspace changed",
          );
          need(
            l.receipt?.status === "completed" &&
              l.receipt.result?.sessionId === l.session_id &&
              uuid(l.receipt.result.nativeSessionId) &&
              l.holder_id === this.holderId &&
              l.holder_journal_id === this.journalId &&
              l.native_recovery_version === 2 &&
              l.receipt.result.nativeRecoveryVersion === 2,
            "Incomplete/mismatched Pi launch receipt",
          );
          if (recovery)
            need(
              l.session_id !== previous.session_id &&
                l.resume_from === previous.session_id &&
                l.receipt.result.nativeSessionId ===
                  previous.receipt.result.nativeSessionId,
              "Recovery identity changed",
            );
          this.launchRow = l;
          this.session = await this.api("/api/agent-sessions/" + l.session_id);
          this.closeIntent = null;
          this.record("launch", {
            id: l.id,
            operationId,
            sessionId: l.session_id,
            profileId: l.profile_id,
            revision: l.profile_revision,
            nativeSessionId: l.receipt.result.nativeSessionId,
            holderId: l.holder_id,
            journalId: l.holder_journal_id,
            recoveryVersion: 2,
            recovery,
          });
          await this.checkDelivery();
          await this.acquire();
          return;
        }
        await this.sleep(2000);
      }
      throw new Error(
        "Launch deadline; preserve operation and inspect, never relaunch",
      );
    }
    async command(kind, fields = {}) {
      this.check();
      const s = await this.sessionRead();
      need(s.state === "open" && this.lease, "Session/control unavailable");
      const body = {
        operationId: this.crypto.randomUUID(),
        leaseId: this.lease.id,
        generation: s.generation,
        kind,
        ...(["prompt", "set-config"].includes(kind)
          ? { controlEpoch: s.control_epoch }
          : {}),
        ...fields,
      };
      const intent = { body, sessionId: s.id, deadline: this.now() + 120000 };
      if (kind === "prompt") this.pending = intent; // Preserve even when POST's response is lost.
      if (kind === "close") this.closeIntent = intent;
      const row = await this.mutate(
        "/api/agent-sessions/" + s.id + "/commands",
        body,
      );
      return { ...intent, row };
    }
    async operation(op) {
      const v = await this.api(
        "/api/agent-sessions/" +
          op.sessionId +
          "/operations/" +
          op.body.operationId,
      );
      need(v.command, "Operation not visible; preserve ID and inspect");
      completed(v.command, { ...op.body, sessionId: op.sessionId });
      return v.command;
    }
    async finishCommand(op) {
      while (this.now() < op.deadline) {
        const row = await this.operation(op);
        if (row.state === "completed") return row;
        await this.sleep(2000);
      }
      throw new Error("Command deadline; inspect saved operation");
    }
    async decide(expected, kind) {
      const list = await this.api(
        "/api/agent-sessions/" + this.session.id + "/permissions",
      );
      need(Array.isArray(list), "Permissions unavailable");
      const pending = list.filter((r) => r.state === "pending");
      need(pending.length <= 1, "Concurrent/unexpected permission requests");
      if (!pending.length) return false;
      const d = await this.api("/api/agent-permissions/" + pending[0].id);
      need(expected, "Unexpected tool permission");
      const optionId = permission(
        d,
        this.session.id,
        this.session.generation,
        expected,
        kind,
        this.now(),
      );
      await this.ask(
        kind === "allow_once"
          ? "Approve exactly one marker write"
          : "Deny exactly one marker write",
        d.context.title +
          "\n" +
          d.context.description +
          "\nOnce-only option: " +
          optionId,
        undefined,
        Math.min(30000, Date.parse(d.expires_at) - this.now()),
      );
      const fresh = await this.api("/api/agent-permissions/" + d.id);
      need(
        permission(
          fresh,
          this.session.id,
          this.session.generation,
          expected,
          kind,
          this.now(),
        ) === optionId,
        "Permission changed while reviewing",
      );
      await this.mutate("/api/agent-permissions/" + d.id + "/decision", {
        operationId: this.crypto.randomUUID(),
        generation: this.session.generation,
        outcome: { outcome: "selected", optionId },
      });
      const deadline = Math.min(this.now() + 30000, Date.parse(d.expires_at));
      while (this.now() < deadline) {
        const delivered = await this.api("/api/agent-permissions/" + d.id);
        need(
          delivered.session_id === this.session.id &&
            delivered.generation === this.session.generation &&
            !["expired", "cancelled", "interrupted"].includes(
              delivered.state,
            ) &&
            !["rejected", "unknown"].includes(delivered.delivery),
          "Permission delivery failed/uncertain",
        );
        if (
          delivered.state === "resolved" &&
          delivered.delivery === "accepted"
        ) {
          need(
            delivered.decision?.outcome === "selected" &&
              delivered.decision.optionId === optionId,
            "Permission decision changed",
          );
          this.record("permission", {
            requestId: d.id,
            kind,
            sessionId: this.session.id,
            delivery: "accepted",
            execution: "unknown; marker evidence required",
          });
          return true;
        }
        await this.sleep(2000);
      }
      throw new Error("Permission delivery deadline");
    }
    async prompt(
      text,
      {
        expectedText,
        tool,
        decision,
        cancel = false,
        notice,
        backendRefusal = false,
      } = {},
    ) {
      this.check();
      need(
        ++this.run.prompts <= (this.run.effort ? 4 : 12),
        "Prompt budget exceeded",
      );
      this.save();
      const base = await this.timeline(),
        before = new Set(base.events.map((e) => e.sequence));
      const op = await this.command("prompt", { text });
      this.pending = op;
      let approved = false,
        cancelled = false,
        result;
      while (this.now() < op.deadline) {
        this.check();
        const s = await this.sessionRead();
        need(s.state === "open", "Session stopped during prompt");
        if (await this.decide(approved ? null : tool, decision))
          approved = true;
        const row = await this.operation(op);
        if (cancel && !cancelled && row.state === "accepted") {
          const live = await this.timeline();
          const producing = live.events.some(
            (e) =>
              !before.has(e.sequence) &&
              e.event.type === "agent_message_chunk" &&
              e.event.text?.length,
          );
          if (producing) {
            const stop = await this.command("cancel");
            const ack = await this.finishCommand(stop);
            need(
              ack.receipt.result?.cancelSent === true,
              "Cancel not delivered",
            );
            cancelled = true;
          }
        }
        if (row.state === "completed") {
          result = row;
          break;
        }
        await this.sleep(2000);
      }
      need(result, "Prompt deadline");
      this.pending = null;
      const stop = result.receipt.result?.stopReason;
      if (cancel)
        need(
          cancelled && stop === "cancelled",
          "Cancellation not exercised while active",
        );
      else
        need(
          stop === "end_turn" || (backendRefusal && stop === "refusal"),
          "Unexpected native stop reason: " + stop,
        );
      // Connector receipts and journal projection arrive independently. Bound the
      // final projection wait; never submit a second prompt to fill missing text.
      let all, added, messages;
      const projectionDeadline = Math.min(op.deadline, this.now() + 15000);
      do {
        all = await this.timeline();
        added = all.events.filter((e) => !before.has(e.sequence));
        messages = added
          .filter(
            (e) =>
              e.event.kind === "update" &&
              e.event.type === "agent_message_chunk",
          )
          .map((e) => e.event.text || "")
          .join("");
        if (
          (expectedText && messages.trim() === expectedText) ||
          (notice && messages.includes(notice)) ||
          (backendRefusal && messages.includes(CONFIGURATION_CHANGED_NOTICE)) ||
          (!expectedText &&
            !notice &&
            !backendRefusal &&
            all.prompts.some(
              (p) => p.id === op.row.id && p.state === "completed",
            ))
        )
          break;
        await this.sleep(1000);
      } while (this.now() < projectionDeadline);
      const toolEvents = added.filter((e) =>
        /tool_call/.test(e.event.type || ""),
      );
      need(tool || toolEvents.length === 0, "Unexpected tool activity");
      if (tool) need(approved, "Expected permission path not exercised");
      if (expectedText)
        need(messages.trim() === expectedText, "Unexpected assistant result");
      if (notice)
        need(
          messages.includes(notice),
          "Expected native control outcome missing",
        );
      if (backendRefusal)
        need(
          messages.includes(CONFIGURATION_CHANGED_NOTICE),
          "Specific configuration refusal unavailable; generic failure is inconclusive",
        );
      const otherPrompts = all.prompts.filter(
        (p) => !base.prompts.some((b) => b.id === p.id) && p.id !== op.row.id,
      );
      need(otherPrompts.length === 0, "Concurrent prompt detected");
      this.record("prompt", {
        operationId: op.body.operationId,
        sessionId: op.sessionId,
        stopReason: stop,
        expectedText: expectedText || null,
        expectedNotice: notice || null,
        permissionExercised: approved,
        cancelExercised: cancelled,
      });
      return messages;
    }
    async evidence(label, phase, zero = false) {
      const boundary = this.collectionBoundary(label);
      const value = await this.ask(
        label,
        "Paste operator JSON: {trajectory: the fresh collector snapshot, marker: fresh observe.py report if requested, coverageConfirmed:true}. Same scope/process/database and continuously enabled recording required. " +
          (phase ? "Marker phase=" + phase : "No marker report needed.") +
          " Begin collection at or after " +
          new Date(boundary.notBefore).toISOString() +
          "; all browser/collector/observer clocks must be verified within5s. Do not reuse a pre-action report.",
        (v) => {
          need(
            v?.coverageConfirmed === true,
            "Operator coverage confirmation missing",
          );
          snapshot(v.trajectory, this.run.provider);
          this.collectedAfter(boundary, v.trajectory, phase ? v.marker : null);
          need(
            this.now() - v.trajectory.observedAt >= -5000 &&
              this.now() - v.trajectory.observedAt < 120000,
            "Trajectory observation stale",
          );
          const added = delta(
            this.run.lastSnapshot,
            v.trajectory,
            this.run.provider,
          );
          if (zero) need(added === 0, "Expected zero recorded attempts");
          need(
            v.trajectory.totalAttempts - this.run.firstSnapshot.totalAttempts <=
              (this.run.effort ? 6 : 20),
            "Observed generation budget exceeded",
          );
          return {
            trajectory: snapshot(v.trajectory, this.run.provider),
            ...(phase
              ? {
                  marker: marker(
                    v.marker,
                    this.run.provider,
                    this.run.runId,
                    phase,
                    this.now(),
                  ),
                }
              : {}),
            added,
          };
        },
      );
      this.run.lastSnapshot = value.trajectory;
      this.record("operator-observation", {
        label,
        trajectory: value.trajectory,
        ...(phase ? { marker: value.marker } : {}),
        recordedAttemptDelta: value.added,
        coverage: "operator-confirmed; not independently observed by browser",
        collectionBoundary: boundary,
      });
      return value;
    }
    async close() {
      if (!this.session) return;
      this.closing = true;
      const renewalDeadline = this.now() + 16000;
      while (this.renewing && this.now() < renewalDeadline)
        await this.sleep(100);
      need(!this.renewing, "Outstanding lease renewal");
      const s = await this.sessionRead();
      if (s.state === "open") {
        const op = await this.command("close"),
          row = await this.finishCommand(op);
        need(
          row.receipt.result?.cancelSent === true,
          "Close notification missing",
        );
        const end = this.now() + 30000;
        while ((await this.sessionRead()).state === "open" && this.now() < end)
          await this.sleep(2000);
      }
      need(this.session.state === "closed", "Session not cleanly closed");
      await this.release();
      this.record("closed", { sessionId: this.session.id });
      this.closing = false;
    }
    async setEffort(value) {
      const current = await this.sessionRead();
      const v = await this.api(
        "/api/agent-sessions/" + this.session.id + "/settings",
      );
      const option = v.snapshot?.configOptions?.find(
        (o) => o.id === "thought_level",
      );
      need(
        v.generation === current.generation &&
          v.controlEpoch === current.control_epoch &&
          v.snapshot?.configOptions?.length === 1 &&
          option &&
          canonical(option.options.map((o) => o.id)) ===
            canonical(["off", "low", "high"]),
        "Effort ceiling/generation changed",
      );
      if (value === "low")
        need(
          option.currentValue === "off",
          "Fresh effort session must start at provider default",
        );
      const op = await this.command("set-config", {
        settingsRevision: v.snapshot.revision,
        configId: "thought_level",
        value,
      });
      const row = await this.finishCommand(op);
      need(
        row.receipt.result?.settingsApplied === true,
        "Settings not applied",
      );
      const after = await this.api(
        "/api/agent-sessions/" + this.session.id + "/settings",
      );
      const session = await this.sessionRead();
      need(
        after.generation === session.generation &&
          after.controlEpoch === session.control_epoch &&
          after.snapshot?.configOptions?.find((o) => o.id === "thought_level")
            ?.currentValue === value,
        "Settings acknowledgement differs",
      );
      this.record("effort", { sessionId: this.session.id, value });
    }
    async phase(provider, effort = false) {
      const p = PROFILES[provider],
        runId = effort ? EFFORT_RUN : p.run;
      this.run = {
        provider,
        runId,
        effort,
        prompts: 0,
        deadline: this.now() + (effort ? 600000 : 900000),
      };
      this.state.runs.push(this.run);
      this.save();
      const profiles = await this.api("/api/agent-profiles");
      const match = profiles.filter(
        (r) =>
          r.id === p.id &&
          r.revision === APPROVED_PROFILE_REVISION &&
          r.enabled === true,
      );
      need(
        match.length === 1 &&
          (await digest(this.crypto, match[0].definition)) === p.definitionHash,
        "Approved profile absent/drifted; do not overwrite",
      );
      const pre = await this.ask(
        provider +
          (effort ? " effort" : " lifecycle") +
          ": operator pre-launch handoff",
        "Paste {trajectory: fresh scoped snapshot, marker: baseline observe.py report, coverageConfirmed:true, holderReadyConfirmed:true, routeConfirmed:true, clocksWithin5s:true}. Operator must verify browser/collector/observer clocks differ by at most 5 s, and freshly confirm holder " +
          this.holderId +
          ", zero unrelated work, published pins, one attempt/no fallback; Codex target codex-3/gpt-6-astra. Run UUID " +
          runId +
          ". This starts the first actual launch; no probe.",
        (v) => {
          need(
            v?.coverageConfirmed === true &&
              v.holderReadyConfirmed === true &&
              v.routeConfirmed === true &&
              v.clocksWithin5s === true,
            "Operator handoff incomplete",
          );
          need(
            this.now() - v.trajectory.observedAt < 120000 &&
              v.trajectory.observedAt <= this.now() + 5000,
            "Stale trajectory baseline",
          );
          return {
            marker: marker(v.marker, provider, runId, "baseline", this.now()),
            trajectory: snapshot(v.trajectory, provider),
          };
        },
      );
      this.run.firstSnapshot = pre.trajectory;
      this.run.lastSnapshot = pre.trajectory;
      this.record("pre-launch", {
        marker: pre.marker,
        trajectory: pre.trajectory,
      });
      await this.launch();
      const openBoundary = this.collectionBoundary("Post-Open marker baseline");
      const post = await this.ask(
        "Open complete: verify delivery before prompting",
        "Paste {marker: post-open baseline report, deliveryConfirmed:true}. Operator verifies file UID/mode, delivery/install receipts, same principal, holder/profile readiness. Preserve original pre-launch report; this observation cannot replace it. Observe at or after " +
          new Date(openBoundary.notBefore).toISOString() +
          ".",
        (v) => {
          need(v?.deliveryConfirmed === true, "Delivery handoff missing");
          const m = marker(v.marker, provider, runId, "baseline", this.now());
          this.collectedAfter(openBoundary, null, m);
          unchangedBaseline(pre.marker, m);
          return { marker: m };
        },
      );
      this.record("post-open", {
        marker: post.marker,
        delivery: "operator-confirmed",
        collectionBoundary: openBoundary,
      });
      await this.evidence(
        "Import/Open made no recorded provider attempts",
        null,
        true,
      );
      if (effort) await this.effort();
      else await this.lifecycle();
      await this.close();
      await this.evidence(
        "Final settled evidence",
        effort ? null : "recovered",
        true,
      );
      this.run.status = "passed-with-live-wire-unobserved";
      this.save();
      this.session = null;
      this.run = null;
    }
    async lifecycle() {
      await this.prompt(
        "Reply exactly READY. Do not use tools or modify files.",
        { expectedText: "READY" },
      );
      const known = await this.evidence(
        "Confirm known READY request recording",
        "baseline",
      );
      need(known.added >= 1, "Recorder coverage not exercised");
      const prefix =
        "Request permission to run exactly the following shell command once. Do not run any other command, change the command, retry after denial, or modify any other file. After the decision, report the outcome and stop. COMMAND: ";
      const denied = markerCommand(this.run.provider, this.run.runId, "denied"),
        effects = markerCommand(this.run.provider, this.run.runId, "effects");
      await this.prompt(prefix + denied, {
        tool: denied,
        decision: "reject_once",
      });
      await this.evidence("Denied marker remains absent", "denied");
      await this.prompt(prefix + effects, {
        tool: effects,
        decision: "allow_once",
      });
      await this.evidence("Approved marker appears exactly once", "approved");
      const history = await this.prompt("/relay-history", {
        notice: "Completed turns: ",
      });
      await this.evidence(
        "History listing made no recorded attempts",
        null,
        true,
      );
      const ids = history.match(
        /Completed turns: ([a-f0-9]{8}(?:, [a-f0-9]{8})*)/,
      );
      need(ids, "Completed turn IDs unavailable");
      const turn = ids[1].split(", ").at(-1);
      await this.prompt("/relay-branch " + turn, {
        notice: "Independent branch selected.",
      });
      await this.evidence(
        "Branch selection made no recorded attempts",
        null,
        true,
      );
      await this.prompt(
        "Reply exactly BRANCH_OK. Do not use tools, repeat earlier commands, or modify files.",
        { expectedText: "BRANCH_OK" },
      );
      await this.evidence("Independent branch completion", "approved");
      await this.prompt("/relay-compact", { notice: "History compacted." });
      await this.evidence("Compaction settled", "approved");
      await this.prompt(
        "Reply exactly COMPACT_OK. Do not use tools, repeat earlier commands, or modify files.",
        { expectedText: "COMPACT_OK" },
      );
      await this.evidence("Compacted history continuation", "approved");
      await this.prompt(
        "Without using tools or modifying files, count upward from 1, one integer per line, until 500.",
        { cancel: true },
      );
      await this.evidence("Cancellation settled; no tool effect", "approved");
      await this.close();
      const status = await this.api(
        "/api/agent-sessions/" + this.session.id + "/native-recovery",
      );
      need(
        status.available === true && status.kind === "pi",
        "Same-holder recovery unavailable",
      );
      await this.ask(
        "Recover retained history once",
        "Operator confirms original holder/journal " +
          this.holderId +
          " / " +
          this.journalId +
          ", preserved marker and no unknown tool effects. Recover into a new Relay session with the original native UUID. Unknown-effect acknowledgement is NOT submitted.",
      );
      await this.launch(true);
      await this.evidence(
        "Recovery imported history without provider dispatch",
        "recovered",
        true,
      );
      const choice = await this.ask(
        "Inspect retained branch before continuation",
        "After operator inspection, enter exactly {reconcile:true} only if explicit reconstruction is required; otherwise {reconcile:false}. This never acknowledges unknown tool effects.",
        (v) => {
          need(
            typeof v?.reconcile === "boolean",
            "Explicit branch decision required",
          );
          return v.reconcile;
        },
      );
      if (choice) {
        await this.prompt("/relay-reconcile confirmed", {
          notice: "Previous branch retired.",
        });
        await this.evidence(
          "Explicit reconstruction made no recorded attempts",
          "recovered",
          true,
        );
      }
      await this.prompt(
        "Reply exactly RECOVERED_OK. Do not use tools, repeat earlier commands, or modify files.",
        { expectedText: "RECOVERED_OK" },
      );
      await this.evidence(
        "Recovered continuation preserved effects",
        "recovered",
      );
    }
    async effort() {
      await this.setEffort("low");
      await this.prompt(
        "Reply exactly EFFORT_LOW_OK. Do not use tools, access files or modify anything.",
        { expectedText: "EFFORT_LOW_OK" },
      );
      const low = await this.evidence(
        "Low effort completed and recorded",
        null,
      );
      need(low.added >= 1, "Effort recorder coverage missing");
      await this.setEffort("high");
      await this.prompt(
        "Reply exactly EFFORT_HIGH_PROBE. Do not use tools, access files or modify anything.",
        { backendRefusal: true },
      );
      await this.evidence(
        "Changed effort refused before recorded dispatch",
        null,
        true,
      );
      await this.ask(
        "Explicitly retire the settled prior branch",
        "Review the refusal, applied high setting and zero-attempt evidence. Confirm explicit /relay-reconcile confirmed; no tool effect acknowledgement is sent.",
      );
      await this.prompt("/relay-reconcile confirmed", {
        notice: "Previous branch retired.",
      });
      await this.evidence(
        "Explicit reconstruction made no recorded attempts",
        null,
        true,
      );
      await this.prompt(
        "Reply exactly EFFORT_HIGH_OK. Do not use tools, access files or modify anything.",
        { expectedText: "EFFORT_HIGH_OK" },
      );
      await this.evidence("High effort completed on successor branch", null);
    }
    async cleanup() {
      if (this.cleaning || this.cleanupAttempted) return;
      this.cleaning = true;
      this.cleanupAttempted = true;
      let step = "inspect-state",
        operationId = null;
      const dispatch = async (path, body) => {
        if (body.leaseId)
          need(
            Date.parse(this.lease?.expires_at) > this.now() + 1000,
            "Cleanup lease expired; no command dispatched",
          );
        operationId = body.operationId;
        const item = { path, body, status: "cleanup-dispatch" };
        this.state.operations.push(item);
        this.save();
        try {
          await this.api(path, "POST", body);
          item.status = "received";
        } catch (error) {
          item.status = "uncertain";
          throw error;
        } finally {
          this.save();
        }
      };
      try {
        // Narrowing actions only; never launch, prompt, approve, reconcile or take a lease here.
        if (
          this.session &&
          !this.lease &&
          this.state.controlAcquisition?.status === "uncertain"
        ) {
          await this.inspectAcquisition();
          this.record("cleanup", {
            status: "unresolved-lease-acquisition-owner-inspection-required",
          });
          return;
        }
        if (!this.session || !this.lease) {
          const closed = new Set(
            this.state.evidence
              .filter((e) => e.kind === "closed")
              .map((e) => e.sessionId),
          );
          const settled = new Set(
            this.state.evidence
              .filter((e) => e.kind === "launch" && closed.has(e.sessionId))
              .map((e) => e.operationId),
          );
          const launches = this.state.operations.filter(
            (op) =>
              !settled.has(op.body.operationId) &&
              (op.path === "/api/agent-launches" ||
                /^\/api\/agent-sessions\/[^/]+\/resume$/.test(op.path)),
          );
          if ((this.session && !closed.has(this.session.id)) || launches.length)
            this.record("cleanup", {
              status: this.session
                ? "open-session-without-lease; operator-close-required"
                : "uncertain-launch; operator-inspection-and-close-required",
              sessionId: this.session?.id || null,
              generation: this.session?.generation || null,
              launchId: this.state.activeLaunch?.id || null,
              operations: launches.map((op) => ({
                path: op.path,
                operationId: op.body.operationId,
                status: op.status,
              })),
            });
          return;
        }
        const path = "/api/agent-sessions/" + this.session.id;
        step = "inspect-session-and-control";
        const s = await this.api(path),
          lease = await this.api(path + "/control");
        need(
          s.generation === this.session.generation && !s.rebinding,
          "Cleanup session changed",
        );
        if (s.state === "closed" && lease.lease === null) {
          await this.release();
          this.record("cleanup", {
            status:
              "session-already-closed; effects-require-receipt-inspection",
          });
          return;
        }
        need(
          lease.lease?.owned === true && lease.lease.id === this.lease.id,
          "Cleanup authority changed; owner inspection required",
        );
        step = "inspect-permissions";
        const list = await this.api(path + "/permissions");
        for (const p of list.filter((p) => p.state === "pending")) {
          const body = {
            operationId: this.crypto.randomUUID(),
            generation: s.generation,
            outcome: { outcome: "cancelled" },
          };
          step = "cancel-permission";
          await dispatch("/api/agent-permissions/" + p.id + "/decision", body);
        }
        if (s.state === "open") {
          const narrow = async (kind) => {
            step = "dispatch-" + kind;
            operationId = null;
            let intent = kind === "close" ? this.closeIntent : null;
            if (!intent) {
              const body = {
                operationId: this.crypto.randomUUID(),
                leaseId: this.lease.id,
                generation: s.generation,
                kind,
              };
              intent = { body, sessionId: s.id, deadline: this.now() + 30000 };
              if (kind === "close") this.closeIntent = intent;
              try {
                await dispatch(path + "/commands", body);
              } catch (error) {
                if (
                  !this.state.operations.some(
                    (op) => op.body.operationId === body.operationId,
                  )
                )
                  throw error; // Expiry refused dispatch; no operation to inspect.
                // POST may have committed. Read the original operation only.
              }
            }
            // If dispatch was uncertain, inspect the SAME operation. Never resubmit it.
            step = "inspect-" + kind + "-receipt";
            operationId = intent.body.operationId;
            const receipt = await this.finishCommand({
              ...intent,
              deadline: this.now() + 30000,
            });
            need(
              receipt.receipt.result?.cancelSent === true,
              "Cleanup notification not delivered",
            );
          };
          if (this.pending) {
            await narrow("cancel");
            step = "inspect-prompt-receipt";
            operationId = this.pending.body.operationId;
            const receipt = await this.finishCommand({
              ...this.pending,
              deadline: this.now() + 30000,
            });
            need(
              [
                "end_turn",
                "max_tokens",
                "max_turn_requests",
                "refusal",
                "cancelled",
              ].includes(receipt.receipt.result?.stopReason),
              "Prompt outcome unresolved; inspect before Close/release",
            );
            this.pending = null;
          }
          await narrow("close");
          step = "confirm-session-closed";
          const end = this.now() + 30000;
          let closed = false;
          while (this.now() < end) {
            if ((await this.api(path)).state === "closed") {
              closed = true;
              break;
            }
            await this.sleep(2000);
          }
          need(closed, "Close unresolved; retain lease evidence");
        }
        step = "release-control";
        await this.release();
        this.record("cleanup", { status: "closed-and-released" });
      } catch (error) {
        const safeReasons = [
          "Cleanup lease expired; no command dispatched",
          "Cleanup session changed",
          "Cleanup authority changed; owner inspection required",
          "Cleanup notification not delivered",
          "Prompt outcome unresolved; inspect before Close/release",
          "Close unresolved; retain lease evidence",
          "Command deadline; inspect saved operation",
        ];
        this.record("cleanup", {
          status: "unresolved-owner-inspection-required",
          reason: safeReasons.includes(error.message)
            ? error.message
            : "Request or receipt inspection failed",
          step,
          operationId,
          sessionId: this.session?.id || null,
          generation: this.session?.generation || null,
        });
      } finally {
        this.cleaning = false;
      }
    }
    async revocationInventory() {
      const inventory = await this.api("/api/router-credentials");
      need(
        Array.isArray(inventory.bindings) &&
          inventory.bindings.length < 500 &&
          Array.isArray(inventory.credentials) &&
          inventory.credentials.length < 1000 &&
          Array.isArray(inventory.consumers) &&
          inventory.consumers.length < 1000,
        "Credential inventory incomplete or truncated",
      );
      const targets = [];
      for (const [provider, p] of Object.entries(PROFILES)) {
        const bindings = inventory.bindings.filter((b) => b.id === p.binding);
        need(bindings.length === 1, "Binding inventory ambiguous");
        const b = bindings[0];
        need(
          b.enabled === true &&
            b.state === "ready" &&
            b.host_id === HOST &&
            b.execution_identity === "c4" &&
            b.current_credential_id === p.credential &&
            b.pending_credential_id === null &&
            integer(b.revision) &&
            b.revision > 0 &&
            integer(b.generation) &&
            b.generation > 0 &&
            b.generation < 128,
          "Binding changed; inspect before revocation",
        );
        const credentials = inventory.credentials
          .filter((c) => c.binding_id === b.id)
          .sort((a, b) => a.generation - b.generation);
        need(
          credentials.length === b.generation &&
            new Set(credentials.map((c) => c.id)).size === credentials.length &&
            credentials.every(
              (c, i) =>
                c.generation === i + 1 &&
                uuid(c.id) &&
                /^key_[a-f0-9]{12,32}$/.test(c.router_key_id),
            ),
          "Credential generations incomplete or ambiguous",
        );
        need(
          credentials.at(-1).id === p.credential &&
            credentials.at(-1).router_key_id === p.key,
          "Current credential or router principal changed",
        );
        need(
          !inventory.consumers.some(
            (c) => c.binding_id === b.id && c.state === "active",
          ),
          "Active consumer remains; revocation requires separate review",
        );
        const generations = [];
        for (const c of credentials) {
          const view = await this.api("/api/router-credentials/keys/" + c.id);
          need(
            view.credential?.id === c.id &&
              view.credential.bindingId === b.id &&
              view.credential.generation === c.generation &&
              view.credential.routerKeyId === c.router_key_id &&
              view.binding?.id === b.id &&
              view.binding.revision === b.revision &&
              view.binding.enabled === true &&
              view.binding.currentCredentialId === p.credential &&
              view.binding.pendingCredentialId === null &&
              Array.isArray(view.consumers) &&
              view.consumers.length < 128 &&
              !view.consumers.some((x) => x.state === "active"),
            "Credential inspection changed or is incomplete",
          );
          generations.push({
            id: c.id,
            generation: c.generation,
            routerKeyId: c.router_key_id,
          });
        }
        targets.push({
          provider,
          bindingId: b.id,
          revision: b.revision,
          generations,
        });
      }
      return targets;
    }
    runExclusive(action) {
      need(!this.activeOperation, "Another owner action is still running");
      const task = Promise.resolve().then(action);
      this.activeOperation = task;
      return task.finally(() => {
        if (this.activeOperation === task) this.activeOperation = null;
      });
    }
    stop() {
      this.stopped = true;
      this.env.cancelCheckpoint?.();
      if (!this.stopPromise) {
        const active = this.activeOperation;
        this.stopPromise = (async () => {
          try {
            await active;
          } catch {
            /* Original action records its failure. */
          }
          await this.cleanup();
        })();
      }
      return this.stopPromise;
    }
    revokeFinal() {
      return this.runExclusive(() => this.revokeFinalRun());
    }
    async revokeFinalRun() {
      need(
        this.env.origin === ORIGIN &&
          !this.stopped &&
          !this.session &&
          !this.lease &&
          !this.pending &&
          !this.run,
        "Final revocation requires settled sessions and no pending work",
      );
      need(
        this.state.status === "phase-a-passed" &&
          this.state.runs.length === 3 &&
          this.state.runs.every(
            (r, i) =>
              r.status === "passed-with-live-wire-unobserved" &&
              r.provider === ["codex", "qwen", "codex"][i] &&
              r.effort === (i === 2),
          ),
        "All three phase-A runs must pass before final owner revocation",
      );
      need(
        !this.state.finalRevocation?.intents.length,
        "Revocation already attempted; inspect saved operations, never restart",
      );
      this.check();
      this.state.finalRevocation = { status: "reviewing", intents: [] };
      this.save();
      try {
        const targets = await this.revocationInventory();
        await this.ask(
          "Review final revocation of both phase-A bindings",
          "This disables ALL generations. Accepted inference may finish. Do not proceed if later approved work still needs either binding. Paste exactly {confirmRevokeAllGenerations:true,noFurtherUse:true,targets:" +
            JSON.stringify(targets) +
            "}. This does not revoke Claude's separate binding or prove renderer cleanup.",
          (v) => {
            need(
              v?.confirmRevokeAllGenerations === true &&
                v.noFurtherUse === true &&
                canonical(v.targets) === canonical(targets),
              "Explicit exact binding/revision/generation confirmation required",
            );
            return true;
          },
        );
        need(
          canonical(await this.revocationInventory()) === canonical(targets),
          "Binding inventory changed during owner review",
        );
        for (const target of targets) {
          this.check();
          const body = {
            operationId: this.crypto.randomUUID(),
            revision: target.revision,
          };
          const path =
            "/api/router-credentials/" + target.bindingId + "/revoke";
          const intent = {
            ...target,
            path,
            body,
            status: "sending",
            at: this.now(),
          };
          this.state.finalRevocation.intents.push(intent);
          this.save();
          try {
            await this.mutate(path, body);
            intent.status = "requested";
          } catch {
            intent.status = "dispatch-uncertain";
          }
          this.save();
          const deadline = Math.min(this.now() + 120000, EXPIRES);
          let confirmed = false;
          while (this.now() < deadline) {
            // Stop forbids the next binding mutation, but retain bounded readback
            // of the immutable request already sent before cleanup can begin.
            need(this.now() < EXPIRES, "Credential window expired");
            let settled = true;
            for (const c of target.generations) {
              const v = await this.api("/api/router-credentials/keys/" + c.id);
              need(
                v.credential?.id === c.id &&
                  v.credential.bindingId === target.bindingId &&
                  v.credential.generation === c.generation &&
                  v.credential.routerKeyId === c.routerKeyId &&
                  v.binding?.id === target.bindingId &&
                  v.binding.generation === target.generations.length &&
                  [target.revision, target.revision + 1].includes(
                    v.binding.revision,
                  ) &&
                  Array.isArray(v.operations) &&
                  v.operations.length < 32 &&
                  Array.isArray(v.consumers) &&
                  v.consumers.length < 128 &&
                  !v.consumers.some((x) => x.state === "active"),
                "Revocation readback changed or is incomplete",
              );
              const operation = v.operations.find(
                (o) => o.id === body.operationId,
              );
              if (operation)
                need(
                  operation.kind === "revoke-binding",
                  "Revocation operation identity changed",
                );
              if (c.generation === 1)
                settled &&=
                  operation?.state === "revoked" &&
                  operation.failureCode === null;
              settled &&=
                v.credential.state === "revoked" &&
                v.credential.revokeRequested === true &&
                v.credential.cleanupPending === false &&
                v.credential.failureCode === null &&
                v.binding.enabled === false &&
                v.binding.state === "revoked" &&
                v.binding.revision === target.revision + 1 &&
                v.binding.currentCredentialId === null &&
                v.binding.pendingCredentialId === null;
            }
            if (settled) {
              confirmed = true;
              break;
            }
            await this.sleep(2000);
          }
          need(
            confirmed,
            "Revocation not confirmed; preserve original operation and inspect without resending",
          );
          intent.status = "router-revoked-and-escrow-cleaned";
          this.save();
        }
        this.state.finalRevocation.status = "awaiting-renderer-evidence";
        this.save();
        const launches = this.state.evidence.filter((e) => e.kind === "launch");
        need(
          launches.length === 5 &&
            new Set(launches.map((l) => l.id)).size === 5,
          "Expected five recorded phase-A launches for cleanup evidence",
        );
        const expected = launches.map((l) => {
          const target = targets.find(
            (t) => PROFILES[t.provider].id === l.profileId,
          );
          need(
            target && uuid(l.sessionId),
            "Unrecognized launch in cleanup evidence",
          );
          return {
            launchId: l.id,
            sessionId: l.sessionId,
            bindingId: target.bindingId,
            credentialId: PROFILES[target.provider].credential,
            generation: target.generations.length,
          };
        });
        const boundary = this.collectionBoundary(
          "Final renderer cleanup evidence",
        );
        const renderer = await this.ask(
          "Separate operator renderer cleanup and marker preservation",
          "Router revocation and escrow cleanup are confirmed; renderer-file absence is NOT observed by this browser. Collect evidence no earlier than " +
            new Date(boundary.notBefore).toISOString() +
            ". Paste {operatorVerified:true,observedUtc:ISO,launches:[...],markers:[Codex recovered report,Qwen recovered report]}. Required launch identities: " +
            JSON.stringify(expected) +
            ". Each launch additionally needs commandId:UUID, cleanedAt:ISO, status:'absent', fileAbsent:true from the operator's exact cleanup receipt and metadata-only file check. 'not-current' alone is insufficient. Preserve histories and marker files; provide no credentials or file contents.",
          (v) => {
            const observed = Date.parse(v?.observedUtc);
            need(
              v?.operatorVerified === true &&
                Number.isFinite(observed) &&
                observed >= boundary.notBefore &&
                observed <= this.now() + 5000 &&
                observed >= this.now() - 120000 &&
                Array.isArray(v.launches) &&
                v.launches.length === expected.length &&
                new Set(v.launches.map((l) => l.launchId)).size ===
                  expected.length,
              "Complete fresh operator renderer evidence required",
            );
            const records = expected.map((e) => {
              const row = v.launches.find((l) => l.launchId === e.launchId),
                cleaned = Date.parse(row?.cleanedAt);
              const launched = Date.parse(
                launches.find((l) => l.id === e.launchId)?.at,
              );
              need(
                row &&
                  Object.keys(e).every((k) => row[k] === e[k]) &&
                  uuid(row.commandId) &&
                  row.status === "absent" &&
                  row.fileAbsent === true &&
                  Number.isFinite(cleaned) &&
                  Number.isFinite(launched) &&
                  cleaned >= launched - 5000 &&
                  cleaned <= observed + 5000,
                "Renderer cleanup receipt or file absence is unconfirmed",
              );
              return {
                ...e,
                commandId: row.commandId,
                cleanedAt: row.cleanedAt,
                status: "absent",
                fileAbsent: true,
              };
            });
            need(
              Array.isArray(v.markers) && v.markers.length === 2,
              "Both preserved marker observations required",
            );
            const markers = ["codex", "qwen"].map((provider) => {
              const reports = v.markers.filter((m) => m.provider === provider);
              need(reports.length === 1, "Marker report ambiguous");
              need(
                Date.parse(reports[0].observedUtc) >= boundary.notBefore,
                "Marker evidence predates final cleanup checkpoint",
              );
              const current = marker(
                reports[0],
                provider,
                PROFILES[provider].run,
                "recovered",
                this.now(),
              );
              const previous = this.state.evidence
                .filter(
                  (e) =>
                    e.kind === "operator-observation" &&
                    e.marker?.provider === provider &&
                    e.marker.runId === PROFILES[provider].run &&
                    e.marker.phase === "recovered",
                )
                .at(-1)?.marker;
              need(
                previous &&
                  integer(previous.files?.effects?.bytes) &&
                  previous.files.effects.bytes > 0 &&
                  current.files.effects.bytes ===
                    previous.files.effects.bytes &&
                  current.files.effects.sha256 ===
                    previous.files.effects.sha256,
                "Preserved marker bytes or hash changed since recovered-phase evidence",
              );
              return current;
            });
            return {
              source:
                "operator-reported; browser did not inspect host files or private renderer receipts",
              observedUtc: v.observedUtc,
              launches: records,
              markers,
            };
          },
        );
        this.record("final-renderer-cleanup", renderer);
        this.state.finalRevocation.status = "confirmed";
        this.state.status = "phase-a-and-grants-closed";
        this.state.completed = false; // Claude, warm and overall plan evidence remain separate.
        this.save();
      } catch (e) {
        this.state.finalRevocation.status = this.state.finalRevocation.intents
          .length
          ? "unresolved-owner-inspection-required"
          : "declined-before-dispatch";
        this.state.finalRevocation.reason = String(e.message).slice(0, 300);
        this.state.completed = false;
        this.save();
        throw e;
      }
    }
    start() {
      return this.runExclusive(() => this.startRun());
    }
    async startRun() {
      need(
        this.env.origin === ORIGIN,
        "Run only in the owner session at " + ORIGIN,
      );
      need(
        !this.env.load(),
        "A saved run already exists. Export/inspect it; never restart uncertain effects",
      );
      need(
        QUALIFIED_INPUTS,
        "Await the qualified patch release and reviewed successor inputs; this draft cannot launch",
      );
      this.check();
      this.state.status = "running";
      this.save();
      try {
        const handoff = await this.ask(
          "Fresh patched-holder handoff",
          "Paste {holderId:UUID,journalId:UUID,nativeRecovery:2,nativeStateReady:true,qualifiedRelease:'v0.13.1',profilesSha256:OBSERVED_PROFILE_FILE_SHA256,holderBinarySha256:OBSERVED_INSTALLED_BINARY_SHA256,operatorVerified:true}. Use the current operator readback after stopped upgrade and explicit seal release, not copied expected hashes. The journal ID may survive while incarnation changes.",
          (v) => {
            need(
              uuid(v?.holderId) &&
                uuid(v?.journalId) &&
                v.nativeRecovery === 2 &&
                v.nativeStateReady === true &&
                v.qualifiedRelease === "v0.13.1" &&
                v.profilesSha256 === PROFILES_SHA256 &&
                v.holderBinarySha256 === HOLDER_BINARY_SHA256 &&
                v.operatorVerified === true,
              "Qualified holder handoff missing",
            );
            return {
              holderId: v.holderId,
              journalId: v.journalId,
              profilesSha256: v.profilesSha256,
              holderBinarySha256: v.holderBinarySha256,
              provenance:
                "operator-reported installed file hashes; not a browser host measurement",
            };
          },
        );
        this.holderId = handoff.holderId;
        this.journalId = handoff.journalId;
        this.record("operator-holder-handoff", handoff);
        await this.phase("codex");
        await this.phase("qwen");
        await this.phase("codex", true);
        this.state.status = "phase-a-passed";
        this.state.phaseACompleted = true;
        this.state.completed = false;
        this.record("limits", {
          liveWireEffortCapOpaque:
            "unobserved; pair with published Pi synthetic qualification",
          remaining:
            "Claude, warm, paired performance, grant revocation and final plan evidence",
        });
      } catch (e) {
        this.stopped = true;
        this.state.status = "stopped";
        this.state.reason = String(e.message).slice(0, 300);
        this.save();
        await this.cleanup();
        throw e;
      } finally {
        this.save();
      }
    }
  }
  function mount(win) {
    need(
      win.location.origin === ORIGIN,
      "Open " + ORIGIN + " in the owner's existing session",
    );
    need(
      !win.document.getElementById("relay-pi-acceptance-panel"),
      "Acceptance panel already installed",
    );
    const d = win.document,
      panel = d.createElement("section");
    panel.id = "relay-pi-acceptance-panel";
    Object.assign(panel.style, {
      position: "fixed",
      zIndex: "2147483647",
      right: "12px",
      top: "12px",
      width: "min(640px,90vw)",
      maxHeight: "90vh",
      overflow: "auto",
      background: "#fff",
      color: "#111",
      padding: "18px",
      border: "2px solid #225",
      font: "14px system-ui",
    });
    const element = (tag, text) => {
      const el = d.createElement(tag);
      if (text) el.textContent = text;
      panel.appendChild(el);
      return el;
    };
    element("h2", "Pi acceptance — Codex, Qwen, Codex effort");
    element(
      "p",
      "Keep this tab visible. Operator observations and explicit once-only decisions remain required. No provider request occurs merely by pasting this file. Download the report before leaving; reloading never resumes mutations.",
    );
    const status = element("p", "Prepared"),
      title = element("h3"),
      instructions = element("pre");
    instructions.style.whiteSpace = "pre-wrap";
    const input = element("textarea");
    input.rows = 9;
    input.style.width = "100%";
    input.placeholder = "Paste requested nonsecret JSON evidence here";
    const proceed = element("button", "Confirm current checkpoint"),
      start = element("button", "Start phase A"),
      stop = element("button", "Stop and clean up"),
      revoke = element("button", "Review final grant revocation"),
      download = element("button", "Download sanitized report");
    proceed.disabled = true;
    revoke.disabled = true;
    let waiting = null,
      active = false;
    const read = () => win.localStorage.getItem(STORE);
    const runner = new Runner({
      origin: win.location.origin,
      crypto: win.crypto,
      fetch: win.fetch.bind(win),
      now: Date.now,
      load: read,
      cancelCheckpoint: () => {
        if (waiting) waiting.reject(new Error("Owner stopped"));
      },
      save: (state) => win.localStorage.setItem(STORE, JSON.stringify(state)),
      ask: (name, text, validate, timeout) =>
        new Promise((resolve, reject) => {
          need(!waiting, "Concurrent checkpoint");
          title.textContent = name;
          instructions.textContent = text;
          input.value = "";
          proceed.disabled = false;
          const timer = win.setTimeout(() => {
            waiting = null;
            proceed.disabled = true;
            reject(new Error("Checkpoint deadline"));
          }, timeout);
          waiting = {
            validate,
            resolve: (v) => {
              win.clearTimeout(timer);
              waiting = null;
              proceed.disabled = true;
              resolve(v);
            },
            reject: (e) => {
              win.clearTimeout(timer);
              waiting = null;
              proceed.disabled = true;
              reject(e);
            },
          };
        }),
    });
    proceed.onclick = () => {
      if (!waiting) return;
      try {
        const v = input.value.trim() ? JSON.parse(input.value) : true;
        const result = waiting.validate(v);
        waiting.resolve(result);
      } catch (e) {
        status.textContent = String(e.message);
      }
    };
    let heartbeat;
    start.onclick = async () => {
      if (active) return;
      active = true;
      start.disabled = true;
      heartbeat = win.setInterval(() => {
        runner.renew().catch(() => {
          runner.stopped = true;
          if (waiting) waiting.reject(new Error("Control renewal failed"));
        });
      }, 20000);
      try {
        await runner.start();
        status.textContent =
          "Phase A passed. Download report; Claude/warm/final cleanup remain.";
        revoke.disabled = false;
      } catch (e) {
        status.textContent =
          "STOPPED: " +
          e.message +
          ". Download report and inspect recorded operation IDs.";
      } finally {
        win.clearInterval(heartbeat);
        active = false;
      }
    };
    stop.onclick = async () => {
      await runner.stop();
      status.textContent = "Stopped; inspect cleanup in report.";
    };
    revoke.onclick = async () => {
      if (active) return;
      active = true;
      revoke.disabled = true;
      try {
        await runner.revokeFinal();
        status.textContent =
          "Phase-A grants revoked; operator renderer evidence recorded. Download report. Overall plan remains incomplete.";
      } catch (e) {
        status.textContent =
          "Revocation unresolved: " +
          e.message +
          ". Inspect saved operation IDs; no automatic resend.";
      } finally {
        active = false;
        revoke.disabled =
          runner.stopped || !!runner.state.finalRevocation?.intents.length;
      }
    };
    download.onclick = () => {
      const bytes = read() || JSON.stringify(runner.state);
      const blob = new Blob([bytes], { type: "application/json" });
      const url = URL.createObjectURL(blob),
        a = d.createElement("a");
      a.href = url;
      a.download =
        "pi-phase-a-" + new Date().toISOString().replaceAll(":", "-") + ".json";
      a.click();
      win.setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    win.addEventListener("beforeunload", (e) => {
      if (active) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
    if (read()) {
      start.disabled = true;
      status.textContent =
        "Saved run exists: export and inspect it. Automatic replay is disabled.";
    }
    if (!QUALIFIED_INPUTS) {
      start.disabled = true;
      status.textContent =
        "Draft held: qualified patch artifacts and successor profile inputs are not installed in this packet. No launch permitted.";
    }
    d.body.appendChild(panel);
  }
  return {
    Runner,
    PROFILES,
    SOURCE,
    CONFIGURATION_CHANGED_NOTICE,
    ROUTER,
    ORIGIN,
    EXPIRES,
    HOLDER,
    canonical,
    digest,
    markerCommand,
    marker,
    unchangedBaseline,
    snapshot,
    delta,
    completed,
    permission,
    mount,
  };
});
