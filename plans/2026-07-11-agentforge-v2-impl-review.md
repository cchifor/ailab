# Implementation review — agentforge-v2 — round 2

<!-- codex-impl-review-status: complete -->

Round 2 re-reviewed the round-1 fixes and found 3 blockers + 4 important residuals
(the round-1 fixes were correct but incomplete). All 7 accepted and fixed with
tests. Round 3 verifies.

## Findings & resolutions

### R2-1. Weak/dev-mode session secrets still servable — blocker → FIXED
Raised the production floor to 32 chars and made an empty/placeholder key refused
even in `dev_mode` (no key = no signing; dev_mode only relaxes the length floor).
CP `630663f` — `test_session_hardening.py`.

### R2-2. NetworkPolicy blocked the control-plane config endpoint — blocker → FIXED
Egress allowed only TCP 443, so a fresh worker could not DNS-resolve or reach the
in-cluster CP config API on 8080 (and had no last-good yet). Egress now allows DNS
(53 UDP+TCP) + CP (8080) + forge/model (443).
CP `924e17c` — `test_gitops_renderer.py::test_networkpolicy_allows_dns_and_control_plane`.

### R2-3. Pod identity could not match fetched config — blocker → FIXED
`AF_WORKER_NAME` was the random pod name, but the worker resolves roles via
`cfg.roles_for(AF_WORKER_NAME)` — pods started role-less. Now a stable
`{slug}-{pool}` identity (single-replica P1; used for role lookup AND the claim
owner), `bind_workspace_policy` requires the config to declare that worker, and
`max_worker_replicas=1` (P2 adds a per-pod claim id + KEDA).
CP `924e17c` — `test_api_workspaces.py::test_put_config_requires_declared_worker`.

### R2-4. Ingest idempotency racy + partially applied — important → FIXED
The mark happened after an awaited audit, opening a race and letting an audit
failure re-emit the event. Commit the key immediately after apply (steps 4-7 are
synchronous — no await in the window), and make the audit best-effort.
CP `97adeac` — `test_api_ingest.py`, `test_readmodel.py`.

### R2-5. Bootstrap used the tenant-repo commit token — important → FIXED
`GiteaLabelBootstrapper` was built with `tenants_bot_token` (scoped to the tenants
repo, must not write workspace repos). Now a distinct `bootstrap_token`; empty =>
501, keeping the GitOps boundary intact.
CP `272dab5`.

### R2-6. Committer validation not normalized/preflighted — important → FIXED
`safe_content_path` now also rejects backslash + percent-encoded traversal +
single-dot segments, and `commit()` validates the whole file batch before any HTTP
so one unsafe name can't land after earlier-safe writes.
CP `272dab5` — `test_gitops_committer.py`.

### R2-7. Codex gate output missed the shared job root — important → FIXED
The alignment gate built codex jobs with `cwd=Path(".")`, so the round-1 scratch
dir landed under the process CWD, not `AF_JOBS_ROOT`. The gate now allocates a
validated per-issue dir under the shared jobs root and passes it to every codex
job (both `run` and `critique_once`).
worker `ca8320f` — `test_gates.py::TestCritiqueWorkspace`.

## Gate status after fixes
- agentforge-platform: 115 passed, ruff + mypy clean.
- agentforge: 356 passed / 20 skipped, ruff + mypy clean.

## Diff stat
Round-2 fixes: CP `1e56c7c..272dab5` (924e17c, 630663f, 97adeac, 272dab5) and
worker `ca8320f`.
