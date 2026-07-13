# Implementation review — iac-activation

<!-- codex-impl-review-status: pending -->

## Scope
Cross-repo IaC-activation build for AgentForge v2 (init a LIVE OpenBao vault, no manual ops):
- agentforge `feat/openbao-activation` — `provisioner/bootstrap.py` (init/unseal/provision seams + fail-closed
  state machine), main.py subcommands, `.gitea/workflows/images.yml`.
- ailab `feat/p2-unlock` — `security/openbao/**` (TLS/RBAC/Jobs/unsealer/provisioner/HelmRelease) +
  `security/openbao-canary/**` + scripts + runbook.
- agentforge-platform `feat/image-ci` — image CI + pin path.

## Codex Phase B — round 1 (3 blockers, 3 important) → ALL fixed
- **BLOCKER hvac missing from image** → orchestrator.Dockerfile runtime now `uv sync … --extra provisioner`
  (hvac==2.4.0 in the layer). [agentforge 4460a9c]
- **BLOCKER canary CRDs deadlock Flux** (openbao applied ESO CRs before external-secrets installs the CRDs;
  external-secrets dependsOn openbao) → moved the canary to a SEPARATE `openbao-canary` Kustomization
  dependsOn external-secrets. [ailab 7159de0]
- **BLOCKER run_init could strand root/unseal material** (persisted Secrets after unseal) → now persists the
  bootstrap + keys Secrets (409-tolerant, retried) IMMEDIATELY after `initialize()`, THEN unseals; a new test
  asserts persist precedes unseal via a shared event log. [agentforge 4460a9c]
- **important reachability retry** → new `wait_reachable` (bounded ~5min exp backoff, redacted, fail-closed)
  wired into all 3 entrypoints; provision no-op path stays OpenBao-free. [agentforge 4460a9c]
- **important canary seed/property mismatch** → seed key is now `canary` (matches the ExternalSecret
  remoteRef property). [agentforge 4460a9c]
- **important provisioner token policy too broad (`af/*`)** → narrowed to the exact paths the controller +
  keypair lifecycle touch (operator provisioner/broker records, per-tenant orchestrator KV, per-tenant ACL
  policies + kubernetes-auth roles, scoped revoke-prefix). [agentforge 4460a9c]

### Deviations accepted (round 1)
- `cluster_id` anchor moved from the one-shot keys Secret to the `openbao-state` ConfigMap (forced by
  persist-before-unseal — cluster_id isn't known until after unseal). PV-swap (4b) protection preserved,
  reading from the CM. → REQUIRED a paired RBAC fix: `openbao-init` now has create+update on the
  `openbao-state` ConfigMap (was get-only). [ailab 7efd2b1]
- Narrowed policy includes `af/data/operator/broker/*` + `af/data/tenants/*` (keypair.py writes the kid
  public records + the tenant orchestrator private half through the provisioner token).

## Verification (post-fix)
- agentforge: `pytest tests/unit/test_openbao_bootstrap.py` → 34 passed (99% cov); full suite 988 passed,
  81 skipped; ruff + mypy clean.
- ailab: `kubectl kustomize` builds for openbao + openbao-canary; openbao has ZERO ESO CRs (deadlock gone);
  nested-virt gate passes live.

## Round 2 (codex verify) — PENDING
<!-- filled on read -->
