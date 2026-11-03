# Implementation review - router-admission-queue - round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The implementation largely follows the finalized plan, but two reproduced lifecycle defects need correction before acceptance.
- Re-selection can override a waiter's cancellation or timeout, and delayed timer callbacks permit admission after the queue deadline.
- Tests cover many normal paths but miss these interleavings; the timer-cleanup test also lacks assertions that would detect leaked timers.
- No new credential or request-body disclosure was identified in the queue's error bodies or journal events.
- Verification was read-only, including in-memory engine reproductions under Node 26. Full tests and CLI acceptance were not rerun: pnpm and dependencies are absent. The architecture check fails on inference's `Date.now()` usage; that violation already exists in `93108f5`.

## Findings

### Re-selection failures override an already-ended wait
**Location:** packages/modules/inference/index.ts:396
**Severity:** important
<!-- codex: Mark the queued account offline, wake its waiter, hold the second routing.evaluated write, then trigger consumer return, the whole-request cap, or queue expiry: the resulting non-busy CAPACITY_UNAVAILABLE bypasses ended(), reproducing a deferred trajectory after consumer cancellation and an ordinary 429 instead of MAX_DURATION or QUEUE_TIMEOUT. Check the queued request's terminal state when awaited selection or journal operations settle, before propagating another refusal, preserve the previous busy error for timeout mapping, and test all three interleavings. -->

### The commit point does not enforce the actual deadline
**Location:** packages/modules/inference/index.ts:601
**Severity:** important
<!-- codex: The guard checks only ticket membership, which remains valid until the expiry callback executes: with maxWaitMs=1000 and a synchronous 1100 ms delay during the awakened routing.evaluated write, the engine dispatched upstream and recorded admitted with waitedMs=1102. Check the absolute queue deadline synchronously immediately before reserve, leave and map QUEUE_LEFT when overdue, and add a regression where time passes without letting the timer callback run; the current write-only waiting.expired field does not enforce this guarantee. -->

### Timer cleanup is claimed but not tested
**Location:** tests/admission-queue.test.ts:87
**Severity:** nit
<!-- codex: This test checks results and abort listeners but never checks outstanding timers, so its assertions still pass if clearTimeout(timer) is removed from Admission.wait, leaving the 60-second timers alive after abort or leave. Use fake timers or explicit timer tracking to assert cleanup after wake, timeout, abort and leave, including repeated waits on the same ticket. -->

## Diff stat

```text
 README.md                           |   8 +-
 docs/implementation-status.md       |   2 +-
 packages/contracts/index.ts         |  10 +
 packages/modules/admission/index.ts | 100 ++++-
 packages/modules/inference/index.ts | 150 +++++++-
 packages/modules/routing/index.ts   |   9 +-
 packages/plugins/metrics/index.ts   |  13 +-
 packages/runtime/config.ts          |   2 +
 tests/admission-queue.test.ts       | 732 ++++++++++++++++++++++++++++++++++++
 tests/images.test.ts                |  49 +++
 10 files changed, 1052 insertions(+), 23 deletions(-)
```
