# Implementation review — iac-activation

<!-- codex-impl-review-status: finalized -->

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

## Round 2 → Round 3 (codex verify) — CLOSED
- Round 2: 5 of 6 round-1 findings CLOSED; 2 blockers remained — (A) run_init two-Secret write not atomic
  (a hard-kill could lose the unseal key), (B) NEW openbao-state clobber (init+provision both wrote it, full-
  replace).
- Fixed by ONE atomic-boot redesign [agentforge 643b65b]: after initialize(), BOTH one-shot materials go
  into a SINGLE `openbao-boot` Secret before any unseal; unseal; SPLIT to durable openbao-keys{+cluster_id}
  + openbao-bootstrap-token; delete boot. run_init RESUMES from openbao-boot on a mid-split crash (never
  re-inits). cluster_id back in openbao-keys; run_init no longer writes openbao-state (provision is sole
  writer); write_state MERGES. 38 tests (was 34), full suite 992 passed, ruff+mypy clean. Paired init-SA
  RBAC for openbao-boot [ailab 8e6e9ae].
- Round 3 (the cap) = A + B CONFIRMED CLOSED. One residual: write_state PATCHes the CM but openbao-provision
  had only `update` → added `patch` on openbao-state [ailab, this commit]. SSAR precheck covered by the
  default system:basic-user (fails closed if removed).

**FINALIZED — codex Phase B 3 rounds → the OpenBao auto-init/unseal/provision bring-up is crash-safe +
least-privilege; Stage 1 is SAFE to execute live.** Documented residual: the sub-second pre-atomic-boot-
create window loses only an EMPTY vault (recovery = PVC re-init); true crash-safety needs transit/KMS
auto-unseal (a future hardening, no seal backend in the homelab).
