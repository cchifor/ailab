# Implementation review — r3-worker-autoscaling — round 1
<!-- codex-impl-review-status: finalized -->

**FINALIZED — codex Phase B round 2 = READY TO MERGE onto feat/p2-unlock.** R1 3 important -> all fixed -> R2 all CLOSED, no regressions. Dormant (both Deployments gated/unlisted, no privilege_hardening flip). Cross-repo follow-up (non-blocking): the af-dispatcher must emit forge_pending{pool=<pool>} not {pool=<account>} before activation.
## Summary
- Dormancy discipline is mostly intact: both Deployments are unlisted, gated, and still use placeholder digests; listing the ScaledObject while the target Deployment is gated is explicitly documented as a dormant `Ready=False` state.
- Reuse is mostly correct: the worker uses the existing `af-orch-playground-planner` ServiceAccount, existing tenant credentials, and the expected pinned `AF_SANDBOX_*` values; capability signing material remains worker-only.
- I found three important defects: one KEDA activation threshold bug, one tenant NetworkPolicy overlap that breaks the intended worker boundary, and one missing Flux dependency on External Secrets CRDs.
- The dispatcher side is appropriately read-only, and the in-cluster Gitea target is consistent with the configured `AF_GITEA_URL`.

## Findings
### KEDA will not wake for exactly one pending issue
**Location:** kubernetes/apps/infrastructure/agentforge-workers/scaledobject.yaml:47
**Severity:** important
<!-- codex: `activationThreshold: "1"` is off by one for the Prometheus trigger. KEDA activation uses a strict greater-than comparison, so a metric value of exactly 1 will not activate the ScaledObject; it would require more than one pending item before scaling from zero outside the cron warm window. This remains a bug even after the documented cross-repo `pool="planner"` dispatcher follow-up lands. Keep `threshold: "1"` for scaling math, but set `activationThreshold: "0"` or omit it. KEDA 2.20 docs: https://keda.sh/docs/2.20/concepts/scaling-deployments/#activating-and-scaling-thresholds -->

### Worker pod is still selected by the tenant NetworkPolicy
**Location:** kubernetes/apps/infrastructure/agentforge-workers/worker-deployment.yaml:60
**Severity:** important
<!-- codex: The worker template carries `agentforge.io/pool: planner`, which matches the existing tenant renderer's NetworkPolicy selector for that pool. That policy has `ingress: []` and broad egress by port, so in the reused tenant namespace the worker does not actually have the intended boundary of forge + kube-apiserver + DNS only, and metrics ingress from monitoring is not allowed for selected worker pods. Fix by preventing operator workers from matching the CP-rendered tenant NetworkPolicy, for example by reconciling that selector to include a CP-only label or by removing/replacing this pool label on the operator worker and targeting the worker Cilium policy with an operator-only label. Then add an explicit worker metrics ingress path if worker metrics are intended to be scraped. -->

### Flux can apply ExternalSecret resources before ESO CRDs exist
**Location:** kubernetes/apps/clusters/ai/agentforge-workers.yaml:16
**Severity:** important
<!-- codex: This Kustomization applies `SecretStore` and `ExternalSecret` resources from `dispatcher-externalsecret.yaml`, but it only depends on `infrastructure` and `keda`. External Secrets is installed by the separate `external-secrets` Flux Kustomization, not by `infrastructure`, so bootstrap/rebuild ordering can dry-run or apply these CRs before `external-secrets.io/v1` exists. Add `dependsOn: - name: external-secrets` alongside the existing KEDA dependency. -->

## Diff stat
 .../apps/clusters/ai/agentforge-workers.yaml       |  34 +++++
 .../agentforge-workers/cilium-egress.yaml          |  66 +++++++++
 .../agentforge-workers/dispatcher-deployment.yaml  | 106 ++++++++++++++
 .../dispatcher-externalsecret.yaml                 |  72 ++++++++++
 .../agentforge-workers/dispatcher-netpol.yaml      |  66 +++++++++
 .../dispatcher-service.yaml                        |  22 +++
 .../dispatcher-serviceaccount.yaml                 |  14 ++
 .../dispatcher-servicemonitor.yaml                 |  26 ++++
 .../agentforge-workers/kustomization.yaml          |  27 ++++
 .../agentforge-workers/scaledobject.yaml           |  57 ++++++++
 .../agentforge-workers/worker-deployment.yaml      | 156 +++++++++++++++++++++
 11 files changed, 646 insertions(+)
---
## Round-1 addressed (all 3 — commits 725f8d8..f9eecac)
- [FIXED] KEDA activationThreshold "1"->"0" (strict >): one pending issue now wakes 0->1; threshold "1" kept.
- [FIXED] operator worker decoupled from the CP tenant NetworkPolicy (selector {agentforge.io/pool:planner},
  ingress:[], broad egress): worker now carries operator-only labels (app.kubernetes.io/name=agentforge-worker,
  managed-by=agentforge-operator), NO agentforge.io/* labels; the worker CNP retargets those + adds ingress
  default-deny + metrics-from-monitoring. Verified sandbox VAPs pin the pool label on the Job pods, not the orchestrator.
- [FIXED] Flux Kustomization dependsOn += external-secrets (ESO CRDs before the SecretStore/ExternalSecret apply).
Cross-repo FOLLOW-UP (documented, not this tranche): the af-dispatcher emits forge_pending{pool=<account>} (P1);
the KEDA query filters pool="planner" — a small agentforge dispatcher change is needed before activation
(fail-safe until then: empty -> ignoreNullValues -> 0 + the cron warm-floor).
