# Bounded Relay session for automated phase A (relay-operator `session`)

## Codex Review

- The revision resolves the main transport, fixed-scope, request-reconstruction and binding-revocation concerns; the qualified rotation and login-binding claims match Relay v0.14.1.
- Permission decisions and resume need exact contract corrections, and launch provenance still admits a replay from an earlier run of the same JIT user.
- Cleanup must retain existing control leases while refusing new approvals; deadline enforcement, reaper coordination and reaper privileges need tighter definitions.
- Verify the copied bundle's actual bytes and reviewed provenance, and preserve proxy evidence on failure paths before removing containers.
- Deferring adapter integration is reasonable, but its acceptance gate must include complete predecessor receipts, observer prerequisites and exercised cleanup behavior.

## Context

On 2026-11-27 the owner directed: "Automate the implementation, you should not require me to manually execute relay
tasks. Help the worker agent to test and validate the work by himself." Phase A of the dev-worker-1 Pi canary
(docs/runbooks/inference-canary-v0113/OWNER-PHASE-A.md) has run as a browser flow in the owner's own Relay session. The
inference session will drive it non-interactively through a reviewed adapter around the exported Runner
(owner-phase-a.js). Its access requirement (bus 2276):

- Relay control leases bind `actor.userId` AND `actor.sessionHash` (verified: control.ts). `relay-operator.py call`
  logs in and out per call, so it cannot hold a lease. The run needs ONE authenticated session for its whole duration,
  with a hard deadline and receipt-preserving cleanup.
- Admin is needed only for `POST /api/router-credentials/:id/rotate`, `GET /api/agent-sessions/:id/native-recovery`
  and `POST /api/agent-sessions/:id/resume`. Everything else is operator.
- NOT wanted: an unrestricted proxy; profile, grant or delegation approvals; copied owner cookies; a membership bypass.

The JIT identity (`relay-operator@ailab.invalid`, password only in OpenBao `af/relay/operator-oidc`) has no Relay
membership at rest. Its standing rule is that it never makes durable approvals (grants, creation policies,
delegations, management authority, repository issuance), because Relay re-checks the approver's or issuer's CURRENT
admin membership when those are used.

**Ordinary credential rotation, checked in v0.14.1 (5f90d03a).** `rotate()` records the actor in
`router_credential_operations.actor_id` (an audit journal), and later reconciliation and `remoteRevoke()` do not recheck
that actor's membership. However, `rotate()` calls `issue()`, which re-enters `actorTenant(actor, "admin")` after
recording intent and catches failures, so HTTP 202 does not prove issuance. Therefore the run must read the new
credential back to a terminal issuance state (installing/ready, or an explicit failure) BEFORE authority is removed.
Outcomes left unresolved are reported, never assumed.

## Approach

### 0. Scope pinned in reviewed code, not caller configuration

`relay-session-proxy.py` holds the scope as constants:

- **Origin:** `https://relay.chifor.me`.
- **Bindings:** `a0f57684-747f-4c3e-8d90-e684b7262590` (Codex) and `d2871eec-8f5a-4afe-a917-014f0ae8e14d` (Qwen).
- **Profiles:** `1d9a7d6e-4b26-49fd-9053-7b0a22e1baae` and `f25c187b-1e50-4c7c-860e-52947bfdf39e`, each at revision 1,
  with its approved workspace cwd and its expected binding.
- **Predecessor, READ-ONLY:** session `45724087-335a-4c55-ad0f-cb278afe608a` and permission
  `70fb1b8e-f7cc-4cc3-b98f-66f83e46d1ab`. These are GET-only and can never be a path or body ID in any mutation.

The orchestrator takes no scope parameters. A change of scope is a reviewed code change.

### 1. `scripts/relay-session-proxy.py` (new; stdlib only; pinned python image)

