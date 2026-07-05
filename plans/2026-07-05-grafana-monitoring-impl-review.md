# Implementation review — grafana-monitoring — round 1

<!-- codex-impl-review-status: finalized -->

Genuine codex `gpt-5.5` review of `be0d638..HEAD` against the finalized plan (model confirmed via codex.log; verbatim output at `.claude/worktrees/codex-review-20260705-115746/codex-out.txt`). No blockers; converged in one round.

## Findings

### Missing zero-target guards on new discrete count stats — FIXED

**Location:** `scripts/gen-reporting-dashboard.py` (Runner/Worker Cores + Memory stats)
**Severity:** important
**Outcome:** fixed — added `or vector(0)` to Runner Cores, Runner Memory, Worker Cores, Worker Memory (commit after this review). Verified 4/4 guards present; maxY still 124.

### Fleet dashboard has a vertical gap after the AI row — ACCEPTED (no change)

**Location:** `scripts/gen-reporting-dashboard.py` (AI row ends y=65, Storage starts y=71)
**Severity:** nit
**Outcome:** no change (documented decision). The plan explicitly chose to accept the 6-unit gap: closing it means reflowing Storage + both new sections and losing the round `maxY=124`, for a cosmetic gain Grafana rows largely absorb. Codex offered "explicitly accept the gap" as a valid resolution.

## Checks Passed

- KSM custom-resource-state: indentation, `metricNamePrefix: gotk`, metric labels, RBAC, default collectors correct.
- Flux PodMonitor: `monitoring` namespace, `flux-system` namespaceSelector, `app In (...)`, `http-prom` port all match plan.
- k8s-releases dashboard: 17 non-row panels, 3 rows; table renames `customresource_kind`/`exported_namespace`; reconcile duration groups by `kind`.
- Pods-not-ready query: `count`, `and on(namespace,pod)`, active-phase filter `Pending|Running|Unknown`, `or vector(0)`.
- Kustomization registration: new monitor/dashboard files included.
- Job labels `ci-runner-node`, `dev-worker-node`, `ai-llm-node` consistent across monitors and Fleet selectors.

## Diff stat

```
 kubernetes/apps/apps/ai/monitoring.yaml            |   3 +
 .../monitoring/dev-workers-node.yaml               |  50 ++++++
 .../infrastructure/monitoring/flux-monitoring.yaml |  25 +++
 .../monitoring/k8s-releases-dashboard.yaml         |  14 ++
 .../monitoring/kube-prometheus-stack.yaml          |  86 +++++++++++
 .../infrastructure/monitoring/kustomization.yaml   |   3 +
 .../monitoring/reporting-dashboard.yaml            |   2 +-
 scripts/gen-k8s-releases-dashboard.py              | 171 +++++++++++++++++++++
 scripts/gen-reporting-dashboard.py                 |  79 ++++++++--
 9 files changed, 420 insertions(+), 13 deletions(-)
```
