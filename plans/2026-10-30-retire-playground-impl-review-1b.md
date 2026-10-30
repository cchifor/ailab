# Implementation review — retire-playground — round 1 (Phase 1b)

<!-- codex-impl-review-status: pending -->

## Summary

- Changes required: deleting the worker alert family also removes coverage for CP-rendered pools, including platform-dev, which use the same `agentforge-worker` job label.
- The resource cleanup otherwise follows the planned Flux split. Assuming Phase 1a’s shutdown checks passed, the retained RBAC, reaper, canary storage, broker policies, and namespace reservation remain coherent; removing the tenant-map entry does not block operator or ESO cleanup.
- The seed coverage hash matches the encrypted document, all Secret values remain encrypted, and `gen-broker-inventory.py --check` passes. The recorded removals fit the provision Job’s seed iteration, but decrypted path coverage could not be independently verified here.
- The revocation driver uses the correct agentforge helper signature and handles absence and supersession safely, but rollback persistence and audit logging are defective. Planned admission fixtures and several operational documentation updates are missing.
- Thirty-eight focused tests and all 34 existing CEL cases passed. Broader tests encountered read-only temporary-directory and shell-execution failures; kustomize/promtool validation remains unverified.

## Findings

### Worker alert deletion also removes platform-dev coverage
**Location:** kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:179  
**Severity:** blocker  
<!-- codex: The claim that job="agentforge-worker" belongs exclusively to the playground is false: scripts/check-tenant-guard-cel.py:105 models the CP monitor with that job, agentforge-platform's origin/main renderer confirms it, and AFP_METRICS_PODMONITOR_ENABLED remains true, so seven deleted alerts also cover CP pools; ForgeOrchestratorUnscraped cannot replace their reconcile, authentication, backlog, drift, escalation, and operator-service checks. Retain the worker alert family and ForgeTargetDown's worker selector, remove only the dispatcher-specific coverage, and add a platform-dev Prometheus fixture to prevent this regression. -->

### Rollback record is saved after mutation and discarded on failure
**Location:** scripts/af-revoke-legacy-kid.sh:34  
**Severity:** important  
<!-- codex: The wrapper waits for the entire remote execution before saving the rollback record, and its error branch explicitly removes the ROLLBACK line even when removal succeeded but the subsequent read or platform-dev comparison failed; an unwritable rollback directory is likewise discovered only after revocation. Persist and verify the complete public rollback artifact before permitting mutation, retain it on every failure path, and test successful removal followed by verification failure and local persistence failure. -->

### The revocation audit record never reaches a configured sink
**Location:** scripts/af_revoke_legacy_kid.py:138  
**Severity:** important  
<!-- codex: This creates a new audit object with an unexported metrics registry inside an unconfigured python process: ProvisionerAudit.emit logs at INFO, which the default WARNING level suppresses, leaving the revoked record only in memory until exit rather than in the provisioner's audit trail. Configure and retain the sanitized audit output through an explicit sink, and exercise the execute path in a test that asserts a revoked record is actually emitted. -->

### Required namespace lifecycle admission tests are absent
**Location:** scripts/check-tenant-guard-cel.py:547  
**Severity:** important  
<!-- codex: Phase 1b explicitly requires playground and platform-dev CREATE/UPDATE/DELETE coverage, but this harness is unchanged, hardcodes CREATE, never loads tenant-reserved-guard, and tests the playground reservation only through a PodMonitor creation. Add namespace lifecycle fixtures for both policies, including the DELETE request shape and identity matching, proving that the tenant reconciler remains barred from the reserved namespace while platform-dev operations and operator cleanup remain allowed. -->

### Seat-entitlement remediation and its fixture were not updated
**Location:** kubernetes/apps/infrastructure/monitoring/agentforge-provisioner-rules.yaml:405  
**Severity:** nit  
<!-- codex: The alert still directs operators to an open playground authorization decision and describes three of four seats, while its matching expectation in agentforge-provisioner-rules.test.yaml:317 preserves the same stale guidance; both updates were explicitly required by Phase 1b. Replace this with the retirement decision and guidance for an incomplete revocation or reintroduced grant, updating the fixture alongside the annotation without weakening the alert expression. -->

### Documentation declares the deferred revocation complete
**Location:** docs/decisions/0019-agentforge-v2-control-plane.md:334  
**Severity:** nit  
<!-- codex: This Phase 1b change ships the revocation tool but does not execute Phase 3, so “Revoked” prematurely asserts an authority change that namespace pruning does not perform for the legacy kid; capability-kids-configmap.yaml:48 and agentforge-tenant-platform-dev/externalsecret.yaml:14 make the same assertion. Mark revocation as pending Phase 2 verification and Phase 3 execution, then record completion only after every broker replica confirms absence. -->

