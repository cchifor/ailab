# Bounded Relay session for automated phase A (relay-operator `session`)

## Context

On 2026-11-27 the owner directed: "Automate the implementation, you should not require me to manually execute relay
tasks. Help the worker agent to test and validate the work by himself." Phase A of the dev-worker-1 Pi canary has run
as a browser flow in the owner's own Relay session. The inference session's library
(`docs/runbooks/inference-canary-v0113/owner-phase-a-automation.cjs`, ailab PR #2060, AUTOMATED-PHASE-A.md) wraps the
exported Runner as `createAutomation({directory, predecessorRaw, authenticatedFetch, collectCheckpoint, signal,
maxDurationMs, cleanupTimeoutMs})`. It needs two operator-supplied pieces:

- an `authenticatedFetch` that keeps ONE authenticated Relay session for the whole run. Relay control leases bind
  `actor.userId` AND `actor.sessionHash` (control.ts), so per-call login (`relay-operator.py call`) cannot hold a
  lease;
- a `collectCheckpoint` returning real machine evidence. **That is a separate, later plan (the "observer").**

This plan delivers the first piece: a session broker. The Runner reaches it over a private socket, and it holds
temporary admin only for the bounded run. Admin is needed only for credential rotation and native recovery; everything
else is operator. NOT in scope: an unrestricted proxy; profile, grant, policy or delegation approvals; copied owner
cookies; a membership bypass.

The JIT identity (`relay-operator@ailab.invalid`, password only in OpenBao `af/relay/operator-oidc`) has no membership
at rest. It never makes durable approvals (grants, creation policies, delegations, management authority, repository
issuance), because Relay re-checks the approver's or issuer's CURRENT admin membership when those are used.

**Ordinary credential rotation (verified in v0.14.1, 5f90d03a).** `rotate()` records the actor only in the
`router_credential_operations.actor_id` journal; later reconciliation and `remoteRevoke()` do not recheck it. But
`rotate()` calls `issue()`, which re-enters `actorTenant(actor, "admin")` after recording intent and catches failures,
so HTTP 202 does not prove issuance. The run must read each new credential back to a terminal issuance state before
authority is removed, and report unresolved outcomes as such.

## Approach

### 0. Scope pinned in reviewed code

`relay-session-proxy.py` holds these constants; the orchestrator takes no scope parameters:

- **Origin:** `https://relay.chifor.me`.
- **Bindings:** `a0f57684-747f-4c3e-8d90-e684b7262590` (Codex), `d2871eec-8f5a-4afe-a917-014f0ae8e14d` (Qwen).
- **Profiles:** `1d9a7d6e-4b26-49fd-9053-7b0a22e1baae` and `f25c187b-1e50-4c7c-860e-52947bfdf39e`, at revision 1, each
  with its approved workspace cwd and its binding.
- **Predecessor (GET-only, never in a mutation):** session `45724087-335a-4c55-ad0f-cb278afe608a`, permission
  `70fb1b8e-f7cc-4cc3-b98f-66f83e46d1ab`.

### 1. `scripts/relay-session-proxy.py` (new; stdlib only; pinned python image)

- **Login once** with relay-admin-call.py's `Client` / `login` (imported from the read-only `/tool` mount).
  `/api/auth/me` must say admin, `relay-operator (automation)`. The JIT user id and the server time at login
  (`run_start`) are recorded. The cookie lives only in this process.
- **Transport.** A Unix socket `/run/relay-session/proxy.sock` in a private tmpfs volume shared with the Runner. The
  Runner runs `--network none`, so it reaches only this socket. The proxy's only upstream is the pinned origin.
- **Bounded server.**
  - A fixed pool of 8 handler threads. One slot is reserved for control-lease requests, so stalled clients cannot
    starve the 20 s heartbeat.
  - Client socket read and write timeout: 10 s. Request line plus headers: at most 16 KiB. Body: at most 64 KiB, read
    within the timeout.
- **Trusted reconstruction.** The upstream URL is the origin plus the matched route template, with validated IDs and
  only that route's allowed query keys. The headers are fixed: `Content-Type`, `Origin`, the UA and the cookie.
  Refused, without an upstream call:
  - absolute-form targets, `%2F` / `%2E`, `..`, `//`;
  - duplicate query or JSON keys, unknown JSON keys;
  - `Transfer-Encoding`, or a missing or conflicting `Content-Length`.
- **Routes and schemas.** Anything else gets a local 403.
  - **Operator reads:**
    - GET `/api/router-credentials`, `/api/router-credentials/keys/:cred` (a cred learned from a fixed binding),
      `/api/agent-profiles`, `/api/agent-launches`;
    - GET `/api/agent-sessions/:sid` and its `/router`, `/communication`, `/control`, `/settings`,
      `/operations/:op` and `/permissions`;
    - GET `/api/agent-permissions/:pid`;
    - GET `/api/conversations/sessions/:sid/timeline`, with only the queries `after` (cursor format), `limit`
      (1..200) and `promptsAfter`, each at most once.
  - **Operator writes:**
    - POST `/api/agent-launches` `{operationId, profileId, revision: 1, cwd}`, with the profile and cwd pinned;
    - POST `/api/agent-sessions/:sid/control` `{generation}` or `{generation, leaseId}`, a lease of that sid;
    - DELETE `/api/agent-sessions/:sid/control/:lease`;
    - POST `/api/agent-sessions/:sid/commands` `{operationId, leaseId, generation, kind}`. `kind` is one of:
      - `prompt`, with `controlEpoch` and `text` (≤ 8 KiB);
      - `set-config`, with `controlEpoch`, `settingsRevision`, `configId: "thought_level"` and `value` from a
        fixed set;
      - `cancel`;
      - `close`.

      The prompt and attempt budget stays the Runner's reviewed responsibility.
    - POST `/api/agent-permissions/:pid/decision` `{operationId, generation, outcome}`, where `outcome` is
      `{outcome: "selected", optionId}` or `{outcome: "cancelled"}` (Relay's union). At most once per pid; the pid
      must be learned under a learned sid. The Runner keeps its full permission-input validation.
  - **Admin:**
    - POST `/api/router-credentials/:binding/rotate` `{operationId, revision}`, a fixed binding, at most once per
      binding;
    - GET `/api/agent-sessions/:sid/native-recovery`;
    - POST `/api/agent-sessions/:sid/resume` `{operationId, generation}`. `acknowledgeUnknownToolEffects` is refused
      when true, and removed when false: Relay accepts only an optional literal true.
  - **Binding revoke is excluded.** It disables every generation of a binding, so the final revocation is a separate
    reviewed operation after acceptance.
- **Freshness and provenance.**
  - At login, the proxy snapshots the JIT actor's existing launch `operation_id`s from GET `/api/agent-launches`, and
    refuses any dispatch that reuses one of them.
  - Operation IDs are reserved atomically under a lock before dispatch; a reuse within the run is refused.
  - **A launch is learned** only from a response row that meets all of: `actor_id` is the JIT user; `operation_id` is
    one this run dispatched; `created_at` ≥ `run_start`. In addition:
    - for a launch: `profile_id`, `profile_revision` and `cwd` match the dispatched body;
    - for a resume: `resume_from` and `resume_generation` match the dispatched sid and generation, and the source
      launch's profile, revision and cwd carry over.

    The same rule applies whether the row comes from the POST response or, after a lost response, from a read-only
    GET `/api/agent-launches` reconciliation. A launch is never resent.
  - **The other learned IDs:**
    - a session, from a learned launch's `session_id`;
    - a lease, from POST `/control` (`id`, `generation`, `expires_at`), bound to its sid and generation;
    - a permission, from `/permissions` rows (`id`) of a learned sid;
    - a credential, from inventory rows (`binding_id`) of a fixed binding.

    Cross-session substitution is refused.
- **Responses.** Read incrementally, at most 8 MiB, with `Set-Cookie` removed. Tenant-wide inventories pass unfiltered,
  so the Runner can still detect truncation (documented). No response content is logged.
- **Deadlines.** The orchestrator passes `runner_deadline` and `cleanup_deadline` as wall-clock epochs. The proxy
  converts them once to monotonic time at receipt.
  - The deadline is rechecked immediately before each dispatch, and each upstream call's total budget is the smaller
    of 30 s and the time remaining. A dispatched mutation that is aborted is logged as `uncertain`, never as cancelled.
  - **Cleanup phase** (after `runner_deadline`), only:
    - GETs;
    - renewal of an existing learned lease (same sid and generation), and its DELETE;
    - `cancel` / `close` commands;
    - permission decisions with `outcome: "cancelled"` only.

    Selections and new launches are refused.
  - **At `cleanup_deadline`** everything is refused. The proxy logs out within one 20 s budget covering logout plus
    `/api/auth/me` (which must answer 401), then exits. SIGTERM and SIGINT trigger the same.
  - Exit codes: 0 means logout confirmed, 4 means not confirmed.
- **Evidence.** JSON lines on stderr, flushed per line: time, method, route template, path IDs, operationId, the
  decision and its reason, dispatched yes/no, upstream status (or `timeout` / `uncertain`) and duration. No bodies,
  cookies, tokens or content.

### 2. `scripts/relay-operator.py session` (new subcommand)

```
relay-operator.py session --manifest-ref COMMIT:PATH --bundle DIR --runner-minutes N --evidence-dir DIR
```

1. **Before any authority.**
   - Read the expected bundle manifest (every file + sha256, the entrypoint, the image digest) from a REVIEWED ailab
     commit, for example `gitea/main:docs/runbooks/inference-canary-v0113/automation-bundle.json`, and record that
     commit in the evidence.
   - Copy `--bundle` into a private temp dir. Refuse symlinks, special files, escaping paths and any file not in the
     manifest.
   - Hash every COPIED file against the manifest; the file set must match exactly.
   - Validate the arguments: N at most 45, the evidence dir new and private.
   - Fix `runner_deadline = now + N` and `cleanup_deadline = runner_deadline + 10 min`.
   - Open the evidence files.
2. **Serialize with a fenced Lease.** The Kubernetes Lease is `relay-operator-jit` in namespace `relay`.
   - Acquire it by create, or by a `resourceVersion`-conditioned update when it is expired. The holder is the run id
     and the end is `cleanup_deadline + 5 min`.
   - **Before granting, clear orphan authority:** revoke, then read back `0 memberships, 0 sessions`.
   - Every later destructive step (the revoke in `finally`, the release) re-reads the Lease first and acts only if
     this run is still the holder. A late `finally` never touches a successor.
   - `call` uses the same protocol for its short duration.
3. **Inside one `try/finally`.**
   1. Grant admin.
   2. Start the proxy container, hardened: pinned image, `--read-only`, `--cap-drop ALL`, no-new-privileges, non-root,
      pids and memory limits, `/tool` read-only, the socket tmpfs. The password and deadlines go on stdin. Its stderr
      streams into `proxy.jsonl` from the start.
   3. Wait for READY (at most 120 s, within `cleanup_deadline`).
   4. Run the Runner container from a fixed specification: the manifest's image digest, `--network none`, hardened
      the same way, the verified bundle copy read-only, the socket volume, the manifest's entrypoint, and env
      `RELAY_SESSION_SOCKET`, `RUNNER_DEADLINE`, `CLEANUP_DEADLINE`. No caller Docker arguments. Its stdout (the
      library's snapshots and its final report, as JSON lines) streams continuously into `runner.jsonl`.
   5. At `runner_deadline`, SIGTERM the Runner, which stops and cleans up through the still-valid session while the
      adapter keeps renewing its existing lease. Wait until `cleanup_deadline − 3 min`, then kill.
   6. Stop the proxy with a grace of 30 s, longer than its 20 s logout budget. Capture its exit code and drain its
      logs.
4. **`finally`: bounded, independent stages.** Every subprocess has a timeout, SIGTERM and SIGINT are converted to an
   exception, and each stage catches `BaseException`.
   1. A bounded drain and inspect of both containers' logs and exit status.
   2. Force-remove both containers.
   3. If this run still holds the Lease: revoke and read back `0 memberships, 0 sessions` (rc 3 if not), then end the
      Keycloak SSO sessions (rc 5 on failure).
   4. Release the Lease if this run still holds it.
   - rc precedence: 3, then 4 (proxy logout), then 5, then 6 (evidence capture failed), then the Runner's or the
     deadline's failure.

### 3. Independent reaper (IaC; a separate, second PR)

- **Database.** An idempotent Flux Job, run with the existing superuser Secret, creates:
  - schema `ops`, owned by `relay_owner`;
  - `ops.relay_operator_reap()`, a SECURITY DEFINER function owned by `relay_owner` with a fixed `search_path`, the
    JIT subject as a literal and `statement_timeout` / `lock_timeout` set. It deletes only that subject's sessions and
    memberships and returns the counts;
  - role `relay_operator_reaper` (LOGIN, no other rights) with USAGE on `ops` and EXECUTE on that function only. Its
    password is in a new SOPS Secret.
- **CronJob** `relay-operator-reaper`, every 5 minutes, with `concurrencyPolicy: Forbid`, a deadline and a
  `postgres:16` client image.
  - It takes the same Lease as holder `reaper` only when it is absent or expired (`resourceVersion`-conditioned),
    calls the function, then releases. A live run therefore excludes the reaper, and the reaper excludes a new run.
  - Its ServiceAccount is limited to get/create/update on Lease `relay-operator-jit`.
  - A NetworkPolicy allows the CronJob's pods to reach relay-postgres:5432 and the API server.
- **Alerting.** A PrometheusRule fires on a missed or failed reaper run (kube-state-metrics, CronJob last success
  older than 20 min, or a failed Job).
- This is a best-effort backstop: scheduler, API and database availability bound it. Real runs are gated on it being
  deployed and checked once. The check grants, crashes the orchestrator on purpose, and the reaper removes the
  authority within one schedule.

### 4. Docs

`docs/runbooks/relay.md` "Operator automation identity" gains a `session` subsection:

- the pinned scope, the routes and schemas (a pointer to the script);
- freshness and provenance, the deadlines and the cleanup phase;
- the Lease protocol and the reaper;
- the qualified rotation finding, and that durable approvals stay owner-only;
- that inventory reads are tenant-wide;
- the run gates (below).

### Gates before any real run

Each of these must be true and recorded in the run evidence:

1. The proxy and orchestrator (this plan) are merged; the codex impl review and the reviewbot gate have passed.
2. The reaper (section 3) is deployed and checked once.
3. The inference session's adapter / library and bundle manifest are merged, pinned to the hub source that is
   actually deployed (after v0.14.2, a re-pin once). It performs both rotations and reads issuance back to a terminal
   state.
4. The observer (`collectCheckpoint`, a later plan) is merged, with fixed read-only collectors per checkpoint name.
5. Predecessor command receipts: the actor-scoped `GET /operations/:op` hides the owner's old commands from the JIT
   identity, so the operator exports a read-only, sanitized projection. It carries `id`, `operation_id`,
   `session_id`, `generation`, `kind`, `state`, timestamps, and the native receipt status plus `result.stopReason` /
   `result.cancelSent`, with no prompt text. It is bound to the exact predecessor, the report sha256 and the query
   text, and pinned by sha256. The inference session confirms that its validator accepts it without weakening
   settlement checks.
6. The original browser report `relay-pi-phase-a-20261126-v1` has been recovered byte-exact. Until then: no run.
7. The holder handoff checkpoint is done by the operator (holder-only start, then `resume.sh`) at the window.

## Critical files

- `scripts/relay-session-proxy.py` (new).
- `scripts/relay-operator.py` (`session`, and the Lease protocol in `call` and `session`).
- `scripts/relay-admin-call.py` (unchanged; imported).
- `scripts/tests/test_relay_session_proxy.py`, `scripts/tests/test_relay_operator_session.py` (new).
- `docs/runbooks/relay.md`.
- Second PR: `kubernetes/apps/apps/relay/operator-reaper.yaml` (Job, CronJob, RBAC, NetworkPolicy, PrometheusRule), its
  SOPS Secret and the kustomization entry.

## Verification

1. **Proxy unit and contract tests** (docker python:3.13).
   - Deployed shapes: launch and resume return top-level `id`; the inventory carries `session_id`, `operation_id`,
     `actor_id`, `created_at`, `resume_from`, `resume_generation`; POST `/control` returns `id`, `generation`,
     `expires_at`; permission rows carry `id`; the credential inventory is `{bindings, credentials, consumers}` with
     `binding_id`.
   - Every allowed route passes; every near-miss is refused before upstream.
   - Provenance:
     - a same-actor replay (same operation, profile and cwd, `created_at` before `run_start`);
     - a startup-inventory operationId reused;
     - cross-session substitution, the predecessor used in a mutation;
     - `acknowledgeUnknownToolEffects: true` refused and `false` removed;
     - a second rotate per binding;
     - the permission union, both selected and cancelled;
     - lost-response reconciliation for a launch and for a resume.
   - Bounds: stalled clients during lease renewal, a slow upstream with deadline capping, an oversize request or
     response, framing conflicts.
   - Deadlines: in the cleanup phase, renewal and cancel pass while selection and new launches are refused; after
     `cleanup_deadline` everything is refused; logout runs on SIGTERM.
2. **Orchestrator fault tests** (fakes for docker, kubectl and the proxy):
   - a manifest or bundle mutation during copy, an altered file under an unchanged manifest, a symlink;
   - Lease interleavings: a reaper versus a new run, an expired run's late `finally` versus its successor;
   - a grant transport failure after commit, a READY timeout, crashes, SIGINT and SIGTERM, the deadline during work,
     each cleanup stage failing.
   - Assert that the logs are drained before removal, the stages are independent, nothing is replayed and the rc
     precedence holds.
3. **Live read-only smoke** (operator), with a trivial manifest-pinned bundle:
   - two GETs answer 200;
   - local refusals for `/api/auth/me`, an unlearned sid, a predecessor mutation;
   - a TCP connect from the Runner fails;
   - the readback `0 memberships, 0 sessions`, the Keycloak logout, the Lease released;
   - a concurrent second run is refused.
4. **Reaper PR:** the function and role are tested against a disposable Postgres. It's checked once in production as in
   section 3, and the alert is checked to render.
5. **The first real run** (after all gates):
   - integration evidence for the five launches, including two resumes, the permission decisions and the
     deadline-bounded cleanup;
   - after removal, a read-only database check (the operator's own read-only query, not the JIT identity) that both
     rotated credentials stay ready or installing.
6. **Codex implementation review** before each PR, then the normal reviewbot gate.

<!-- codex-review-status: finalized -->
