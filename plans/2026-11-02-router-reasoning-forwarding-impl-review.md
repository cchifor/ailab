# Implementation review — router-reasoning-forwarding — round 2

<!-- codex-impl-review-status: complete -->

## Summary

- Two coverage findings remain incomplete: the shutdown test does not prove the connection was cut, and the size test does not establish the planned non-ASCII boundary.
- Replay validation, Activity’s emitted-state check, and failover documentation match their resolutions. In-memory checks confirmed that the replay, Activity, usage-boundary and progress assertions reject targeted regressions. The engine timing, first-content and capture assertions match their guarded branches on inspection.
- The admin-bridge test request is withdrawn after verifying the pushback against provisioning, access enforcement and the reachable adapters.
- Full test execution remains unverified: `pnpm verify` could not start because `pnpm` is unavailable, and this checkout has no `node_modules`. The isolated checks used source evaluated in memory; no repository files were changed.
- `git diff --check 08c0fc3..HEAD` passed. The architecture check failed on `Date.now()` calls in the inference module; both offending calls already exist in `08c0fc3`.

## Findings

### Interrupted reasoning closure lacks regression coverage
**Location:** tests/reasoning-forwarding-http.test.ts:230  
**Severity:** important  
**Resolved (98f0f7f).** New tests: a client leaving while the first close frame waits for a drain that never comes (the upstream is cancelled), a shutdown after the early close while the answer is unfinished (the connection is cut, no terminal event, the item closed once), and maxDurationMs during endless reasoning (response.failed, never closed). The close never sets `routerTerminal`, so a shutdown cuts it like any non-terminal event.

<!-- codex: round-2: The implementation keeps routerTerminal separate correctly, but the shutdown regression coverage remains incomplete at tests/reasoning-forwarding-http.test.ts:320. The reader loop accepts both a rejected read and normal EOF; its assertions check only one reasoning close and absence of terminal events. Replaying those assertions against the actual HTTP handler in an in-memory harness passed both unchanged and with routerTerminal=true inserted into closeReasoning. The unchanged handler cut the connection and rejected the read; the mutant ended it normally. Assert the transport interruption explicitly, for example a rejected body read or an aborted/incomplete Node response, so marking the early close terminal fails this test. The client-cancellation and max-duration cases do not establish that distinction. -->

### Reasoning-specific timeout and progress transitions are untested
**Location:** tests/reasoning-forwarding.test.ts:166; tests/reasoning-forwarding-http.test.ts:21  
**Severity:** important  
**Resolved (98f0f7f).** Engine: reasoning then silence ends the attempt with TIMEOUT idle; reasoning then pings every 60 s stays alive (fake timers). HTTP: progress events before the reasoning item, none after, with upstream bytes continuing; maxDurationMs during continued reasoning.

### Reasoning size accounting has no boundary test
**Location:** packages/plugins/api-openai/index.ts:530  
**Severity:** important  
**Resolved (98f0f7f).** 9M code units of non-ASCII reasoning end both a Responses stream and a chat stream with RESPONSE_TOO_LARGE; without `a.reasoning.length` in the guard the test would pass the reasoning through.

<!-- codex: round-2: The omission regression is now covered, but the planned boundary check remains missing at tests/reasoning-forwarding-http.test.ts:310. Nine million copies of é exceed 8 MiB under both code-unit and UTF-8-byte accounting. Evaluating the actual guard confirmed that removing the reasoning charge escapes rejection, while replacing it with Buffer.byteLength(a.reasoning) still produces the asserted rejection despite incorrectly rejecting valid smaller outputs. Add a successful reasoning-only stream at exactly 8*1024*1024 code units and rejection at one code unit over, for both protocols. Also exercise reasoning after visible output: the current leading-only fixture cannot distinguish charging a.reasoning from charging a.lead, although the plan explicitly requires discarded late Responses reasoning to count. -->

### Two tests do not establish their advertised event boundaries
**Location:** tests/reasoning-forwarding-http.test.ts:108; tests/reasoning-forwarding.test.ts:154  
**Severity:** important  
**Resolved (98f0f7f).** A mock provider now yields reasoning → usage → reasoning → text (one item, closed once); stream.first_content is asserted absent while only reasoning has come and present once after visible output.

### Consumer and capture safeguards lack the planned tests
**Location:** packages/plugins/admin-bridge/index.ts:123; packages/modules/inference/index.ts:641  
**Severity:** important  
**Partly resolved (98f0f7f):** capture keeps the text only and never the reasoning (test with a marker); Activity's emitted-dependent line and the reasoning chars have a test.

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