### Active runbook procedures still reference deleted workloads
**Location:** docs/runbooks/agentforge.md:482  
**Severity:** nit  
<!-- codex: The active image-repin procedure still instructs operators to edit the deleted dispatcher-deployment.yaml and worker-deployment.yaml, while the namespace table at line 162 continues to describe the dispatcher as always-on. Update the current operating procedures and inventory to the surviving workloads, and explicitly mark obsolete instructions as historical rather than leaving commands that now fail. -->

## Diff stat

```text
 .gitea/workflows/broker-inventory.yaml             |   2 +-
 .../0018-agentforge-autonomous-dev-agents.md       |   7 +
 docs/decisions/0019-agentforge-v2-control-plane.md |  14 +
 docs/runbooks/agentforge-v3-cutover.md             |   5 +
 docs/runbooks/agentforge.md                        |  46 ++-
 docs/runbooks/sandbox-boundary-canary.md           |   6 +
 .../apps/apps/agentforge/admission/tenant-map.yaml |  15 +-
 .../broker-anthropic-claude-max-3.yaml             |   2 +
 .../broker-anthropic-claude-max-4.yaml             |   2 +
 .../agentforge-broker/broker-anthropic-max1.yaml   |   2 +
 .../agentforge-broker/broker-anthropic-max2.yaml   |   2 +
 .../agentforge-broker/broker-inventory.yaml        |   6 +-
 .../agentforge-broker/broker-openai-codex.yaml     |   2 +
 .../orchestrator-endpoints-rbac.yaml               |   8 +-
 .../orchestrator-endpoints-rbac.yaml               |   7 +-
 .../agentforge-sandbox/kustomization.yaml          |   7 +-
 .../agentforge-sandbox/orchestrator-rbac.yaml      |   5 +-
 .../agentforge-sandbox/reaper-storage.yaml         |   6 +
 .../agentforge-sandbox/staging-pv.yaml             |  51 ---
 .../agentforge-sandbox/tenant-namespace.yaml       |  34 --
 .../agentforge-sandbox/tenant-resourcequota.yaml   |  76 -----
 .../agentforge-sandbox/workspace-pvc.yaml          |   7 +
 .../externalsecret.yaml                            |  11 +-
 .../agentforge-workers/cilium-egress.yaml          | 108 -------
 .../agentforge-workers/conductor-netpol.yaml       |   4 +-
 .../agentforge-workers/dispatcher-deployment.yaml  | 219 -------------
 .../dispatcher-externalsecret.yaml                 | 154 ---------
 .../agentforge-workers/dispatcher-netpol.yaml      |  97 ------
 .../agentforge-workers/dispatcher-service.yaml     |  34 --
 .../dispatcher-serviceaccount.yaml                 |  14 -
 .../dispatcher-servicemonitor.yaml                 |  29 --
 .../agentforge-workers/kustomization.yaml          |  83 +----
 .../agentforge-workers/scaledobject.yaml           | 106 -------
 .../agentforge-workers/worker-deployment.yaml      | 304 ------------------
 .../agentforge-workers/worker-externalsecret.yaml  |  42 ---
 .../agentforge-workers/worker-openbao-ca-cert.yaml |  25 --
 .../agentforge-workers/worker-podmonitor.yaml      | 128 --------
 .../agentforge-workers/worker-secretstore.yaml     |  74 -----
 .../agentforge-workers/worker-serviceaccount.yaml  |  18 --
 .../agentforge-workers/worker-staging-pvc.yaml     |  26 --
 .../agentforge-workers/worker-webhook-service.yaml |  38 ---
 .../monitoring/agentforge-coverage-rules.yaml      |  12 +-
 .../monitoring/agentforge-rules.yaml               | 343 +--------------------
 .../infrastructure/monitoring/kustomization.yaml   |   2 +-
 .../openbao/capability-kids-configmap.yaml         |   7 +-
 .../security/openbao/operator-seeds.sops.yaml      |  15 +-
 .../security/openbao/provisioner-deploy.yaml       |   2 +-
 scripts/af-revoke-legacy-kid.sh                    |  42 +++
 scripts/af_revoke_legacy_kid.py                    | 162 ++++++++++
 scripts/gen-litellm-consumers.py                   |   2 +-
 scripts/tests/test_af_revoke_legacy_kid.py         |  96 ++++++
 scripts/tests/test_broker_refresh_intervals.py     |  53 +---
 scripts/tests/test_provisioner_podmonitor.py       |   3 +-
 scripts/tests/test_reviewbot.py                    |   2 +-
 scripts/tests/test_worker_probes.py                | 204 ------------
 55 files changed, 497 insertions(+), 2274 deletions(-)
```