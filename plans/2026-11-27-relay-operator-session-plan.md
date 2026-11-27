# Bounded Relay session for automated phase A (relay-operator `session`)

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

## Approach

### 1. `scripts/relay-session-proxy.py` (new; stdlib only; runs in the pinned python image)

- **Login once.** Reuse `relay-admin-call.py`'s `Client` / `login` / `logout`: import it from the same read-only
  `/tool` mount. The flow is Keycloak form, every redirect checked, `/api/auth/me` must say admin and
  `relay-operator (automation)`. The `relay_session` cookie lives only in that process's memory and never crosses
  the proxy.
- **Listen on 127.0.0.1:8765 only,** inside its own container network namespace. The Runner container joins it with
  `--network container:<proxy>`. Nothing listens on the LAN or on the host's loopback.
- **Allowlist, method + path template.** Everything else gets 403 before any upstream call.
  - Path IDs are either FIXED (from a run config) or LEARNED (this run's own responses). Fixed: the two bindings
    `a0f57684-747f-4c3e-8d90-e684b7262590` (Codex) and `d2871eec-8f5a-4afe-a917-014f0ae8e14d` (Qwen); the
    dev-worker-1 revision-1 profile IDs.
  - Learning:
    - launch IDs, from the `id` of POST `/api/agent-launches` and POST `/resume` responses;
    - session IDs, only from `session_id` of rows in GET `/api/agent-launches` whose `id` is a learned launch;
    - lease IDs, from POST `/control` responses for a learned session;
    - permission IDs, from GET `/api/agent-sessions/:sid/permissions` (learned sid);
    - operation IDs, from this run's own POST bodies (`operationId`);
    - credential IDs, from GET `/api/router-credentials` rows of the two fixed bindings.
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
- **Request hygiene.**
  - JSON bodies only, at most 64 KiB, and each must parse.
  - Write bodies must carry a fresh `operationId` UUID, never reused within the run; this does not apply to `/control` or DELETE.
  - The proxy sets `Origin`; it drops the client's `Cookie`, `Authorization` and `Origin`.
  - Responses pass through with `Set-Cookie` stripped. Each upstream call is bounded at 30 s.
- **Hard deadline** (`--deadline-s`, at most 3600). After it every request is refused, the proxy logs out and exits.
  SIGTERM and SIGINT also log out first. Exit status: 0 if the logout was confirmed (`/api/auth/me` 401 afterwards),
  4 otherwise.
- **Receipt log** to stderr, one JSON line per request: time, method, route template, learned or fixed, status,
  duration. No bodies, cookies, tokens or response content.

### 2. `scripts/relay-operator.py session` (new subcommand)

```
relay-operator.py session --config RUN.json --deadline-s N -- <runner docker run args…>
```

- `RUN.json`: fixed binding IDs, fixed profile IDs + revision, origin. It is validated against UUIDs and an
  allowlisted origin `https://relay.chifor.me`.
- Order, all inside one `try/finally`:
  1. read the creds from OpenBao;
  2. `grant` admin;
  3. start the proxy container, detached, pinned IMAGE, `/tool` read-only, with the password and RUN.json on stdin;
  4. wait for its `READY` line (login done, admin confirmed), at most 120 s;
  5. run the Runner container `--network container:<proxy>` with `--rm --read-only --cap-drop ALL`, a non-root
     user and the reviewed adapter directory mounted read-only. The adapter must match the sha256 given with
     `--runner-sha256`. It reaches `http://127.0.0.1:8765` only;
  6. at the Runner's exit or the deadline, `docker stop` the proxy (SIGTERM → logout).
- `finally` (unchanged semantics from `call`): `cleanup()` deletes the memberships and sessions of the subject (read
  back `0 memberships, 0 sessions`, rc 3 if not), ends the Keycloak SSO sessions, and `docker rm -f` the proxy. The
  proxy's own logout result is folded into the rc (4).
- Output: the proxy receipt log, plus the Runner's own report (it writes its sanitized report to stdout). Both are
  saved under the operator's scratchpad by the caller; the command itself writes no file.

### 3. Docs

`docs/runbooks/relay.md` "Operator automation identity" gains a `session` subsection: the purpose, the allowlist
(pointer to the script's table), the learned-ID rule, the deadline, the cleanup readback, why the router-credential
rotation does not hit the approver recheck (the source pointers above), and that profile, grant, policy and
delegation approvals stay owner-only.

### Out of scope

- The Runner adapter itself: the inference session's reviewed PR, pinned by hash.
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
2. **Fake upstream:** a local HTTP server stands in for Relay (login endpoints faked). It asserts the proxy sends the
   cookie and Origin and that nothing from a refused request reaches it.
3. **Live read-only smoke** (operator, before any run): `session` with a trivial Runner (curl image) that GETs
   `/api/auth/me` (refused: not on the allowlist), then `/api/agent-profiles` and `/api/router-credentials` (200), and
   one POST with an unlearned session ID (403 locally). Then the readback `0 memberships, 0 sessions` and the
   Keycloak logout.
4. **Codex implementation review** before the PR; the PR goes through the normal reviewbot gate.

<!-- codex-review-status: pending -->
