# Implementation review — agentforge-v2 — round 3 (finalized)

<!-- codex-impl-review-status: finalized -->

Codex Phase B ran the full 3-round cross-review (the user's cap). Round 3
confirmed R2-1/2/4/5/6/7 resolved with no new blocker, and raised 2 `important`
residuals hardening the single-identity invariant — both fixed and self-verified
with tests. **The P1 slice is aligned.**

## Round-3 findings & resolutions

### R3-1. Single-replica invariant was only a default — important → FIXED
`max_worker_replicas` is now `le=1` (an env override >1 fails at Settings
construction) and `assert_allowlisted` fails closed on any Deployment with
`replicas > 1`, so multiple pods can never share the one stable
`AF_WORKER_NAME`/claim identity.
CP `c7e4c66` — `test_gitops_renderer.py` (`test_multi_replica_deployment_rejected`,
`test_max_worker_replicas_env_override_above_one_rejected`).

### R3-2. Empty pool bypassed the required-worker policy — important → FIXED
`PUT /config` now requires a non-empty, existing pool and always binds that pool's
worker identity, so a config can't be accepted under an empty pool while the
deployed worker fetches its named pool and gets no roles.
CP `c7e4c66` — `test_api_workspaces.py`
(`test_put_config_requires_a_pool`, `test_put_config_rejects_unknown_pool`).

## Full cross-review trail
- Round 1: 4 blockers + 6 important → all fixed (session forgery, arbitrary hub
  image, non-runnable worker env, caller-supplied policy, degraded-claims,
  ingest atomicity, bootstrap stub, SPA-in-image, committer scoping, codex /tmp).
- Round 2: 3 blockers + 4 important residuals → all fixed (session floor 32 +
  dev-mode empty ban, NetworkPolicy DNS/CP egress, stable worker identity + config
  binding, ingest race + best-effort audit, workspace-scoped bootstrap token,
  committer encoded-traversal + batch preflight, codex gate cwd).
- Round 3: 0 blockers + 2 important residuals → all fixed (non-configurable
  single-replica, required existing pool on config write).

## Final gate status
- agentforge-platform: 119 passed, ruff + mypy clean, webapp builds.
- agentforge: 356 passed / 20 skipped, ruff + mypy clean.

## Verdict
ALIGNED — the P1 tenant-zero slice is sound to proceed (push + PR). P2 (OpenBao/
ESO/KEDA/kro/Kata SandboxExecutor/Cilium egress/k8s-native runners/HA ingest) and
P3 (external clusters/multi-user/dogfood) remain the phased follow-ons.