- **Login once** with relay-admin-call.py's `Client` / `login` (imported from the read-only `/tool`). `/api/auth/me`
  must say admin, `relay-operator (automation)`, and the JIT user id, which is recorded for provenance. The cookie
  lives only in this process.
- **Transport: a Unix domain socket,** not TCP. The socket is `/run/relay-session/proxy.sock` in a private tmpfs
  volume shared with the Runner container. The Runner runs with `--network none`, so it can reach nothing but this
  socket; the boundary is enforced, not a trusted-adapter promise. The proxy container alone has network, and its
  only upstream is the pinned origin.
- **Requests are reconstructed from trusted values.**
  - The upstream URL is the origin plus a matched route template, filled with validated IDs and the parameters
    allowed for that route only.
  - Headers are a fixed set: `Content-Type: application/json`, `Origin`, the UA and the cookie. No client header is
    forwarded.
  - Refused: absolute-form targets, a `%2F` or `%2E` encoding, `..`, `//`, duplicate query or JSON keys, both
    `Content-Length` and `Transfer-Encoding`, chunked bodies, bodies over 64 KiB.
- **Allowlist, with strict per-route schemas.** Anything else gets a local 403 without an upstream call.
  - Operator reads:
    - GET `/api/router-credentials`, `/api/router-credentials/keys/:cred` (cred learned from a fixed binding),
      `/api/agent-profiles`, `/api/agent-launches`;
    - GET `/api/agent-sessions/:sid` and its `/router`, `/communication`, `/control`, `/settings`,
      `/operations/:op` and `/permissions`;
    - GET `/api/agent-permissions/:pid`;
    - GET `/api/conversations/sessions/:sid/timeline`, with only the queries `after` (cursor format), `limit`
      (1..200) and `promptsAfter`, each at most once.
  - Operator writes:
    - POST `/api/agent-launches` `{operationId, profileId, revision:1, cwd}`, with the profile and cwd pinned;
    - POST `/api/agent-sessions/:sid/control` `{generation}` or `{generation, leaseId}` (lease of that sid);
    - DELETE `/api/agent-sessions/:sid/control/:lease`;
    - POST `/api/agent-sessions/:sid/commands` `{operationId, leaseId, generation, kind}`. `kind` is one of `prompt`
      (+`controlEpoch`, `text` ≤ 8 KiB), `set-config` (+`controlEpoch`, `settingsRevision`,
      `configId:"thought_level"`, `value` from a fixed set), `cancel` or `close`. The prompt and attempt budget stays
      the Runner's reviewed responsibility;
    - POST `/api/agent-permissions/:pid/decision` `{operationId, optionId, generation}`, once per pid, the pid
      learned under a learned sid.
      <!-- codex: [P1] Relay's PermissionDecision schema requires {operationId, generation, outcome:{outcome:"selected", optionId}} or outcome:{outcome:"cancelled"}; the proposed top-level optionId is rejected and cannot express Runner.cleanup() cancellation. Use the deployed nested union and test both ordinary decisions and cancellation while retaining the Runner's full permission-input validation. -->
  - Admin:
    - POST `/api/router-credentials/:binding/rotate` `{operationId, revision}`, a fixed binding, at most once per
      binding per run;
    - GET `/api/agent-sessions/:sid/native-recovery`;
    - POST `/api/agent-sessions/:sid/resume` `{operationId, generation}`. `acknowledgeUnknownToolEffects` must be
      absent or false.
      <!-- codex: [P2] native-recovery.ts accepts acknowledgeUnknownToolEffects only as an optional literal true, so forwarding false also fails validation. Require omission upstream, normalizing an accepted false away during trusted reconstruction, and cover that case in the contract tests. -->
  - **Binding revoke is NOT in this session.** Revoking a binding disables every generation, including credentials
    in use elsewhere; the final revocation is a separate reviewed operation after acceptance.
