# Implementation review — agentforge-v2 — round 1

<!-- codex-impl-review-status: complete -->

Round-1 codex review of the P1 tenant-zero vertical slice found 4 blockers + 6
important findings (no invented nits). All 10 were accepted and fixed with tests;
each resolution is a focused commit in `agentforge-platform` (CP) or `agentforge`
(worker). Round 2 re-reviews these fixes.

## Findings & resolutions

### 1. Public default permitted forged authenticated sessions — blocker → FIXED
The session cookie signs the principal + memberships and a hardcoded/optional
signing key made owner-session forgery trivial. `build_app()` now refuses a
weak/known/empty `AFP_SESSION_SECRET` (dev_mode opts out), the cookie defaults to
`https_only=True`, and the deploy Secret ref is non-optional.
CP `90d6c3d` — `tests/unit/test_session_hardening.py`.

### 2. Any operator could provision an arbitrary image on the hub — blocker → FIXED
Split the RLS-scoped workspace RECORD (any org) from COMPUTE provisioning
(tenant-zero only in P1). The caller-supplied `image` is gone (server picks the
digest-pinned `worker_image`), replicas are clamped, and no manifests are ever
committed for a non-tenant-zero org.
CP `743d0c9` — `test_api_workspaces.py`.

### 3. Rendered workers could not start or fetch config — blocker → FIXED
The ConfigMap shipped only unused `AF_WORKER_POOL/AF_ROLES`. It now emits the real
`AF_CONFIG_SOURCE=control_plane` + `AF_GITEA_URL` + the `/api/v1/.../config`
endpoint, a downward-API `AF_WORKER_NAME`, and the previously-dead
`config_bearer_secret` wired as an `envFrom` secretRef. The P1 execution image is
the new combined `p1-worker` (orchestrator + agent CLIs) since LocalExecutor runs
the agent in-process.
CP `743d0c9`, `1e56c7c` · worker `9bedabe` — `test_gitops_renderer.py`, release.yml.

### 4. Workspace policy accepted as caller-supplied config — blocker → FIXED
`PUT /config` did schema validation only. `bind_workspace_policy()` now rejects
(422) any config that targets a foreign repo, enables `auto_merge` on the
merge-disabled P1 shadow, or uses an engine outside the org allowlist.
CP `fafa6ba` — `test_api_workspaces.py`.

### 5. Degraded config source did not stop new claims — important → FIXED
`degraded` previously affected only `/readyz`. An admission predicate is now
checked immediately before `ClaimService.acquire` on both the reconcile and
webhook paths; in-flight work drains, no new work is admitted.
worker `741be7c` — `test_orchestrator_degraded.py`.

### 6. Ingest hardening process-local + lost retryable events — important → FIXED
The idempotency key was recorded at check-time, before rate-limit/apply, so a
rejected event's retry was swallowed. Split into `already_applied` (peek) +
`mark_applied` (post-apply commit); pinned the CP to `replicas: 1` for P1 (the
read model/idempotency/rate state are in-memory; shared store + HA is P2).
CP `edc5420` — `test_readmodel.py`, `test_api_ingest.py`.

### 7. Repository bootstrap was a success-reporting stub — important → FIXED
Added a `Bootstrapper` port + `GiteaLabelBootstrapper` (idempotent lifecycle-label
creation); the endpoint delegates and flips `bootstrapped` only on success, and
returns 501 when no adapter is wired instead of lying.
CP `d33977b` — `test_api_workspaces.py`.

### 8. Vue app absent from the deployed control plane — important → FIXED
Added a node build stage to the image and SPA serving from FastAPI at `/` with
client-side-routing fallback (deep links load index.html), gated on
`Settings.webapp_dist`; `/api` + health routes keep their behavior.
CP `20fbe8d` — `test_spa_serving.py`.

### 9. Required GitOps boundary negatives not exercised — important → FIXED
`safe_content_path()` fails closed on traversal/repo-root escape; tests prove
every committer request targets only `cchifor/agentforge-tenants` under
`tenants/` and that an escaping `path_prefix` raises before any HTTP call. The
deployed-bot + admission integration negatives remain P2.
CP `0daebdf` — `test_gitops_committer.py`.

### 10. Codex executor assumed a shared orchestrator /tmp — important → FIXED
Codex's output-last-message file now lives under the shared job checkout (unique
per run, traversal-checked, cleaned up) instead of an orchestrator-local temp
dir, so a future SandboxExecutor in a separate pod can produce it.
worker `1aa85ec` — `test_runners_codex.py`.

## Gate status after fixes
- agentforge-platform: 106 passed, ruff + mypy clean, webapp builds.
- agentforge: 354 passed / 20 skipped, ruff + mypy clean. (One rare order-dependent
  flake under pytest-randomly, passes deterministically; pre-existing, tracked.)

## Diff stat
Round-1 fixes span CP commits `90d6c3d..1e56c7c` and worker commits
`741be7c`, `1aa85ec`, `9bedabe`.
