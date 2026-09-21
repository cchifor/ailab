# Implementation review — env-pool-root-cause-followup — round 2

<!-- codex-impl-review-status: finalized -->

## Findings

### A partial evidence dump still counts as complete

**Location:** scripts/env-pool-soak.py:411
**Severity:** important
**Resolution (round 2):** the incomplete marker now wins per (sandbox, pid): a state= header followed by a timeout line is not a complete dump. Test: header-then-timeout → INCOMPLETE.

### Partial member and restart telemetry still permits OK

**Location:** scripts/env-pool-soak.py:326
**Severity:** important
**Resolution (round 2):** restart counters must cover each member's observed lifetime and the union of member observations must cover the window. Tests: counter truncated to one sample; both histories truncated with no successor.

### Fresh closures are declared contained before their effects appear

**Location:** scripts/env-pool-soak.py:503
**Severity:** important
**Resolution (round 2):** a signal within EVENT_ATTACH_SECONDS of the window end is pending — INCOMPLETE with the checkpoint held (open_incident). Test added.

### Stage-2 reaps disappear from verdict decisions

**Location:** scripts/env-pool-soak.py:410
**Severity:** important
**Resolution (round 2):** both stages are parsed; stage-2 kills are signals and need in-window relay correlation; the pre-kill dump is required for stage 1 only. Test added.

### Untimestamped replay can still manufacture fresh coverage

**Location:** scripts/env-pool-soak.py:426
**Severity:** important
**Resolution (round 2):** records without a valid containerd time= are excluded from capture/evidence regardless of their share (kept in the raw export, counted in the report). Test: 3 old + 1 untimestamped every 5 min → no in-window capture.

### Replay outside the window creates fictitious capture gaps

**Location:** scripts/env-pool-soak.py:470
**Severity:** important
**Resolution (round 2):** only records with a source time inside [from, to] count for coverage and per-sandbox evidence; earlier replay is reported as history. Test: a pre-window record in a fully covered window → OK.

### An incident already underway gets an invented start time

**Location:** scripts/env-pool-soak.py:492
**Severity:** important
**Resolution (round 2):** an incident deficient at the first matched sample is reported with unknown start/duration (INCOMPLETE, re-run with an earlier --from) instead of being certified contained. Test added.

### Missing desired capacity fabricates an unresolved incident

**Location:** scripts/env-pool-soak.py:479
**Severity:** important
**Resolution (round 2):** deficits are evaluated only where ready and spec are both observed for the same step; a missing target is INCOMPLETE, never an outage. Tests: spec absent with ready=0; spec unobserved where the dip falls.

### A closed slow incident permanently blocks the checkpoint

**Location:** scripts/env-pool-soak.py:262
**Severity:** important
**Resolution (round 2):** Report.open_incident is tracked separately; a fully observed incident past the bound keeps UNRESOLVED but the checkpoint advances; only open incidents/pending signals hold it. Test added.

## Diff stat

```text
 .gitea/workflows/manifests.yaml                    |   9 +
 docs/runbooks/env-pool.md                          |  39 ++-
 .../infrastructure/monitoring/cri-log-relay.yaml   |   2 +-
 .../monitoring/kube-prometheus-stack.yaml          |  33 ++
 .../apps/infrastructure/testpool/env-reaper.yaml   |  33 +-
 plans/env-pool-root-cause-followup-impl-review.md  |  28 +-
 scripts/env-pool-soak.py                           | 268 ++++++++-------
 scripts/tests/test-env-reaper.sh                   | 130 ++++++++
 scripts/tests/test_env_pool_soak.py                | 367 +++++++++++----------
 9 files changed, 579 insertions(+), 330 deletions(-)
```