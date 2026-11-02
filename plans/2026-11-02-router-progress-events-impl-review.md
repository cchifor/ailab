# Implementation review — router-progress-events — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The implementation largely follows the planned scope. The normal streaming path preserves sequence ordering, pre-output gating, backpressure handling and a stable `created_at`.
- Changes are needed around synchronous provider-open failures: activity ownership survives into asynchronous failure handling, allowing callbacks from a failed provider to authorize progress.
- The supplied Codex and mutation results support the reported fix, but do not establish the required OpenAI Node/Python stream-helper compatibility.
- Tests miss important cleanup windows, and the new cancellation test can hang when the behavior it checks regresses. Progress envelopes also drift from the promised identical snapshot.
- Review remained read-only; the supplied checks were not rerun. No new authentication or secret-exposure issue was identified; duration caps and accepted uncapped paths remain the plan’s explicit release decision.

## Findings

### Clear activity ownership before recording synchronous open failures
**Location:** packages/modules/inference/index.ts:599, packages/modules/inference/index.ts:696
**Severity:** important
<!-- codex: A synchronous throw from open() or [Symbol.asyncIterator]() bypasses the inner cleanup and leaves the failed attempt owning activity during awaited failure recording; an isolated in-memory probe with mocked routing and recording observed upstreamActivity() remain 1 and increase to 2 when that failed provider’s saved callback ran. Invalidate ownership before the catch’s awaits, so failed-provider callbacks cannot authorize progress events, and add a delayed-recorder regression test for this path. -->

### Ownership tests do not exercise the cleanup window
**Location:** tests/stream-timeouts.test.ts:735, tests/stream-timeouts.test.ts:747
**Severity:** important
<!-- codex: The new engine tests inspect activity only after execution finishes or the second attempt starts, so removing the inner ownership clear at inference/index.ts:648 would still satisfy them through the outer clear and subsequent ownership replacement. Hold an upstream return() pending during timeout cleanup, invoke the abandoned attempt’s callback, and assert activity is already undefined before closeGrace expires, during failure recording, and until the next attempt receives its own bytes. -->

### Broad client enablement lacks the planned SDK release evidence
**Location:** packages/plugins/api-openai/index.ts:485
**Severity:** important
<!-- codex: Progress events are enabled for every non-native Responses caller, but the supplied verification does not cover the finalized plan’s explicit Node/Python stream-helper release gate, and the repository contains no Responses helper tests exercising it. Record pinned-version comparisons against main for text, tool arguments, usage, incomplete completion and timeout failure before release, applying the planned Codex-only fallback if helper compatibility fails. -->

### Progress envelopes are rebuilt from mutable state
**Location:** packages/plugins/api-openai/index.ts:488, tests/stream-timeouts.test.ts:561
**Severity:** nit
<!-- codex: outputIndex === 0 does not guarantee an unchanged accumulator: a permitted usage event before the first output changes a.usage, so subsequent progress envelopes contain different usage and router_usage_confidence fields from response.created, contrary to the plan’s identical-envelope promise; the partial toMatchObject assertion misses this. Capture and reuse the initial envelope, and test full equality after an early usage event followed by continued upstream activity. -->

### Bound and clean up the cancellation test’s read loop
**Location:** tests/stream-timeouts.test.ts:725
**Severity:** important
<!-- codex: The loop waiting for progress ignores reader.read().done: an early EOF produces an endless chain of immediately resolved reads that can starve timeout timers, while a stream that merely never produces progress leaves the client open because client.abort() is reached only on success. Check EOF, bound both the read and upstream-abort waits, and register unconditional client cancellation in cleanup before starting fetch so failures cannot strand the connection and upstream. -->

## Diff stat

```text
 README.md                            |   4 +-
 docs/implementation-status.md        |   2 +-
 packages/contracts/index.ts          |  12 +-
 packages/modules/inference/index.ts  |  21 ++-
 packages/plugins/api-openai/index.ts |  43 ++++--
 tests/codex-chat-routes.test.ts      |  29 +++-
 tests/stream-timeouts.test.ts        | 250 ++++++++++++++++++++++++++++++++++-
 7 files changed, 342 insertions(+), 19 deletions(-)
```