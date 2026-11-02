# Implementation review — router-reasoning-forwarding — round 1

<!-- codex-impl-review-status: complete -->


## Findings

### Interrupted reasoning closure lacks regression coverage
**Location:** tests/reasoning-forwarding-http.test.ts:230  
**Severity:** important  
**Resolved (98f0f7f).** New tests: a client leaving while the first close frame waits for a drain that never comes (the upstream is cancelled), a shutdown after the early close while the answer is unfinished (the connection is cut, no terminal event, the item closed once), and maxDurationMs during endless reasoning (response.failed, never closed). The close never sets `routerTerminal`, so a shutdown cuts it like any non-terminal event.

### Reasoning-specific timeout and progress transitions are untested
**Location:** tests/reasoning-forwarding.test.ts:166; tests/reasoning-forwarding-http.test.ts:21  
**Severity:** important  
**Resolved (98f0f7f).** Engine: reasoning then silence ends the attempt with TIMEOUT idle; reasoning then pings every 60 s stays alive (fake timers). HTTP: progress events before the reasoning item, none after, with upstream bytes continuing; maxDurationMs during continued reasoning.

### Reasoning size accounting has no boundary test
**Location:** packages/plugins/api-openai/index.ts:530  
**Severity:** important  
**Resolved (98f0f7f).** 9M code units of non-ASCII reasoning end both a Responses stream and a chat stream with RESPONSE_TOO_LARGE; without `a.reasoning.length` in the guard the test would pass the reasoning through.

### Two tests do not establish their advertised event boundaries
**Location:** tests/reasoning-forwarding-http.test.ts:108; tests/reasoning-forwarding.test.ts:154  
**Severity:** important  
**Resolved (98f0f7f).** A mock provider now yields reasoning → usage → reasoning → text (one item, closed once); stream.first_content is asserted absent while only reasoning has come and present once after visible output.

### Consumer and capture safeguards lack the planned tests
**Location:** packages/plugins/admin-bridge/index.ts:123; packages/modules/inference/index.ts:641  
**Severity:** important  
**Partly resolved (98f0f7f):** capture keeps the text only and never the reasoning (test with a marker); Activity's emitted-dependent line and the reasoning chars have a test.
<!-- codex: Add a bridge completion containing more than 2 MiB of discarded reasoning plus a small answer. -->
<!-- opus-pushback: Reasoning cannot reach the bridge in production: its only inference action, `complete`, serves bridge-provisioned accounts, which TrueSwarm creates from Codex/Claude subscription logins (admin-bridge/index.ts:103-113), and those adapters never yield reasoning; `forwardReasoning` is read by the chat-completions adapter only. The one-line skip is defensive; a test would need an account shape that cannot occur. -->

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