- **Learned IDs carry provenance, not loose sets.**
  - A launch is learned from a launch-shaped response (POST `/api/agent-launches` or `/resume`), or from a
    GET `/api/agent-launches` row whose `operation_id` is one this run dispatched AND whose `actor_id` is the JIT user
    AND whose `profile_id` / `profile_revision` match the dispatched body. That second path reconciles a lost POST
    response read-only, without resending the launch.
    <!-- codex: [P2] A resume request has no profileId or revision in its body, so this reconciliation rule cannot match its lost response as written. Record the learned source launch's profile/revision/cwd and require the recovered row's resume_from and resume_generation to match the dispatched sid/generation, applying equivalent provenance checks to direct POST responses. -->
  - A replayed launch receipt that predates this run (wrong actor or operation) is refused.
    <!-- codex: [P1] An earlier run of this same JIT user has the same actor_id, and a Runner can copy its operation_id from the unfiltered inventory; profiles.ts returns the old launch for that actor/operation pair even under a new login. Add independently established run freshness, such as proxy-owned fresh upstream operation IDs or validated server creation-time provenance, and test a replay with the same actor, operation, profile and cwd rather than only mismatched fields. -->
  - A session is learned only from a learned launch's `session_id`.
  - A lease is learned from POST `/control` (`id`, `generation`, `expires_at`) and bound to its sid and generation.
  - A permission is learned from `/permissions` rows of a learned sid, with `id`.
  - A credential is learned from inventory rows (`binding_id`) of a fixed binding.
  - Operation IDs are reserved atomically under a lock BEFORE dispatch; a reuse is refused.
  - Cross-session substitution is refused: a lease, permission or operation used under a sid other than its own.
- **Bounds.**
  - The server is a `ThreadingHTTPServer` on the socket, so a slow request cannot starve the Runner's 20 s
    lease heartbeat.
    <!-- codex: [P2] ThreadingHTTPServer creates unbounded handler threads, and the upstream timeout does not bound clients stalling request headers/body reads or response writes. Bound client I/O and concurrent handlers, preserving heartbeat/cleanup capacity, and test stalled Unix-socket clients alongside lease renewal. -->
  - Each upstream call has a 30 s total wall-clock deadline (connect, headers and body) and at most 8 MiB of
    response, read incrementally; it is aborted otherwise.
  - Responses pass through with `Set-Cookie` removed.
  - Inventories are tenant-wide and are NOT filtered, so the Runner can still detect truncation. The disclosure stays
    on the operator's machine, and no response content is logged.
- **Deadlines.** The orchestrator passes the absolute `cleanup_deadline` (monotonic-equivalent epoch, fixed before the
  grant) and an earlier `runner_deadline`.
  - After `runner_deadline` only cleanup-shaped requests pass: GETs, DELETE `/control`, `cancel` / `close` commands
    and permission decisions.
    <!-- codex: [P1] This excludes renewal of an already owned control lease, although control.ts caps leases at 60 seconds and requires a live lease even for cancel/close; Runner.renew() also stops when cleaning is true. Permit renewal only of the existing learned sid/generation/lease during cleanup and make the adapter keep that heartbeat alive, or the advertised cleanup window can expire before Close is dispatched. -->
    <!-- codex: [P1] Unrestricted permission decisions are not cleanup-only: selecting an allow option can authorize new tool effects after runner_deadline. Restrict this phase to outcome:{outcome:"cancelled"} for already learned permissions and test that approval selections are refused. -->
  - At `cleanup_deadline` everything is refused, the proxy logs out (logout plus `/api/auth/me` must answer 401) and
    exits.
    <!-- codex: [P1] Define the wall-clock-to-monotonic conversion and recheck deadlines immediately before upstream dispatch, with in-flight I/O capped by the remaining budget. A fixed 30-second call timeout otherwise outlives a nearby cutoff; aborting HTTP must leave an already dispatched mutation recorded as uncertain, not imply that Relay cancelled it. -->
  - SIGTERM and SIGINT log out first.
  - Exit codes: 0 means logout confirmed, 4 means not confirmed.
