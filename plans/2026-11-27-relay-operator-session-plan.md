# Bounded Relay session for automated phase A (relay-operator `session`)

## Codex Review

- The narrow proxy, single login, retained owner approvals and cleanup readback are sound foundations. The deployed server confirms that control leases bind to both user ID and login session hash.
- The current Runner cannot complete its stopped-run preflight through this plan: historical IDs are excluded, and command receipts are scoped to the requesting user even for admins.
- Ordinary credential rotation introduces no durable approver-membership dependency, but issuance rechecks live admin authority after recording intent. A recorded operation or HTTP 202 does not establish successful issuance.
- The security boundary needs stricter request schemas, trusted artifact/configuration pins and constrained Docker arguments. Sharing the proxy's network namespace does not restrict Runner egress to localhost.
- Verification must cover the actual Runner and deployed response shapes, authority removal, lost responses, concurrent invocations and interrupted cleanup. The adapter's source-version and evidence-collection dependencies remain unresolved.

## Context

On 2026-11-27 the owner directed: "Automate the implementation, you should not require me to manually execute relay
tasks. Help the worker agent to test and validate the work by himself." Phase A of the dev-worker-1 Pi canary
(docs/runbooks/inference-canary-v0113/OWNER-PHASE-A.md) has so far run as a browser flow in the owner's own Relay
session. The inference session now drives it non-interactively, through a reviewed adapter around the exported Runner
(owner-phase-a.js). Its access requirement (bus 2276) is precise:

- Relay control leases bind `actor.userId` AND `actor.sessionHash`. The existing `scripts/relay-operator.py call`
  logs in and out per call, so separate calls cannot hold a lease. The run needs ONE authenticated session for its
  whole duration, with a hard deadline and receipt-preserving cleanup.
- It needs admin for two things only: `POST /api/router-credentials/:id/rotate` (and its reviewed final revoke), and
  `GET /api/agent-sessions/:id/native-recovery` + `POST /api/agent-sessions/:id/resume`. Everything else is operator.
- Explicitly NOT wanted: an unrestricted method/path proxy; new profile, grant or delegation approvals; copied owner
  cookies; a membership bypass.

The JIT identity (`relay-operator@ailab.invalid`, Keycloak realm `relay`, password only in OpenBao
`af/relay/operator-oidc`) has no Relay membership at rest. Its rule "never approve grants, creation policies or
delegations" exists because Relay re-checks the APPROVER's current admin membership on every use of those. **Checked in
the deployed source (v0.14.1, 5f90d03a):** router-credential rotate and revoke record the actor only in
`router_credential_operations.actor_id`, an audit journal. `remoteRevoke` passes it to the Router bridge, and the code
says that journal "is completed even after that human logs out". `isAdmin(…approved_by)` is checked only for worker
grants, creation policies, environment pools and agent templates. Launch profiles keep their owner `approved_by`. So a
rotation by the JIT identity does not break once its membership is removed. This plan adds no approval capability.
<!-- codex: [P1] Qualify this conclusion: service.ts rotate() calls issue(), which re-enters actorTenant(actor, "admin") after insertIssuance(), while subsequent ordinary reconciliation and remoteRevoke() do not recheck the original approver. Require issuance/acknowledgement readback or an explicit unresolved outcome before removing authority; issue() catches failures, so HTTP 202 alone is insufficient. -->
<!-- codex: [P2] The exhaustive "only" claim omits current-admin checks in workers/delegation/service.ts, workers/management/authority.ts and workers/repositories/service.ts, which use issued_by/issuedBy. Keep the conclusion specific to ordinary credential rotation and retain the runbook's broader prohibition on durable approvals. -->

## Approach

### 1. `scripts/relay-session-proxy.py` (new; stdlib only; runs in the pinned python image)

- **Login once.** Reuse `relay-admin-call.py`'s `Client` / `login` / `logout`: import it from the same read-only
  `/tool` mount. The flow is Keycloak form, every redirect checked, `/api/auth/me` must say admin and
  `relay-operator (automation)`. The `relay_session` cookie lives only in that process's memory and never crosses
  the proxy.
- **Listen on 127.0.0.1:8765 only,** inside its own container network namespace. The Runner container joins it with
  `--network container:<proxy>`. Nothing listens on the LAN or on the host's loopback.
  <!-- codex: [P1] This prevents publishing the listener but does not enforce localhost-only Runner egress: the shared namespace also has the proxy's outbound connectivity to Relay and Keycloak. Specify whether localhost use is a trusted-adapter constraint or an enforced network restriction, and test the stated boundary. -->
