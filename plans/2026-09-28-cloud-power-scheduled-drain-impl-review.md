# Implementation review — cloud-power-scheduled-drain — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### Reject malformed and incomplete Gitea lists before counting a clear poll
**Location:** kubernetes/apps/apps/cloud-power/app.py:381  
**Severity:** blocker
**Resolution:** Fixed: `_paged` requires the key and an integer total_count, rejects a premature empty page and mistyped fields (bool vs int guarded); a null/empty runner_name counts as in flight. Tests: ImplReviewRegressionTests missing key / total / premature empty page / mistyped / null runner_name / null list with total 0.

### Failed rollback persistence can resurrect a rejected OFF
**Location:** kubernetes/apps/apps/cloud-power/app.py:649  
**Severity:** blocker
**Resolution:** Fixed: `dirty` flag — no side effect until the ConfigMap has caught up (retried first each tick); `_to_releasing` re-enables only after `releasing` is durable; the schedule is persisted as `pausing` and becomes `draining` only after every pause, and a restart that finds `pausing` rolls back. Test: failed pause during a ConfigMap outage + restart never powers off; cancel not durable re-enables nothing.

### Persist ownership before pausing runners discovered during drain
**Location:** kubernetes/apps/apps/cloud-power/app.py:756  
**Severity:** important
**Resolution:** Fixed: the runner is appended and persisted BEFORE the PATCH; no PATCH if the adoption cannot be saved. Tests: lost PATCH reply still handed back on CANCEL; no pause without a durable owner.

### Serialize ON with scheduling instead of consulting the published snapshot
**Location:** kubernetes/apps/apps/cloud-power/app.py:1083  
**Severity:** important
**Resolution:** Fixed: `Scheduler.cancel_for_wake` decides and cancels under the scheduler lock on the current state; a cancel that cannot be persisted returns 503 and ON is not sent. Tests: ON during a schedule blocked mid-pause withdraws it; ON while powering off reports; ON fails when the cancel cannot be persisted.

### Measure controller staleness against a clock that continues advancing
**Location:** kubernetes/apps/apps/cloud-power/app.py:944  
**Severity:** important
**Resolution:** Fixed: /api/status stamps `served_at` and `poll_sec` per request; the page compares `served_at - last_tick` against 3 polls + 15 s. There is no JS harness in CI, so the test is server-side: a frozen snapshot is served with a fresh clock (StatusEndpointTests).

## Diff stat

```text
 .../0032-opportunistic-cloud-ci-runners.md         |  35 +
 docs/runbooks/ci-runners.md                        |  29 +-
 kubernetes/apps/apps/cloud-power/app.py            | 806 ++++++++++++++++++++-
 kubernetes/apps/apps/cloud-power/deployment.yaml   |  32 +-
 .../apps/apps/cloud-power/kustomization.yaml       |   2 +
 kubernetes/apps/apps/cloud-power/rbac.yaml         |  48 ++
 .../apps/apps/cloud-power/secret-gitea.sops.yaml   |  43 ++
 scripts/tests/test_cloud_power.py                  | 558 ++++++++++++++
 8 files changed, 1513 insertions(+), 40 deletions(-)
```