- **Evidence log** (stderr, flushed per line, JSON). One line per request: time, method, route template, path IDs
  (non-secret UUIDs), operationId, the decision (allowed, refused and why), dispatched yes/no, upstream status (or
  `timeout` / `lost`) and duration. No bodies, cookies, tokens or response content.

### 2. `scripts/relay-operator.py session` (new subcommand)

```
relay-operator.py session --bundle DIR --bundle-sha256 HEX --image REF@sha256:… --runner-minutes N --evidence-dir DIR
```

1. **Before any authority.**
   - Validate the arguments: N at most 50; the image is a digest reference; the bundle's manifest (every file +
     sha256, the entrypoint) hashes to `--bundle-sha256`.
     <!-- codex: [P2] Identify the reviewed commit or published artifact that supplies the expected manifest and image digests, and record that association in run evidence. Matching caller-supplied hashes establishes integrity but not review provenance, which matters because prompt/attempt and permission validation remain entrusted to the bundle. -->
   - Copy the bundle to a private temp dir, so the mounted copy is the verified copy.
     <!-- codex: [P1] Hash every actual file in the private copy against the manifest after copying; hashing the manifest alone, or checking source files before copying, does not verify the mounted bytes. Require an exact file set and entrypoint, reject escaping paths and symlinks, and test source mutation during copy plus an altered file with an unchanged manifest. -->
   - Fix `cleanup_deadline = now + N + 10 min` and `runner_deadline = now + N`.
2. **Serialize.** Create the Kubernetes Lease `relay-operator-jit` in namespace `relay` (holder = run id, duration =
   until `cleanup_deadline`). If a live Lease exists, refuse. `call` takes the same Lease, for its duration.
   <!-- codex: [P1] Define atomic Lease takeover/release, reaper exclusion and stale-holder fencing: a reaper can observe no live Lease and delete memberships after a new run grants them, while an expired run's late finally can revoke or release its successor. Serialize cleanup and admission under an ownership-preserving protocol, clear orphan authority before granting anew, and test both interleavings rather than only two simultaneous creates. -->
