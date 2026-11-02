# Implementation review — router-progress-events — round 1

<!-- codex-impl-review-status: finalized -->

Plan: `plans/2026-11-02-router-progress-events-plan.md` (finalized after two codex rounds). Implementation: llm-router
branch `fix/responses-progress-events` off `main` `6e8104a`. Codex (`gpt-6-astra`, `review` profile, read-only)
reviewed `6e8104a..f40beef`; the fixes below are `8b45cd0`. Every finding was accepted; none was pushed back, so the
review converged after one round.

## Findings and resolutions

### Clear activity ownership before recording synchronous open failures
**Location:** packages/modules/inference/index.ts (attempt loop)
**Severity:** important
**Resolved (8b45cd0).** An attempt now takes ownership only once `open()` has returned its iterator, so an `open()`
that throws never owns the activity, and the attempt's `catch` clears ownership before its awaited failure recording.
The inner `finally` (before the awaited close) and the outer `finally` keep their clears. Test: "a failed attempt stops
counting before its failure is recorded", for an `open()` that throws and for a read that fails, with the
`attempt.failed` journal write held while the failed provider's saved callback runs. Mutation check: with ownership
claimed before `open()` and no clear in the `catch` (the state codex probed), the test fails with
`open: expected 3 to be undefined`.

### Ownership tests do not exercise the cleanup window
**Location:** tests/stream-timeouts.test.ts (engine block)
**Severity:** important
**Resolved (8b45cd0).** Test: "a timed-out attempt stops counting at once, while its upstream close is still pending":
the attempt hangs ignoring the abort, the 100 ms wait timer ends it, `closeGrace` is 1 s, and 200 ms into the close
window (before `attempt.failed` is recorded) the abandoned provider's callback leaves `upstreamActivity()` undefined.
Mutation check: without the inner `finally` clear it fails with `expected 3 to be undefined`.

### Broad client enablement lacks the planned SDK release evidence
**Location:** packages/plugins/api-openai/index.ts (progress gate)
**Severity:** important
**Resolved with the evidence (plan verification step 4), run in docker against the harness router in-process, `main`
`6e8104a` vs the branch `f40beef`:** OpenAI Node `openai@7.47.0` `responses.stream().finalResponse()` and Python
`openai==3.42.1` `responses.stream()` + `get_final_response()`, each for thinking → text, thinking → tool call,
thinking → no output (`finish: length`) and thinking → the route's `maxDurationMs` (`response.failed`).

| Case | Node, main and branch | Python, main and branch |
|---|---|---|
| text | `completed`, text `SDK-DONE`, same usage | same |
| tool call | `completed`, `exec_command` `{"cmd":"echo sdk"}` | same |
| no output | `incomplete`, `output: []` | `RuntimeError: Didn't receive a response.completed event` |
| cap | `failed`, `output: []` | the same `RuntimeError` |

The branch adds 5-7 `response.in_progress` events per case and changes no outcome. The Python helper's error on a
non-`completed` ending is its own behavior, identical on `main`. The Codex-only fallback is therefore not applied.

### Progress envelopes are rebuilt from mutable state
**Location:** packages/plugins/api-openai/index.ts (progress frame)
**Severity:** nit
**Resolved (8b45cd0).** The envelope `response.created` carried is captured once and every progress event repeats it.
Test: "every progress envelope is exactly the response.created envelope, even after a usage event before the first
output" (whole-object equality). Mutation check: rebuilding it from the accumulator fails the test.

### Bound and clean up the cancellation test's read loop
**Location:** tests/stream-timeouts.test.ts (HTTP block)
**Severity:** important
**Resolved (8b45cd0).** The cancellation test registers the client's abort in `cleanup` before `fetch`, stops at the
end of the stream, bounds the read loop and the upstream-abort wait (5 s each) and asserts both. The held-delivery
test's loop also stops at the end of the stream.

## Verification of the final branch

- The real Codex CLI end to end (plan step 3, branch `d3a016d`, unchanged since in the paths it exercises): 0.162.1 and
  0.166.0 through the router in-process, 45 s of reasoning-only output per turn, `stream_idle_timeout_ms` 20 000: text
  exit 0 with 1 upstream request; tool-then-answer exit 0 with 2. On `main`: exit 1 after 2 requests, "idle timeout
  waiting for SSE".
- CI gates in docker (node 24) on `d3a016d`: typecheck, build, build:lib, test:production clean; 1985 passed, 0 failed;
  `check:architecture` only the known #79 line, as on `main`. The final head is re-run before the PR.
- Mutation checks: the backpressure guards (18 timer writes in the needs-drain window without them), and the three
  above.

## Owner decision D1 (2026-11-02)

**No caps; the gap is accepted.** The local routes `qwen3.8-27b` and `qwen3.8-flash-next` keep no `maxDurationMs`, and
direct `local/<model>` requests stay uncapped as they are. A degenerate thinking loop runs until the model stops or the
adapter's 16 MiB / 200 000-event stream limits end it (about 70k streamed tokens). The release records this decision;
no configuration changes with it, so the rollback is the image digest alone.

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
(as reviewed, `6e8104a..f40beef`)
