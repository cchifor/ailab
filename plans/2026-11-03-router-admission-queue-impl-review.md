# Implementation review - router-admission-queue - round 1

<!-- codex-impl-review-status: complete -->

Reviewed: llm-router `feat/admission-queue` `93108f5..80d4857` against the finalized plan. Every finding was accepted
and fixed on the branch; each fix has a regression test that fails on the reviewed code (checked by running the new
tests against the round-1 engine: 4 of 4 fail there, all pass after the fix).

## Findings

### Re-selection failures override an already-ended wait
**Location:** packages/modules/inference/index.ts:396
**Severity:** important
**Resolution:** accepted, fixed in llm-router `8955eaa`. `reserveOrWait` now checks, after every selection that did not
admit, whether the wait phase is already over (`over()`: the request signal or `gone` aborted, the whole-request cap
passed, the queue's deadline passed, or the request is out of the line) before it looks at the refusal; if so it
answers by how the wait ended (`ended()`: CONSUMER_CLOSED, MAX_DURATION or QUEUE_TIMEOUT, the previous busy error kept
for the timeout's 429). Tests (`05219e7`): the account goes offline, the waiter is woken, its re-selection is held at the
`routing.evaluated` write, then the consumer returns / the queue deadline passes / the cap passes: each answers by that
ending, with one `admission.dequeued` of the matching outcome and no upstream request.

### The commit point does not enforce the actual deadline
**Location:** packages/modules/inference/index.ts:601
**Severity:** important
**Resolution:** accepted, fixed in `8955eaa`. `Waiting` carries the absolute `until` (registration time plus
`maxWaitMs`); the commit point before `reserve` throws `QUEUE_LEFT` when the ticket is out of the line, or
`Date.now() >= until`, or the execution's whole-request deadline passed; `ended()` reads the cap from the clock too
(`capPassed()`), so a late timer callback changes nothing. The write-only `expired` field is gone (the expiry timer now
only takes the request out of the line). Test: the woken request's `routing.evaluated` write blocks the event loop past
its 100 ms deadline: it answers QUEUE_TIMEOUT with no upstream request and no slot used.

### Timer cleanup is claimed but not tested
**Location:** tests/admission-queue.test.ts:87
**Severity:** nit
**Resolution:** accepted, test added in `05219e7`: with fake timers, `vi.getTimerCount()` is 0 after a wait settles as
timeout, abort, woken and leave, on the same ticket in turn. Mutation M14 (no `clearTimeout` on settle) fails it.

## Also changed since this review

- `61f5f2b`: the two queued image tests (generation and edit) free the held slot by cancelling its client instead of
  waiting for a router timeout; the edit test failed once under full-suite load, when the 1.5 s timeout could pass
  before the edits had queued.

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
