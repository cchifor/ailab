# Implementation review — retire-playground — round 1 (Phase 1b)

<!-- codex-impl-review-status: complete -->

## Findings

### Worker alert deletion also removes platform-dev coverage
**Location:** kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml:179  
**Severity:** blocker  
Accepted (blocker, verified live): platform-dev's CP PodMonitor relabels job=agentforge-worker and is that job's only series. Fixed in 08d7bfdf: the six worker rules are restored, ForgeTargetDown is narrowed to the worker job, and only ForgeDispatchStalled and ForgeScaleOracleMissing are deleted; the P0 rule tests are restored minus ForgeDispatchStalled.

### Rollback record is saved after mutation and discarded on failure
**Location:** scripts/af-revoke-legacy-kid.sh:34  
**Severity:** important  
Accepted. Fixed in 98b73087: the driver prints ROLLBACK on every run before any write; the wrapper runs a dry pass, saves and verifies the record, and only then executes; the execute output is kept on every path. Re-verified live (dry run).

### The revocation audit record never reaches a configured sink
**Location:** scripts/af_revoke_legacy_kid.py:138  
**Severity:** important  
Accepted. Fixed in 98b73087: configure_audit_logging routes agentforge.provisioner.audit INFO records to stdout (kept in the execute log) and, best effort, to /proc/1/fd/1, the provisioner container's own log stream; tested with a stream and a file sink. The private metrics registry stays unexported by design: the live provisioner exports its own counters.

### Required namespace lifecycle admission tests are absent
**Location:** scripts/check-tenant-guard-cel.py:547  
**Severity:** important  
<!-- codex: Phase 1b explicitly requires playground and platform-dev CREATE/UPDATE/DELETE coverage, but this harness is unchanged, hardcodes CREATE, never loads tenant-reserved-guard, and tests the playground reservation only through a PodMonitor creation. Add namespace lifecycle fixtures for both policies, including the DELETE request shape and identity matching, proving that the tenant reconciler remains barred from the reserved namespace while platform-dev operations and operator cleanup remain allowed. -->
<!-- opus-pushback: Accepted in part (424d88fd): rule 16 is name-based and reads no tenant-map entry, and new Namespace CREATE fixtures prove the reserved name is denied (16) and platform-dev's is admitted, 36/36. A DELETE harness for tenant-reserved-guard is out of scope: this PR does not change that policy (name-based, matches only the tenants-reconciler identity), and Phase 2 checks the one DELETE this PR causes live, main Flux pruning the namespace. -->

### Seat-entitlement remediation and its fixture were not updated
**Location:** kubernetes/apps/infrastructure/monitoring/agentforge-provisioner-rules.yaml:405  
**Severity:** nit  
Accepted. Fixed in 1fba37e2: annotation and promtool fixture changed together; the folded descriptions match exactly.

### Documentation declares the deferred revocation complete
**Location:** docs/decisions/0019-agentforge-v2-control-plane.md:334  
**Severity:** nit  
Accepted. Fixed in dc648bfc: ADR 0019, the ConfigMap and the platform-dev comment now describe revocation as Phase 3 of the retirement, not as done.

### Active runbook procedures still reference deleted workloads
**Location:** docs/runbooks/agentforge.md:482  
**Severity:** nit  
Accepted. Fixed in dc648bfc: the repin procedure lists the two surviving digests, the inventory marks the dispatcher retired, and the job table notes that the CP pools now carry job=agentforge-worker.

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