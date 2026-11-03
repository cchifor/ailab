# Implementation review - router-admission-queue - round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Reviewed `93108f5..61f5f2b` against the finalized plan. All three round-1 findings are resolved; no additional runtime defect was reproduced.
- Replayed 19 existing unit, engine and routing test bodies using a read-only, dependency-free Node harness: all passed at HEAD; four failed against the round-1 engine. Removing the re-selection guard, queue-deadline check or timer cleanup fails the corresponding regressions.
- Additional blocked-event-loop probes confirmed the current whole-request clock checks prevent late admission and record `max_duration`. Their regression coverage has one gap below.
- `pnpm verify` could not run because `pnpm` and dependencies are absent; official Vitest, HTTP/image, browser and PostgreSQL tests were not run. The architecture check fails on `Date.now()` in inference, also present on baseline `93108f5`; `git diff --check` passes.

## Findings

### Whole-request clock guards lack regression coverage
**Location:** tests/admission-queue.test.ts:746
**Severity:** nit
<!-- codex: Independently removing the deadlineAt comparison from capPassed() or the commit-point cap check leaves all five added regression cases passing: the cap test allows its timer to fire, whereas the blocked-event-loop test exercises only the queue deadline. Add a blocked-event-loop case with maxDurationMs shorter than maxWaitMs, asserting TIMEOUT/MAX_DURATION, exactly one max_duration dequeue, zero attempts and no upstream call; additional probes demonstrated that removing these guards respectively exposes QUEUE_LEFT or permits a late upstream start. -->

## Diff stat

```text
 README.md                           |   8 +-
 docs/implementation-status.md       |   2 +-
 packages/contracts/index.ts         |  10 +
 packages/modules/admission/index.ts | 100 ++++-
 packages/modules/inference/index.ts | 158 ++++++-
 packages/modules/routing/index.ts   |   9 +-
 packages/plugins/metrics/index.ts   |  13 +-
 packages/runtime/config.ts          |   2 +
 tests/admission-queue.test.ts       | 813 ++++++++++++++++++++++++++++++++++++
 tests/images.test.ts                |  51 +++
 10 files changed, 1143 insertions(+), 23 deletions(-)
```