- **Allowlist, method + path template.** Everything else gets 403 before any upstream call.
  - Path IDs are either FIXED (from a run config) or LEARNED (this run's own responses). Fixed: the two bindings
    `a0f57684-747f-4c3e-8d90-e684b7262590` (Codex) and `d2871eec-8f5a-4afe-a917-014f0ae8e14d` (Qwen); the
    dev-worker-1 revision-1 profile IDs.
  - Learning:
    - launch IDs, from the `id` of POST `/api/agent-launches` and POST `/resume` responses;
      <!-- codex: [P2] A lost POST response can leave a durable launch whose ID is never learned. Define read-only reconciliation or an explicit unresolved handoff using the original operation ID, without resending the launch or automatically acquiring control. -->
    - session IDs, only from `session_id` of rows in GET `/api/agent-launches` whose `id` is a learned launch;
      <!-- codex: [P1] Runner.replacementPreflight() first reads historical session 45724087-335a-4c55-ad0f-cb278afe608a and its control state, then historical permission 70fb1b8e-f7cc-4cc3-b98f-66f83e46d1ab; neither can be learned from this run's launches. Add narrowly pinned read-only predecessor access or a reviewed adapter alternative, keeping those IDs ineligible for mutations. -->
    - lease IDs, from POST `/control` responses for a learned session;
    - permission IDs, from GET `/api/agent-sessions/:sid/permissions` (learned sid);
    - operation IDs, from this run's own POST bodies (`operationId`);
      <!-- codex: [P1] Even allowlisting predecessor operation IDs will not make the unchanged Runner work: control.ts GET /operations/:operation filters by actor_id = actor.userId and returns {command:null} for the owner's old commands under the JIT identity. Resolve this through a reviewed evidence/adapter contract before implementation; admin role does not bypass that filter. -->
    - credential IDs, from GET `/api/router-credentials` rows of the two fixed bindings.
    <!-- codex: [P1] Store parent relationships and provenance, not independent UUID sets: bind launches to operation/profile/revision/actor, leases and permissions to session/generation, and credentials to binding. Learn only from validated successful responses attributable to this run, rejecting old idempotent launch replays and mismatched rows; operation-ID uniqueness within one run does not establish fresh server intent. -->
  - The allowed routes are exactly bus 2276's list. Operator:
    - GET `/api/router-credentials`, `/api/router-credentials/keys/:cred`, `/api/agent-profiles`, `/api/agent-launches`;
    - POST `/api/agent-launches` (body `profileId` ∈ fixed profiles, `revision` == 1);
    - GET `/api/agent-sessions/:sid`, `/router`, `/communication`, `/control`, `/settings`, `/operations/:op`, `/permissions`;
    - POST `/control`, DELETE `/control/:lease`, POST `/commands`;
    - GET `/api/agent-permissions/:pid`, POST `/api/agent-permissions/:pid/decision`;
    - GET `/api/conversations/sessions/:sid/timeline` (`limit` ≤ 200, only `after` / `limit` / `promptsAfter`).

    Admin:
    - POST `/api/router-credentials/:binding/rotate` and `/revoke` (fixed bindings);
    - GET `/api/agent-sessions/:sid/native-recovery`, POST `/resume` (learned sid).
    <!-- codex: [P1] Binding revocation disables all generations, including credentials used outside this invocation; it is distinct from revoking the temporary identity. Preserve the reviewed no-further-use and settled-consumer checks, and specify the outcome when only one binding's revocation succeeds. -->
- **Request hygiene.**
  - JSON bodies only, at most 64 KiB, and each must parse.
    <!-- codex: [P1] Valid JSON plus a route match permits more authority than this workflow needs: arbitrary profile-root cwd, additional command kinds/settings, unrestricted permission options, and resume's acknowledgeUnknownToolEffects:true. Define strict per-route fields and bounded selections, with the reviewed Runner explicitly responsible for prompt/attempt budgets and full permission-input validation rather than duplicating its entire workflow in the proxy. -->
  - Write bodies must carry a fresh `operationId` UUID, never reused within the run; this does not apply to `/control` or DELETE.
  - The proxy sets `Origin`; it drops the client's `Cookie`, `Authorization` and `Origin`.
    <!-- codex: [P1] Construct upstream URLs and headers from trusted values rather than forwarding all remaining client headers, especially Host and HTTP framing headers. Reject ambiguous request targets, duplicate query/body keys and conflicting Content-Length/Transfer-Encoding, and permit queries only on explicitly specified routes. -->
  - Responses pass through with `Set-Cookie` stripped. Each upstream call is bounded at 30 s.
    <!-- codex: [P2] These inventories are tenant-wide, and rotate/revoke also return the full credential inventory; path-ID restrictions therefore do not confine reads to the two bindings and this run. Either document that broader disclosure or filter responses while preserving detection of the server's inventory limits, so filtering cannot turn truncated data into apparently complete evidence. -->
    <!-- codex: [P1] Client.request() uses a socket timeout and unbounded r.read(), which do not provide a total wall-clock or response-size bound. Add bounded reads and deadline enforcement, and ensure slow requests cannot starve the Runner's 20-second renewal schedule for leases capped at 60 seconds; concurrent handling must reserve operation IDs atomically. -->
- **Hard deadline** (`--deadline-s`, at most 3600). After it every request is refused, the proxy logs out and exits.
  SIGTERM and SIGINT also log out first. Exit status: 0 if the logout was confirmed (`/api/auth/me` 401 afterwards),
  4 otherwise.
  <!-- codex: [P1] Define one monotonic deadline starting before grant, covering startup and in-flight requests, rather than resetting it when the proxy becomes ready. A proxy timer cannot revoke the standing membership after orchestrator SIGKILL or host loss; specify independent revocation enforcement or explicitly qualify this guarantee and document recovery. -->
- **Receipt log** to stderr, one JSON line per request: time, method, route template, learned or fixed, status,
  duration. No bodies, cookies, tokens or response content.

### 2. `scripts/relay-operator.py session` (new subcommand)

```
relay-operator.py session --config RUN.json --deadline-s N -- <runner docker run args…>
```

<!-- codex: [P1] Arbitrary Docker arguments can defeat the stated isolation through mounts, privileged mode, shared PID namespaces, entrypoint overrides or injected runtime options. Prefer a fixed launch specification with narrowly validated inputs, and apply equivalent hardening to the proxy container that holds the admin cookie. -->

- `RUN.json`: fixed binding IDs, fixed profile IDs + revision, origin. It is validated against UUIDs and an
  allowlisted origin `https://relay.chifor.me`.
  <!-- codex: [P1] UUID syntax does not enforce the fixed canary scope: pin the two binding IDs and profile IDs 1d9a7d6e-4b26-49fd-9053-7b0a22e1baae and f25c187b-1e50-4c7c-860e-52947bfdf39e at revision 1, including their expected relationships. Validate configuration and artifact pins before granting admin, and distinguish reviewed scope from caller-selectable parameters. -->
- Order, all inside one `try/finally`:
  <!-- codex: [P1] Serialize call/session invocations for this shared subject across supported execution hosts before granting authority. cleanup() deletes every session and membership for the identity, so one overlapping invocation can invalidate another's lease or leave misleading cleanup readbacks. -->
  1. read the creds from OpenBao;
  2. `grant` admin;
  3. start the proxy container, detached, pinned IMAGE, `/tool` read-only, with the password and RUN.json on stdin;
  4. wait for its `READY` line (login done, admin confirmed), at most 120 s;
  5. run the Runner container `--network container:<proxy>` with `--rm --read-only --cap-drop ALL`, a non-root
     user and the reviewed adapter directory mounted read-only. The adapter must match the sha256 given with
     `--runner-sha256`. It reaches `http://127.0.0.1:8765` only;
     <!-- codex: [P1] Define the reviewed artifact and trusted source of its expected digest: hashing one adapter file does not cover imported owner-phase-a.js, other executable dependencies or a mutable Runner image. Verify the complete executed bundle and image digest, including the entrypoint, and prevent changes between verification and execution. -->
  6. at the Runner's exit or the deadline, `docker stop` the proxy (SIGTERM → logout).
     <!-- codex: [P1] Stop the Runner early enough to call Runner.stop(), settle its cleanup and export evidence while the original login and lease remain usable, then stop/remove the tracked Runner container as well as the proxy. Logging out or deleting authentication sessions does not close agent sessions or prove already accepted inference stopped, and a hard cutoff otherwise prevents cancel/Close requests. -->
     <!-- codex: [P2] Specify a bounded Docker stop grace period and capture the proxy's actual exit status before removal; the default grace can expire while logout and its readback are still waiting on HTTP. Ensure Python receives signals directly and that logout failures cannot skip the independent membership cleanup. -->
- `finally` (unchanged semantics from `call`): `cleanup()` deletes the memberships and sessions of the subject (read
  back `0 memberships, 0 sessions`, rc 3 if not), ends the Keycloak SSO sessions, and `docker rm -f` the proxy. The
  proxy's own logout result is folded into the rc (4).
  <!-- codex: [P1] The existing helpers use subprocess.run() without timeouts, cleanup() catches only SystemExit, and ordinary SIGTERM does not automatically run Python finally blocks. Add bounded, independently attempted cleanup stages and explicit signal handling, retaining rc 3 precedence and nonzero Runner/deadline/Keycloak failures rather than reporting success solely because proxy logout succeeded. -->
- Output: the proxy receipt log, plus the Runner's own report (it writes its sanitized report to stdout). Both are
  saved under the operator's scratchpad by the caller; the command itself writes no file.
  <!-- codex: [P1] A final-only stdout report can disappear with the Runner, while route-template logs omit the IDs needed to reconcile uncertain mutations. Stream and flush sanitized env.save snapshots before dispatch, plus run/resource/operation identifiers and dispatch-versus-response status, with the caller capturing them continuously and a defined failure path when evidence capture fails. -->

### 3. Docs

`docs/runbooks/relay.md` "Operator automation identity" gains a `session` subsection: the purpose, the allowlist
(pointer to the script's table), the learned-ID rule, the deadline, the cleanup readback, why the router-credential
rotation does not hit the approver recheck (the source pointers above), and that profile, grant, policy and
delegation approvals stay owner-only.

### Out of scope

- The Runner adapter itself: the inference session's reviewed PR, pinned by hash.
  <!-- codex: [P1] Make that PR and its compatibility evidence explicit prerequisites: the checked-in Runner requires deployment hubSource 74d1d9f4ab2250fe6a6abe5a2e0bebce2ccc4613, whereas this review targets deployed 5f90d03a, and the Runner itself never rotates credentials. The adapter must resolve the source pin, renewal ownership and historical-receipt access through reviewed changes without fabricating attestations or silently weakening validators. -->
  <!-- codex: [P1] Specify how the adapter supplies fresh holder, marker, Router-trajectory and renderer-cleanup observations to env.ask(), preserves the stopped report, and schedules renew(); the 20-second heartbeat currently lives in mount(), outside Runner. These observations require access beyond this Relay route allowlist, so identify their producer and bounded delivery interface without handing the Runner general operator credentials or returning unconditional confirmations. -->
- Any Relay server change.
- Starting or resuming the dev-worker-1 holder: `scratchpad/handoff/resume.sh`, run by the operator at the automation
  window signal.

## Critical files

- `scripts/relay-session-proxy.py` (new): login once, allowlist + learned IDs, deadline, logout.
- `scripts/relay-operator.py`: the `session` subcommand (grant → proxy → runner → stop → cleanup).
- `scripts/relay-admin-call.py`: unchanged; imported for `Client` / `login` / `logout`.
- `scripts/tests/test_relay_session_proxy.py` (new): unit and fake-upstream tests.
- `docs/runbooks/relay.md`: the `session` subsection.

## Verification

1. **Unit** (`scripts/tests/test_relay_session_proxy.py`, in docker python:3.13): each allowed route passes and
   near-misses are refused. Near-misses: an unknown or unlearned session/permission/lease ID; a fixed binding swapped
   for a random UUID; an extra query key; `limit=201`; a wrong method; a path with `..`, `//` or encoded slashes. Also:
   - learning only from this run's launch rows;
   - `operationId` reuse refused;
   - body size and JSON checks;
   - `Cookie`, `Authorization` and `Origin` from the client dropped, `Set-Cookie` stripped;
   - the deadline refuses and triggers logout.
   <!-- codex: [P1] Add contract cases using the deployed shapes: launch/resume return top-level id, launch inventory carries session_id, POST control returns id/generation/expires_at, GET control returns lease with owned/expiresAt and id only when owned, and permission lists return rows with id rather than commandId. Credential inventory is {bindings,credentials,consumers} with binding_id, while inspection uses camelCase; test cross-session substitution, old launch replay, inventory saturation and timeline cursor validation as well. -->
2. **Fake upstream:** a local HTTP server stands in for Relay (login endpoints faked). It asserts the proxy sends the
   cookie and Origin and that nothing from a refused request reaches it.
   <!-- codex: [P1] Exercise the actual pinned adapter and exported Runner through the proxy across stopped-run replacement, renewal, five launches including two resumes, permission decisions and cleanup, preserving deployed field names and actor-scoped operation lookup. Add an isolated server-backed check that ordinary issued credentials and queued revocations reconcile after JIT membership removal, and that a different login for the same user cannot renew, command through or release the original lease. -->
   <!-- codex: [P1] Add orchestrator fault tests for grant transport failure after commit, READY timeout, slow upstream/body reads, lost mutation responses, proxy/Runner crashes, SIGINT/SIGTERM, deadline during active work, overlapping invocations and each cleanup-stage failure. Assert preserved operation evidence, no automatic mutation replay, both containers accounted for, independent revoke/SSO attempts and correct exit-code precedence. -->
3. **Live read-only smoke** (operator, before any run): `session` with a trivial Runner (curl image) that GETs
   `/api/auth/me` (refused: not on the allowlist), then `/api/agent-profiles` and `/api/router-credentials` (200), and
   one POST with an unlearned session ID (403 locally). Then the readback `0 memberships, 0 sessions` and the
   Keycloak logout.
4. **Codex implementation review** before the PR; the PR goes through the normal reviewbot gate.

<!-- codex-review-status: complete -->