3. **Grant, inside one `try/finally`.**
   1. Grant admin (member.js).
   2. Start the proxy container, detached and hardened: pinned IMAGE, `--read-only`, `--cap-drop ALL`,
      `--security-opt no-new-privileges`, non-root, pids and memory limits, `/tool` read-only and the socket tmpfs.
      The password and deadlines go on stdin.
   3. Wait for `READY` until `cleanup_deadline` (at most 120 s).
   4. Run the Runner container with a fixed specification: the pinned `--image`, `--network none`, `--read-only`,
      `--cap-drop ALL`, no-new-privileges, non-root, limits, the bundle copy read-only, the socket volume, the fixed
      entrypoint from the manifest, and the env `RELAY_SESSION_SOCKET` and `RUNNER_DEADLINE`. No caller-supplied
      Docker arguments. Its stdout (JSON lines: the adapter's env.save snapshots and its final report) streams
      continuously into `--evidence-dir/runner.jsonl`.
   5. At `runner_deadline`, SIGTERM the Runner (it runs `Runner.stop()` and its cleanup). Wait for it until
      `cleanup_deadline − 2 min`, then kill and remove it. The proxy keeps the session valid for that cleanup.
   6. `docker stop -t 30` the proxy, then capture its exit code and logs into `--evidence-dir/proxy.jsonl`.
      <!-- codex: [P2] Logout plus /api/auth/me can consume two 30-second upstream budgets, exceeding this stop grace even when each request meets its bound. Give logout a shared budget shorter than the grace and allocate explicit cumulative budgets for the following cleanup stages within the reserved two minutes. -->
4. **`finally`: bounded and independent stages.** Every subprocess has a timeout, SIGTERM and SIGINT become an
   exception, and `BaseException` is caught per stage.
   <!-- codex: [P1] READY timeout, SIGTERM or a Runner exception can skip step 3.6 and enter finally, which removes the proxy before its logs or exit status are captured. Start continuous proxy-log capture into pre-opened evidence files, retain a bounded final drain/inspect before removal on every path, and stop new work if evidence persistence fails. -->
   1. Force-remove both containers.
   2. Delete the subject's memberships and sessions and read back `0 memberships, 0 sessions`; rc 3 if not.
   3. Keycloak SSO logout.
   4. Release the Lease.
   - rc precedence: 3 (revoke not confirmed), then 4 (proxy logout), then 5 (Keycloak), then the Runner's or the
     deadline's failure.
   - A failed evidence capture fails the run (rc 6).

### 3. Independent reaper (IaC, Flux)

A CronJob, `relay-operator-reaper` in namespace `relay`, runs every 5 minutes. If the JIT subject has any membership or
session AND no unexpired `relay-operator-jit` Lease exists, it deletes them (the same SQL as `revoke()`) and logs one
line. This bounds a SIGKILL of the orchestrator, or the loss of the workstation, to the Lease end plus 5 minutes.
<!-- codex: [P1] A CronJob schedule is best-effort, and the reaper's DELETE can wait behind Relay's actorTenant membership/session locks, so this is not an unconditional five-minute revocation bound. Gate use on a successful deployed reaper check, qualify scheduler/API/database availability, and specify missed-run/failure alerting plus database lock/statement and Job deadlines; a rendered manifest and happy-path SQL test do not establish the claimed backstop. -->

- It uses its own minimal service account, scoped to reading that Lease plus exec on relay-postgres-0. Alternatively
  it uses a dedicated database role allowed only those two DELETEs. The cheaper one that is still narrow is chosen at
  implementation time.
  <!-- codex: [P1] Exec on relay-postgres-0 permits arbitrary commands and effectively database-superuser access, not just revocation of this subject; ordinary DELETE grants on the two tables are also not restricted to these particular statements or rows. Choose an enforced fixed-subject database operation, such as a narrowly executable function, and include its role, readback rights, credentials, Lease RBAC and necessary network-policy changes in the IaC scope. -->

### 4. Docs

`docs/runbooks/relay.md` "Operator automation identity" gains a `session` subsection:

- the purpose, the pinned scope, the allowlist (a pointer to the script);
- the learned-ID provenance rules and the deadlines;
- the reaper and the Lease, and the cleanup readback;
- the qualified rotation finding (issuance readback before removal), and that durable approvals stay owner-only;
- that the inventory reads are tenant-wide.

### Prerequisites owned by the inference session (not this change)

The proxy is necessary but not sufficient. The adapter PR must resolve, through reviewed changes and without
fabricated attestations or weakened validators:

1. **Hub source pin:** the Runner currently pins 74d1. The hub rolls to v0.14.2 first, and the driver is re-pinned
   once to its source.
2. **Renewal ownership:** the Runner never rotates. The adapter performs the two rotations through the proxy, then
   reads issuance back to a terminal state.
3. **Predecessor command receipts:** `GET /operations/:op` is actor-scoped, so the JIT identity cannot see the owner's
   old commands even as admin. Proposed contract: the operator exports, read-only from Postgres, a sanitized
   projection of the predecessor session's command operations (id, kind, state, timestamps, no prompt text) as an
   evidence file pinned by sha256. The adapter's validator accepts that file in place of the live lookup. The
   inference session decides whether this is sound.
   <!-- codex: [P1] The proposed projection omits evidence required by completed() and replacementPreflight(): operation_id, session_id, generation and native receipt status/result.stopReason or result.cancelSent. Include those sanitized fields and bind the export to the exact predecessor/report and authoritative query provenance; id/kind/state/timestamps alone would force the adapter to weaken the existing settlement checks. -->
4. **The `env.ask()` observations** (holder, workspace markers, Router trajectory, renderer cleanup) need access beyond
   Relay. A separate, later plan adds an operator "observer" with fixed read-only queries on its own socket; this
   proxy does not grow for it.
5. **The lease heartbeat:** it moves from `mount()` into the adapter, at a 20 s cadence.

## Critical files

- `scripts/relay-session-proxy.py` (new): scope constants, Unix-socket server, allowlist + schemas, provenance, deadlines, logout.
- `scripts/relay-operator.py`: `session`, the Lease in `call` and `session`, the bounded cleanup stages.
- `scripts/relay-admin-call.py`: unchanged (imported).
- `kubernetes/apps/apps/relay/operator-reaper.yaml` (new) + its kustomization entry.
- `scripts/tests/test_relay_session_proxy.py`, `scripts/tests/test_relay_operator_session.py` (new).
- `docs/runbooks/relay.md`.

## Verification

1. **Proxy unit and contract tests** (docker python:3.13), using the deployed response shapes:
   - launch and resume return top-level `id`; the launch inventory carries `session_id`, `operation_id` and
     `actor_id`; POST `/control` returns `id`, `generation`, `expires_at`; GET `/control` exposes the lease;
     permission rows carry `id`; the credential inventory is `{bindings, credentials, consumers}` with `binding_id`;
   - each allowed route passes and every near-miss is refused before upstream;
   - provenance: an old launch replay, cross-session substitution, an unlearned ID, the predecessor used in a
     mutation, `acknowledgeUnknownToolEffects: true`, a second rotate per binding, reused or duplicate keys, framing
     conflicts;
   - bounds: a slow upstream, an oversize body, the timeline cursor;
   - deadlines: cleanup-only after `runner_deadline`, nothing after `cleanup_deadline`, logout on SIGTERM.
2. **Orchestrator fault tests** (fakes for docker, kubectl and the proxy):
   - a grant transport failure after commit, a READY timeout, Runner and proxy crashes, SIGINT and SIGTERM, the
     deadline during work, an overlapping invocation (the Lease is refused), each cleanup stage failing;
   - assert that both containers are accounted for, the stages run independently, evidence is preserved, nothing is
     replayed, and the rc precedence holds.
3. **Reaper test:** render the CronJob and run its script against a disposable Postgres (docker). It deletes only with
   no live Lease and leaves everything when one exists.
4. **Live read-only smoke** (operator, before any run): `session` with a trivial pinned Runner bundle.
   - It GETs `/api/agent-profiles` and `/api/router-credentials` (200).
   - Locally refused: `/api/auth/me`, a POST with an unlearned sid, a mutation on the predecessor.
   - It confirms that a TCP connect from the Runner fails (`--network none`).
   - Then: readback `0 memberships, 0 sessions`, the Keycloak logout, the Lease released. A second run while the
     first holds the Lease is refused.
5. **The first real run** reads back after cleanup: both rotated credentials stay ready or installed with the
   membership gone, and the reaper finds nothing to do.
6. **Codex implementation review** before the PR; then the normal reviewbot gate.

<!-- codex: round-2: [P1] Deferring the adapter exercise to its prerequisite PR and omitting a disposable Relay stack are reasonable; source inspection supports the unchanged login-binding logic. However, step 5 must explicitly gate real execution on the reviewed adapter/observer and deployed-version pins, require integration evidence for five launches including two resumes, permission decisions and deadline cleanup, and read credentials after removal through an independent read-only identity: the reaper only removes login authority and cannot verify those behaviors. -->
<!-- opus-pushback: The actual-adapter run cannot be part of this change because the adapter does not exist yet (it is the inference session's prerequisite PR); it becomes verification step 5's first real run, gated on that PR. A disposable Relay server stack to prove post-removal reconciliation and lease-session binding is out of proportion: the lease binding is enforced in control.ts by actor.userId+sessionHash, which the deployed code shows, and post-removal reconciliation is checked by step 5's live readback with the reaper as the backstop. -->

<!-- codex-review-status: complete -->
