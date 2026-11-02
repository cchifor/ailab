# Implementation review — router-reasoning-forwarding — round 2

<!-- codex-impl-review-status: finalized -->


## Findings

### Interrupted reasoning closure lacks regression coverage
**Location:** tests/reasoning-forwarding-http.test.ts:230  
**Severity:** important  
**Resolved (98f0f7f).** New tests: a client leaving while the first close frame waits for a drain that never comes (the upstream is cancelled), a shutdown after the early close while the answer is unfinished (the connection is cut, no terminal event, the item closed once), and maxDurationMs during endless reasoning (response.failed, never closed). The close never sets `routerTerminal`, so a shutdown cuts it like any non-terminal event.

**Round 2, resolved (a1ad1d0... see the llm-router branch head):** the shutdown test asserts the cut (the body read fails rather than ending normally). Mutation check: setting `routerTerminal` in the early close makes it fail.

### Reasoning-specific timeout and progress transitions are untested
**Location:** tests/reasoning-forwarding.test.ts:166; tests/reasoning-forwarding-http.test.ts:21  
**Severity:** important  
**Resolved (98f0f7f).** Engine: reasoning then silence ends the attempt with TIMEOUT idle; reasoning then pings every 60 s stays alive (fake timers). HTTP: progress events before the reasoning item, none after, with upstream bytes continuing; maxDurationMs during continued reasoning.

### Reasoning size accounting has no boundary test
**Location:** packages/plugins/api-openai/index.ts:530  
**Severity:** important  
**Resolved (98f0f7f).** 9M code units of non-ASCII reasoning end both a Responses stream and a chat stream with RESPONSE_TOO_LARGE; without `a.reasoning.length` in the guard the test would pass the reasoning through.

**Round 2, resolved:** exactly 8 MiB of non-ASCII output (reasoning + text) completes and one more code unit fails, on Responses and chat; reasoning after visible text that would cross the limit fails a Responses stream though it is not forwarded. Mutation checks: counting bytes fails the exact case; counting only the forwarded `lead` fails the late case.

### Two tests do not establish their advertised event boundaries
**Location:** tests/reasoning-forwarding-http.test.ts:108; tests/reasoning-forwarding.test.ts:154  
**Severity:** important  
**Resolved (98f0f7f).** A mock provider now yields reasoning → usage → reasoning → text (one item, closed once); stream.first_content is asserted absent while only reasoning has come and present once after visible output.

### Consumer and capture safeguards lack the planned tests
**Location:** packages/plugins/admin-bridge/index.ts:123; packages/modules/inference/index.ts:641  
**Severity:** important  
**Resolved (98f0f7f); the admin-bridge test was withdrawn in round 2** (reasoning cannot reach the bridge: its accounts are subscription logins, whose adapters never yield reasoning). Capture keeps the text only and never the reasoning (test with a marker); Activity's emitted-dependent line and the reasoning chars have a test.

**Round 2:** The remaining bridge test request is withdrawn. The bridge provisions only Codex, Claude and Claude-seat accounts, and its delegated principal has subscription-only access. Provider validation enforces each provider’s access kind, and routing enforces the principal’s access restriction. The reachable subscription adapters do not yield the new `reasoning` event; the bridge parses Chat input, so Codex’s native Responses pass-through is not selected. The chat-completions adapters that read `forwardReasoning` require API or local access. The pushback is therefore supported for the production paths this feature changes.

### Replay validation exceeds the finalized allowlist
**Location:** packages/modules/protocol/index.ts:46  
**Severity:** nit  
**Resolved (98f0f7f), reconciled:** `status` is now null or one of in_progress/completed/incomplete (any other value refused, tested); null is kept because the OpenAI Python SDK's default dump sends `status: null`. `content: null` is kept as absent content (the same dumps).

### Failover documentation omits consumers that receive no reasoning
**Location:** README.md:351; packages/contracts/index.ts:65  
**Severity:** important  
**Resolved (98f0f7f).** README and the Account contract state the adapter's yield as the boundary: idle timer, no failover, for every caller, internal consumers that discard it included.

### Unknown emitted state is treated as proof that nothing was sent
**Location:** apps/web/pages/Activity.tsx:132  
**Severity:** nit  
**Resolved (98f0f7f).** `d.emitted === false`; true, false and missing are tested.

## Note on non-streaming

A non-streaming client request makes a non-streaming upstream call, and vLLM then returns all reasoning as one
`message.reasoning_content`: the Responses reasoning item of a non-streaming reply holds all of it (the adapter yields it
before the content). Only a streamed reply separates the leading segment from later ones; the plan's "A, text, B"
rule applies to streams.

## Diff stat

```text
 README.md                                      |   1 +
 apps/web/pages/Activity.test.tsx               |  12 +
 apps/web/pages/Activity.tsx                    |   5 +-
 docs/implementation-status.md                  |   2 +-
 packages/adapters/providers/chatcompletions.ts |  21 +-
 packages/contracts/index.ts                    |  10 +
 packages/modules/inference/index.ts            |   5 +-
 packages/modules/protocol/index.ts             |  29 +-
 packages/plugins/admin-bridge/index.ts         |   2 +
 packages/plugins/api-openai/index.ts           |  33 ++-
 packages/plugins/trajectory-recorder/tools.ts  |   5 +-
 packages/runtime/config.ts                     |   1 +
 scripts/validate-shared-routes.mjs             |   3 +-
 tests/reasoning-forwarding-http.test.ts        | 354 +++++++++++++++++++++++++
 tests/reasoning-forwarding.test.ts             | 256 ++++++++++++++++++
 15 files changed, 719 insertions(+), 20 deletions(-)
```