# Implementation review — router-reasoning-forwarding — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- No blocker established by inspection, but the plan’s verification requirements are not fully implemented.
- The supplied Codex and SDK results establish compatibility for their exercised paths; they do not establish shutdown, interrupted drain, timeout/progress transitions, or size-boundary behavior.
- Replay validation accepts shapes outside the finalized allowlist, and documentation understates the failover implications for consumers that discard or buffer reasoning.
- Review remained read-only. Existing suites and supplied scenarios were not rerun; dependencies are absent. An isolated, in-memory parser check confirmed the validation discrepancy, and `git diff --check` passed.

## Findings

### Interrupted reasoning closure lacks regression coverage
**Location:** tests/reasoning-forwarding-http.test.ts:230  
**Severity:** important  
<!-- codex: The backpressure test delays only the final output_item.done frame and eventually succeeds; it cannot establish behavior when either earlier close frame blocks, or when SHUTDOWN, client cancellation, or maxDurationMs interrupts closure. Add controlled interruptions at each close frame, including shutdown after an early close while the answer remains unfinished, and assert that shutdown cuts the connection without a response terminal or premature routerTerminal state. -->

### Reasoning-specific timeout and progress transitions are untested
**Location:** tests/reasoning-forwarding.test.ts:166; tests/reasoning-forwarding-http.test.ts:21  
**Severity:** important  
<!-- codex: The failover test throws immediately after reasoning, and the HTTP fixture writes its reasoning frames immediately; neither exercises reasoning followed by silence, ping-only activity, or the transition out of #131 progress events. Add tests with different wait/idle deadlines, reasoning followed by silence and by pings, maxDurationMs during continued activity, and progress before—but never after—the reasoning item opens; continuous-delta Codex success does not prove these cases. -->

### Reasoning size accounting has no boundary test
**Location:** packages/plugins/api-openai/index.ts:530  
**Severity:** important  
<!-- codex: No added test reaches the response budget, so removing a.reasoning.length from this guard would leave the new tests green, including the accounting requirement for reasoning suppressed after visible Responses output. Add below-limit, exact-limit and over-limit cases using non-ASCII reasoning on both protocols, cover the non-streaming check, and verify failure framing and upstream cleanup when the limit is exceeded. -->

### Two tests do not establish their advertised event boundaries
**Location:** tests/reasoning-forwarding-http.test.ts:108; tests/reasoning-forwarding.test.ts:154  
**Severity:** important  
<!-- codex: The “usage between reasoning deltas” fixture never delivers an intervening usage InferenceEvent because runSse stores usage until its final tail, while the first_content test merely counts one event and would pass if the first reasoning event incorrectly triggered it. Inject an event source that actually yields reasoning → usage → reasoning, and pause execution or control the clock to assert that first_content is absent during reasoning and appears only with visible output. -->

### Consumer and capture safeguards lack the planned tests
**Location:** packages/plugins/admin-bridge/index.ts:123; packages/modules/inference/index.ts:641  
**Severity:** important  
<!-- codex: The new tests neither enable optional capture nor exercise reasoning through admin-bridge, Activity or explain_request, leaving the planned privacy and consumer safeguards unverified. Add a capture test with a distinct reasoning marker, a bridge completion containing more than 2 MiB of discarded reasoning plus a small answer, and consumer assertions for reasoningChars and the emitted-dependent retry explanation. -->

### Replay validation exceeds the finalized allowlist
**Location:** packages/modules/protocol/index.ts:46  
**Severity:** nit  
<!-- codex: replayedReasoning accepts content: null and any string status, although the finalized plan permits content only as an optional array and does not allow status; the isolated parser check accepted both content: null and status: "not-a-valid-status". Remove these allowances and add negative tests, or explicitly reconcile the plan and documented accepted shape with a justified compatibility requirement. -->

### Failover documentation omits consumers that receive no reasoning
**Location:** README.md:351; packages/contracts/index.ts:65  
**Severity:** important  
<!-- codex: “Once reasoning has reached the client” understates the actual boundary: inference sets emitted before yielding reasoning, so non-streaming requests and internal consumers that discard it also lose failover without delivering that reasoning to their caller. Document adapter emission as the boundary, including the switch to the idle timer and the accepted effects on the Assistant, admin-bridge and test_route, as the finalized plan requires. -->

### Unknown emitted state is treated as proof that nothing was sent
**Location:** apps/web/pages/Activity.tsx:132  
**Severity:** nit  
<!-- codex: d.emitted !== true still displays “nothing was sent” when emitted is absent or otherwise unknown, whereas the plan requires emitted to be false. Use d.emitted === false and cover true, false and missing values so historical entries do not receive an unsupported retry assurance. -->

## Diff stat

```text
 README.md                                      |   1 +
 apps/web/pages/Activity.tsx                    |   5 +-
 docs/implementation-status.md                  |   2 +-
 packages/adapters/providers/chatcompletions.ts |  21 ++-
 packages/contracts/index.ts                    |   9 +
 packages/modules/inference/index.ts            |   5 +-
 packages/modules/protocol/index.ts             |  28 ++-
 packages/plugins/admin-bridge/index.ts         |   2 +
 packages/plugins/api-openai/index.ts           |  33 +++-
 packages/plugins/trajectory-recorder/tools.ts  |   5 +-
 packages/runtime/config.ts                     |   1 +
 scripts/validate-shared-routes.mjs             |   3 +-
 tests/reasoning-forwarding-http.test.ts        | 252 +++++++++++++++++++++++++
 tests/reasoning-forwarding.test.ts             | 188 ++++++++++++++++++
 14 files changed, 535 insertions(+), 20 deletions(-)
```