# Implementation review — grafana-monitoring — round 1

<!-- codex-impl-review-status: pending -->

Genuine codex `gpt-5.5` review of `be0d638..HEAD` against the finalized plan (model confirmed via codex.log; verbatim output at `.claude/worktrees/codex-review-20260705-115746/codex-out.txt`).

## Summary

No blocker findings. The implementation matches the plan: new monitors are registered, Flux PodMonitor shape is correct, KSM CRS is under `spec.values.kube-state-metrics`, dashboards come from generators, k8s-releases has 17 non-row panels + 3 rows, Fleet has 49 non-row panels + 6 rows with maxY 124. Two non-blocking findings below.

## Findings

### Missing zero-target guards on new discrete count stats

**Location:** `scripts/gen-reporting-dashboard.py` (Runner/Worker Cores + Memory stats)
**Severity:** important
<!-- codex: The Runner/Worker "Up" stats use `or vector(0)`, but the new core-count stats `count(node_cpu_seconds_total{job=...,mode="idle"})` (and the memory-total `sum(...)` stats) do not. If a pool is fully powered off, Grafana renders "No data" instead of 0. Add `or vector(0)` to those discrete summary stats. -->

### Fleet dashboard has a vertical gap after the AI row

**Location:** `scripts/gen-reporting-dashboard.py` (AI row ends y=65, Storage starts y=71)
**Severity:** nit
<!-- codex: After trimming the AI row to 5 panels, the last AI panels end at y=65 but Storage still starts at y=71, leaving a 6-grid-unit blank gap. No overlap; maxY=124 preserved. Either move Storage + later rows up by 6, or explicitly accept the gap. -->

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
