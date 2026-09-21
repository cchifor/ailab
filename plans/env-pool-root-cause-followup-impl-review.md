# Implementation review — env-pool-root-cause-followup — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Reviewed only `d5098885..HEAD`. All 29 soak tests pass; additional synthetic cases expose nine important findings below.
- The pool-owned capacity source, bounded signal attachment, single-PID identity check, three relay endpoints, and runbook corrections address their intended round-1 issues.
- Telemetry coverage, evidence completion, and relay freshness remain partially unresolved; window boundaries and checkpoint handling introduce further incorrect outcomes.
- No static defect found in the KSM metric names, CRD field paths, `nilIsZero` placement, or added list/watch RBAC. Rendered chart permissions and live metric emission remain unverified here.
- The harness uses the DaemonSet image and is wired into CI; its Bash syntax check passes. Docker and WSL access were denied, preventing BusyBox runtime verification.

## Findings

### A partial evidence dump still counts as complete

**Location:** scripts/env-pool-soak.py:411
**Severity:** important
<!-- codex: round-2: evidence_body emits the state= header before collecting thread stacks, so a subsequent timeout produces both that header and an incomplete line; supplying this actual timeout shape still yields RECURRENCE-CONTAINED without problems. Require successful completion of the dump preceding each kill, and add a verdict fixture containing both the header and timeout marker, as the shell harness can produce. -->

### Partial member and restart telemetry still permits OK

**Location:** scripts/env-pool-soak.py:326
**Severity:** important
<!-- codex: round-2: Coverage failures are unconditionally discarded for member_age and restarts: reducing a continuously observed member's restart series to its first sample still returns OK, and truncating both histories also passes as lease turnover. Validate restart coverage over each observed member's lifetime and aggregate member coverage across legitimate turnover, rather than exempting these series from boundary and coverage checks. -->

### Fresh closures are declared contained before their effects appear

**Location:** scripts/env-pool-soak.py:503
**Severity:** important
<!-- codex: round-2: A ready-port closure five seconds before the window ends, with capacity still reported Ready during probe/controller propagation, now returns RECURRENCE-CONTAINED as a transient and advances the checkpoint; the previous end-of-window guard was removed. Keep such signals pending until subsequent observations establish recovery or sustained capacity, and prevent checkpoint advancement while their outcome is unknown. -->

### Stage-2 reaps disappear from verdict decisions

**Location:** scripts/env-pool-soak.py:410
**Severity:** important
<!-- codex: round-2: Restricting reaps to stage=1 also removes stage-2 kills from recurrence signals and sandbox relay checks, so adding a stage-2 shim kill to an otherwise quiet window returns OK. Parse both stages for incident reporting and relay correlation, while requiring the pre-kill evidence dump only for stage 1. -->

### Untimestamped replay can still manufacture fresh coverage

**Location:** scripts/env-pool-soak.py:426
**Severity:** important
<!-- codex: round-2: Unparsed relay records retain their ingestion timestamps and participate in coverage whenever they constitute at most half the raw lines; replaying three timestamped old records plus one untimestamped record every five minutes reproduces OK without fresh source evidence. Exclude records lacking a valid source timestamp from freshness and incident-evidence calculations, regardless of their proportion, while retaining them in raw exports. -->

### Replay outside the window creates fictitious capture gaps

**Location:** scripts/env-pool-soak.py:470
**Severity:** important
<!-- codex: round-2: Source timestamps are never restricted to the requested interval: adding one record from an hour before start to an otherwise fully covered window produces INCOMPLETE for an entirely pre-window gap, and pre-window records also remain eligible for relay_per_sb. Restrict coverage checks to the requested interval and require incident-relevant source times for sandbox evidence, preserving older replay only as historical/raw data. -->

### An incident already underway gets an invented start time

**Location:** scripts/env-pool-soak.py:492
**Severity:** important
<!-- codex: round-2: If capacity is already missing at the first sample and returns two minutes later, the report certifies RECURRENCE-CONTAINED within two minutes even though the outage may have begun more than ten minutes before the window. Recover the actual start from earlier observations or persisted incident state, and otherwise report the duration as unknown rather than certifying containment. -->

### Missing desired capacity fabricates an unresolved incident

**Location:** scripts/env-pool-soak.py:479
**Severity:** important
<!-- codex: round-2: A failed warm_spec query with warm_ready=0 substitutes replicas=1 and yields UNRESOLVED ahead of INCOMPLETE, although the requested capacity is unknown and could legitimately be zero; missing individual timestamps similarly borrow the maximum target from unrelated times. Evaluate deficits only from matched observed ready/spec samples and treat missing target observations as incomplete data rather than inventing an outage. -->

### A closed slow incident permanently blocks the checkpoint

**Location:** scripts/env-pool-soak.py:262
**Severity:** important
<!-- codex: round-2: should_advance_checkpoint treats every unresolved entry as an open incident, but a fully observed fifteen-minute outage remains in unresolved after recovery, so subsequent checkpoint runs repeatedly include it and cannot advance before eventually exceeding retention. Track open incidents separately from historical recovery-bound violations, preserving the adverse verdict while allowing complete windows with confirmed recovery to advance. -->

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