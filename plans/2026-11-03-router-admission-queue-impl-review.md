# Implementation review - router-admission-queue - round 2

<!-- codex-impl-review-status: finalized -->

Round 1 (`93108f5..80d4857`) found two important lifecycle defects and one test gap; all three were fixed in
llm-router `8955eaa` and `05219e7` (the round-1 record is in this file's history). Round 2 reviewed `93108f5..61f5f2b`:
codex confirmed the three fixes (its own harness: the four new engine regressions fail on the round-1 engine and pass
at HEAD; removing the re-selection guard, the queue-deadline check or the timer cleanup fails the matching test) and
found one remaining test gap, fixed below. No finding is open.

## Findings

### Whole-request clock guards lack regression coverage
**Location:** tests/admission-queue.test.ts:746
**Severity:** nit
**Resolution:** accepted, test added in llm-router `dda2d3c`: B's route caps the whole request at 100 ms, and its
re-selection after the wake blocks the event loop past that at its `routing.evaluated` write, so the cap's timer cannot
run before the commit point; B answers 504 `TIMEOUT` (`failed`, `MAX_DURATION`, 0 attempts), exactly one
`admission.dequeued` with outcome `max_duration`, no upstream call, no slot used. Mutation checks: removing the
deadline comparison from `capPassed()` (M15) or the cap check from the commit point (M16) each fail it.

## Also changed in round 2

- The unit test of `ahead()` for a ticket of another pool now joins that ticket before this pool's waiters (an older
  sequence number), so a global-order count fails it (mutation M8 survived the round-1 version of the test).

## Verification

- Mutation checks (`tests/admission-queue.test.ts`, each on its own copy of the branch): all 16 fail at least one test:
  no anti-barging; the request signal ignored; the queue deadline ignored; an expired wait kept in the line; no version
  check; `maxWaiting` ignored; queueing on failover; global order for another pool's ticket; two busy pools queueing;
  the commit point by membership only; no commit point; a later refusal winning over an ended wait; owed slots counted
  as load; no `clearTimeout` on settle; the cap read only from its signal; the commit point without the cap.
- CI gates in docker (node 24.22.2, pnpm 10.32.1) at `61f5f2b`: typecheck, test (2073 passed, 14 skipped, 0 failed),
  build, build:lib and test:production pass; check:architecture reports only
  `packages/modules/inference/index.ts: forbidden runtime side effect`, the same line as unchanged `93108f5` (baseline:
  2034 passed, 14 skipped). The test-only commit `dda2d3c`: typecheck and `tests/admission-queue.test.ts` (38 passed).
- The real Codex CLI (0.162.1 and 0.166.0) end to end, concurrency 1, a 60 s upstream, two agents at once: with
  `queue: { maxWaitMs: 120000 }` both exit 0 and the second's request waits 59-61 s for its head with no 429; with
  `maxWaitMs: 20000` the second meets two 429s with `Retry-After: 1`, rejoins 1.0 s later each time, and exits 0; on
  `main` the second exits 1 after 9 × 429 (41-43 s). After each run: no request in flight, no waiter, no slot used.

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
