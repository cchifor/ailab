# llm-router: a real Responses event while the upstream thinks (progress events)

## Codex Review

- The pre-output scope resolves the snapshot and payload-size objections: `outputIndex` covers text, refusal and tools, while empty endings still depend on lifecycle gates.
- Activity tracking still needs collision-free marks across attempts and cleanup that covers opening errors and cancellation.
- Synchronous timer writes remove the promise race, but backpressure must also suppress comments; submission and drain ordering need direct tests.
- D1 needs whole-request semantics and explicit treatment of uncapped direct-model paths. Tests need bounded gates, clock control and coverage of every first-item and ending type.
- LiteLLM concern dropped: ailab's manifests route directly to backends (`kubernetes/apps/apps/ai/litellm.yaml:349,878,886`, `litellm-local.yaml:73,83,131`); the custom ChatGPT provider also calls ChatGPT directly (`chatgpt_chat.py:4,17`). Both disputed comments are removed.

## Context

Codex agents on the local models (`qwen-27b` → cloud2 vLLM, `flash-next` → cloud3 llama.cpp) die with
`stream disconnected before completion: idle timeout waiting for SSE` after 8 reconnects whenever one turn thinks
for more than 300 s. Diagnosed on 2026-11-02 from the router journal, the router image's code, cloud2's vLLM metrics
and the real CLI; nothing below needs re-deriving.

- **The router drops reasoning.** `packages/adapters/providers/chatcompletions.ts` `runSse` yields only
  `delta.content`, `delta.refusal` and `delta.tool_calls`. vLLM/llama.cpp reasoning deltas are read and reset the
  engine's `Watchdog` through `onActivity` (`packages/modules/inference/index.ts:587`), but nothing reaches the client.
- **The client sees only comments.** `packages/plugins/api-openai/index.ts:437-444` sends `: ok`, then
  `response.created`, then `: keepalive` SSE comments after every 5 s quiet period (`streamKeepalive`). The timer runs
  for the whole stream, before and after the first output, until the handler's `finally` (`:513`).
- **Codex counts events, not bytes.** Its `stream_idle_timeout_ms` (default 300 000; the dev_worker role does not set
  it) is reset only by parsed SSE events. Measured in docker on 2026-11-02 with a fake Responses server, idle timeout
  6 s and a 15 s silent phase:

  | Codex CLI | During the silent phase | Result |
  |---|---|---|
  | 0.162.1 (the workers' version) | `: keepalive` comments every 2 s | cut at 6.0 s, one reconnect, exit 1 "idle timeout waiting for SSE" |
  | 0.162.1 | `response.in_progress` events every 2 s | completed, exit 0, one request |
  | 0.166.0 | comments | same failure |
  | 0.166.0 | `response.in_progress` events | completed, exit 0 |

- **Reproduced through the router itself** (docker, `main` `6e8104a` in-process, a fake vLLM-style server streaming
  only `reasoning_content` for 45 s, Codex `stream_idle_timeout_ms = 20000`, `stream_max_retries = 1`): Codex 0.162.1
  and 0.166.0 each exit 1 with "idle timeout waiting for SSE"; the router closes each upstream request at 20.0 s and the
  retry repeats the whole thinking phase.
- **Production evidence.** 2026-11-02 09:29-10:15Z, conversation `ba741bcd` (dev-worker-2's key): 9 identical
  requests, each `CLIENT_CLOSED` at 300.0 s with no `stream.first_content`, router utilization 0.25, while cloud2's
  vLLM had 2 requests running, 0 waiting, 0 preemptions and KV cache at most 44 %. 10 such kills on 11-01, 9 on
  11-02 on cloud2, 1 on cloud3 (flash-next, 11:16Z). The sibling run's turns reached first content at 218-234 s
  (18-22k reasoning tokens), so the 300 s cliff is routinely close. Each retry repeats the whole thinking phase.
- **Baseline of the suite on `main` in docker (node 24):** typecheck and build clean; 1972 passed, 14 skipped, 1 failed
  (`tests/image-memory.test.ts`, an RSS ceiling that fails only under full-suite load and passes alone);
  `check:architecture` reports only the known #79 line.

## Approach

Send a real, standard Responses event, `response.in_progress`, on a Responses stream **before its first output item,
while the upstream is sending bytes**, so a client's idle timer tracks upstream liveness during the thinking phase.

1. **The engine exposes the running attempt's upstream activity.** `Execution` gains an optional
   `upstreamActivity?: () => number | undefined`.
   - **Meaning:** a count of the raw upstream body chunks received so far by the attempt that is running now. It only
     grows within an attempt and is `undefined` before any attempt starts, while the attempt has received nothing,
     and once the attempt has ended (completed, failed, timed out, cancelled) until the next one receives a chunk.
   - **Ownership:** each attempt's `onActivity` closure carries its attempt id. A callback from an attempt that is no
     longer the running one (a failover's abandoned provider calling late, after `closeGrace`) is ignored. The attempt
     clears its state in the same `finally` that stops its watchdog.
   <!-- codex: round-2: The watchdog's inner finally starts after provider.open/[Symbol.asyncIterator], so an opening failure can bypass the proposed cleanup (packages/modules/inference/index.ts:587-590,634-644). Invalidate before awaited close/failure recording on every exit, with an outer cleanup fallback and guards for aborted attemptSignal/gone, not only a matching attempt id (packages/modules/inference/index.ts:603,683-705). -->
   - **No clocks:** the transport compares the value with the one it saw at its last event (inequality only), so the
     engine's injectable clock and `Date.now()` are never compared.
   <!-- codex: round-2: A per-attempt count can equal the previous attempt's last emitted mark: attempt 1 emits at count 1, attempt 2 receives one chunk and also returns 1, so inequality suppresses genuine new activity; the transport need not observe the intervening undefined state (packages/modules/inference/index.ts:536,587,698). Use an execution-wide increasing activity sequence hidden while inactive, or compare attempt identity plus count, and test equal counts across failover. -->
   - **Availability:** the getter is on the `Execution` that `execute()` returns, before the generator first runs; it
     reads `undefined` until an attempt receives bytes.
   - **What counts:** body chunks only (`readSseEvents`/`readJson` call `onActivity` after `reader.read()`), never
     the response headers. A chunk may be an upstream comment, a ping or a partial frame: this is transport activity,
     the same signal that keeps the router's own wait/idle timers alive (`contracts/index.ts:260`). An upstream that
     sends its own keepalives while stalled therefore keeps progress events going, exactly as it keeps the router's
     timers going today. Documented as such.
   - **Consumers:** additive and optional. `Execution` is built in `RouterEngine.execute` and consumed directly by
     api-openai, `agent-llm/index.ts:95`, `admin-bridge/index.ts:118`, `agent-image/index.ts:75` and
     `router-engine/tools.ts:27` (chat-api reaches it through the router agent). None of them change. cchifor/platform
     vendors the module and compiles against the type; an optional property does not break it.
   - **Contract change:** `packages/contracts/index.ts` is a contract file (AGENTS.md: ask first). The owner asked on
     2026-11-02 for this fix to be implemented in llm-router; the PR names the change explicitly.
2. **api-openai writes progress events from the keepalive timer, synchronously.** The timer stays the single place
   that fills a quiet stream. When it fires on a quiet stream it writes a progress event instead of a comment only if
   ALL of these hold:
   - Responses protocol, non-native route (checked in the tick itself: every streaming branch shares it);
   - `response.created` has been written, and **no output item has been added yet** (`outputIndex === 0`): the
     initial release covers the thinking phase before the first output only (see 3);
   - no terminal state: not `routerTerminal`, not `terminal`, not `closed`, neither `controller.signal` nor
     `live.signal` aborted, the whole-request deadline (`live.deadlineAt`) not passed;
   - the socket can take it: not `writableEnded`, not `destroyed`, not `writableNeedDrain`, and the main loop is not
     awaiting drain (`draining`);
   - no event (comments excluded) for `streamProgress.intervalMs` (15 s; exported and settable for tests like
     `streamKeepalive`);
   - `upstreamActivity()` is defined and differs from the value recorded at the last event.

   The frame is built and written in the same synchronous step: `sequence_number` is allocated at the write, and the
   write goes through the same low-level `write` the comments use (no promise, so nothing can reject outside the
   handler's `catch`). Every event write, from the timer or the main loop, records `lastEventAt` and the activity mark
   at the moment the frame is submitted, whether `write` returns true or false; comments update only `lastWrite`.
   Backpressure: the timer writes at most one frame and never while `writableNeedDrain` is set, so a slow reader can
   hold at most one progress frame above the high-water mark; the main loop's own `sendLine` then awaits drain as it
   does today. A client that disconnects is noticed by the main loop's next write (as today); the timer never reports
   errors.
   <!-- codex: round-2: After a timer write(false), draining remains false, so falling back to today's comment branch still appends keepalives while writableNeedDrain is true; sendLine also submits its next frame before awaiting drain (packages/plugins/api-openai/index.ts:442,451). Apply the socket/backpressure/abort no-write guards to both timer branches, and document the bounded extra main-loop frame; a synchronous progress write alone does not coordinate all writes. -->
   - One burst of activity followed by a stall authorizes at most one progress event: the mark then equals the value
     until new bytes arrive.
3. **Payload and scope.** `{type: 'response.in_progress', sequence_number, response}` where `response` is the same
   envelope `response.created` carried: status `in_progress`, `output: []`, the same `id`, `model` and `created_at`.
   - **Pre-output only.** Once the first output item is added, the timer goes back to comments for the rest of the
     stream. This covers the reported failure (the thinking phase before the first token of visible output) and keeps
     every progress envelope tiny and exactly equal to what a snapshot-replacing client (the OpenAI Node helper)
     already holds, so no later delta can be applied to a stale snapshot. Silence after output began (interleaved
     reasoning) is not covered by this release; it is not what was observed.
   - **One `created_at` per stream.** `responseObject` computes `created_at` from `Date.now()` on every call
     (`api-openai/index.ts:244`), so `response.created` and `response.completed` already disagree. The stream captures
     it once and every envelope it writes (created, in_progress, completed/incomplete, failed) carries it.
4. **Unchanged wire behavior:** Chat Completions streams (comments only, as today; a byte-oriented read timeout is
   satisfied by them, and no client of that protocol was reported failing), native Codex routes (the upstream's own
   events), non-streaming requests, the journal and metrics (no event per heartbeat), the engine's own timers, and
   `stream.first_content` and `emitted` (progress never enters the engine's event stream, so failover rules and the
   first-content record stay exactly as they are).
5. **A silent upstream stays silent.** No upstream body bytes since the last event (a queue, a prefill, a cold model
   load, a dead server) means no progress event. The client's idle timer and the router's wait timer bound it, as
   today.

## Owner decision before the roll (D1): a wall-clock cap on the local routes

Today a turn that thinks past 300 s is cut by Codex and retried from scratch: up to 9 × 300 s of slot time per stuck
turn, then the run dies. With progress events, the turn runs until the model finishes. Nothing else ends an active
stream: the wait/idle timers restart on activity (`inference/index.ts:113`), `maxDurationMs` has no default
(`routing/index.ts:25`), the Responses transport passes no duration cap (`api-openai/index.ts:413`), and the live
routes `qwen3.8-27b` (timeoutMs 300000, idleTimeoutMs 120000, as read from the 11-02 journal snapshot) and
`qwen3.8-flash-next` (timeoutMs 600000, runbook rev 2183) set none. The local chat adapter's aggregate stream limits
(16 MiB, 200 000 SSE events: `sse.ts:29`) end a stream at roughly 70k streamed tokens (vLLM sends ~220-330 bytes per
token event), which is a data bound, not a time bound: about 10 minutes on cloud2, 20+ on cloud3.
The same gap let flash-next's runaway tool-call argument on 11-02 run 9 minutes until a human stopped it (journal
`42cbc15f`).

- **Proposal:** `maxDurationMs` on the resolved local routes, read back live (redacted) before the roll. `qwen3.8-27b`:
  1 800 000 (30 min; its longest observed turn is ~4 min). `qwen3.8-flash-next`: 900 000 (15 min). The release
  records the config revision; a rollback of the image keeps the caps (they are valid on the old image and bound the
  same gap there).
  <!-- codex: round-2: maxDurationMs covers cold load, all attempts, visible generation and downstream drain, so it can cut a valid response after output begins (packages/modules/inference/index.ts:390-416; packages/plugins/api-openai/index.ts:451-457). D1 should approve whole-request budgets and record that these policy settings also affect Chat Completions and non-streaming requests on the same routes. -->
- **The owner decides** before the roll PR: the caps as proposed, other values, or no cap with the gap accepted.
  <!-- codex: round-2: These caps do not cover virtual local/... policies or requests entering through another policy's fallback chain, although those streams also receive progress (packages/modules/routing/index.ts:69-87; packages/modules/inference/index.ts:389-391); direct local/qwen3.8-27b access is explicitly enabled in wt-progress-events/docs/runbooks/llm-router.md:2871. Record acceptance or treatment of those uncapped paths, and qualify the Risks claim that D1 supplies a bound by both the selected policy and the owner's decision to set a cap. -->

## Out of scope (follow-ups, named so they are not lost)

- Forwarding the reasoning itself (Responses reasoning items, `reasoning_content` on chat). It would make an agent's
  thinking visible, but adds output items that clients store and send back as input, which the subset parser refuses
  for non-Codex callers. Separate design.
- Progress events after the first output item (interleaved reasoning): needs the accurate-snapshot work codex
  described (stable item identities, partial tool arguments, payload bounds) and a slow-reader test. Not observed.
- cloud3 cold loads: llama-swap sends no bytes while it loads a model, so a Codex client still gives up at 300 s.
  The resolved route `qwen3.8-flash-next` waits up to its `timeoutMs` 600 000 (named routes ignore
  `Account.timeouts`, `contracts/index.ts:85`). The fix is the dev_worker profile's `stream_idle_timeout_ms` (ailab).
- The 429s on cloud2 (no router queue, 4 slots) and the edge-proxy roll that orphaned upstream legs: separate.
- Bounding how long the model thinks (the router drops `reasoning_effort` for local servers; cloud2's template
  defaults to xhigh): a serving decision for the owner.

## Critical files

llm-router (branch `fix/responses-progress-events` off `main` `6e8104a`):
- `packages/contracts/index.ts` - `Execution.upstreamActivity` (optional, documented).
- `packages/modules/inference/index.ts` - per-attempt activity counter owned by the attempt id; exposed on `Execution`.
- `packages/plugins/api-openai/index.ts` - `streamProgress`, the tick's decision, the synchronous progress frame, the
  per-stream `created_at`, `lastEventAt`/activity mark recorded at frame submission.
- `tests/stream-timeouts.test.ts` - writer/engine cases with the scripted mock provider (`server` helper,
  deterministic `onActivity` calls) and the adapter path through stubbed fetch (`sseBody`), which is what produces
  activity from reasoning-only bytes.
- `tests/codex-chat-routes.test.ts` - the Codex 0.166 request fixture against the fake llama.cpp server streaming
  reasoning first.
- `README.md:341-351` (timeout semantics and SSE comments), `docs/implementation-status.md` (the streaming line):
  progress events, their gate, and that they are not engine output, first content or upstream activity.

ailab (after the image exists): `kubernetes/apps/apps/llm-router/router.yaml` (digest, source-commit, release
annotations) and a `docs/runbooks/llm-router.md` release section with rollback and D1's outcome.

## Verification

1. **Failing tests first** (fail on `main`, pass on the branch). Comparisons normalize ids, `created_at` and
   `sequence_number`; the interval objects are restored explicitly in `cleanup` (restoreAllMocks does not undo plain
   assignments).
   - **Delivery while the upstream is held:** a real listening server and a streaming `fetch` reader; the upstream
     emits reasoning chunks and waits on a gate the test opens only after the client has read at least 2
     `response.in_progress` events. Then text; `response.completed` follows.
     <!-- codex: round-2: If the upstream stops emitting when it waits on this gate, one burst can authorize at most one progress event, so waiting for two deadlocks. Keep activity running while gated, impose a bounded reader timeout, and release/abort in finally so the expected failure on main cannot leave app.close waiting on the held request (tests/stream-timeouts.test.ts:245-250,343-357). -->
   - Reasoning-only phase then text (adapter path): ≥ 3 progress events between `response.created` and the first
     `response.output_item.added`; `sequence_number`s strictly increasing; the normalized output items and terminal
     event equal those of the same reply without the quiet phase.
   - Silent upstream (no body bytes; headers only) then text: zero progress events, comments only.
   - Upstream comments only (`: ping` every 50 ms): progress events (documented transport-activity semantics).
   - Fragmented reasoning frames (one SSE event split across chunks): progress events.
   - One burst then a stall: exactly one progress event, then comments only.
   - Text streaming continuously: no progress events (visible events suppress them).
   - After the first output item: a quiet phase with activity gets comments only.
     <!-- codex: round-2: Exercise this with refusal and tool as the first item, including a tool announcement with empty arguments, then further deltas: both increment outputIndex before the awaited item write, just like text (packages/plugins/api-openai/index.ts:475-481). Also cover empty-output length/content_filter endings and include refusal/incomplete cases in the SDK gate: outputIndex remains zero for those endings, so invalidation and terminal gates must supply the protection (packages/plugins/api-openai/index.ts:241-244,489-491). -->
   - Chat Completions with the reasoning upstream: no new chunks.
   - Native route: no injected events (existing native passthrough tests stay green).
   - An `Execution` without `upstreamActivity` (a stub engine): comments only, no error.
   - Two concurrent requests, one active and one silent upstream: only the active one gets progress events.
   - Failover: attempt 1 active (a progress event sent), fails; attempt 2 silent: no further progress events; a late
     `onActivity` from attempt 1 after `closeGrace` and attempt 2's start is ignored; failover still happens and
     `stream.first_content` is recorded only for attempt 2's real content.
   - Terminal boundaries: a router timeout (`response.failed`), an incomplete ending, a client close and a SHUTDOWN
     during a reasoning phase: no progress event after the terminal frame or after the abort.
   - Total cap: continuous reasoning-only activity with `maxDurationMs` set: `response.failed` with TIMEOUT
     (`max_duration`), the upstream closed, admission released, no later progress event.
   - Engine unit: `upstreamActivity()` is `undefined` before an attempt, grows with that attempt's `onActivity`,
     resets to `undefined` at the attempt's end, ignores a stale attempt's callback.
   - The same `created_at` on `response.created`, progress and terminal envelopes.
     <!-- codex: round-2: Force this test across an epoch-second boundary, otherwise the old per-call Date.now implementation can pass (packages/plugins/api-openai/index.ts:244). The counter removes cross-clock comparisons but transport quiet/deadline checks still use Date.now; tests/stream-timeouts.test.ts:55 fakes only timers, so deterministic writer tests must advance Date consistently too. -->
   <!-- codex: round-2: Add controlled write(false) coverage for response.created and for a timer progress frame, with activity arriving during drain, subsequent output, and close/deadline termination. The timer is armed before response.created and that event can suspend in sendLine, so assert created-before-progress ordering, submission-time activity marks, no timer writes while blocked, and no duplicate/reordered sequences (packages/plugins/api-openai/index.ts:440-466); the existing large-output drain tests do not exercise this path (tests/stream-timeouts.test.ts:258,277). -->
2. **CI gates in docker** (node:24, pnpm 10.32.1): a copy of the read-only mounted checkout, `pnpm install
   --frozen-lockfile` with a store volume, then `pnpm typecheck`, `pnpm test`, `pnpm build`, `pnpm build:lib`,
   `pnpm test:production`, `pnpm check:architecture`. Compared to the `main` baseline above. PostgreSQL auth
   integration tests are reported as skipped when PostgreSQL is absent.
3. **The real CLI end to end** (docker): the branch router in-process on a port (the `createRuntime` +
   `createHttpServer` setup of `tests/codex-chat-routes.test.ts`), a local account on a fake vLLM-style server, Codex
   0.162.1 and 0.166.0 with `stream_idle_timeout_ms = 20000`, `stream_max_retries = 1`, `request_max_retries = 0`:
   - text-only: 45 s of reasoning, then text. `main`: exit 1 after 2 upstream requests (reproduced above). Branch:
     exit 0, exactly 1 upstream request.
   - tool, then text: turn 1 reasons 45 s then calls `exec_command`; turn 2 reasons 45 s then answers. Branch: exit 0,
     exactly 2 upstream requests, no replay.
4. **SDK stream helpers (release gate)** in docker against the same branch router, pinned versions recorded: OpenAI
   Node `client.responses.stream(...).finalResponse()` and Python `client.responses.stream(...)` +
   `get_final_response()`, for a reasoning-then-text reply, a reasoning-then-tool-call reply, and a router timeout
   (`response.failed`): final text, parsed arguments, usage and the failure surface, compared with `main` to separate
   existing subset differences. If a helper breaks on the progress envelope, restrict progress events to Codex callers
   (`codexCaller`, `api-openai/index.ts:401`) before release.
5. **Live acceptance after the roll**, both backends:
   - On a dev worker, `codex exec -p qwen-27b -c model_providers.llm-router.stream_idle_timeout_ms=60000` on a task
     that makes a turn think longer than 60 s; the same with `-p flash-next`. For each acceptance turn: the router
     request id from the client log, the journal's `stream.first_content` later than 60 s, no second request for that
     turn, and the run completes.
   - A deliberately cancelled long-thinking request (Ctrl-C equivalent): `client.closed`, upstream closed, admission
     released.
   - `validate-live`: 6/9, the known baseline (`codex text`, `codex stream`, `codex tool call` send a bare
     `model: 'codex'` and get 400 `MODEL_REQUIRED`), and the public checks.

## Release

The standard image flow (llm-router PR → reviewer bots merge → archive in a volume-free pod → Relay v0.2.0 assets →
Relay `deploy-images` dispatch → anonymous pull check → backup + `validate-live` baseline → D1 → ailab roll PR).
Rollback: the previous digest `sha256:f3390a7c7e13e9ff79e3ddced468dc173935d79f63e0cfae48c061db770df5be` (source
`6e8104a4`, `router-0.1.0-20261101-reply-shape`). No schema or secret change. If D1 sets caps, they are a recorded
config revision that the rollback deliberately keeps.

## Risks

- **A Responses client that mishandles repeated `response.in_progress`.** The event is in OpenAI's streaming
  reference, which defines the envelope but promises neither "once" nor periodic repeats; repeat handling is a
  per-client, per-version claim. Measured: Codex 0.162.1 and 0.166.0. Gated: the OpenAI Node and Python helpers
  (verification step 4), with the Codex-only fallback.
- **Transport activity, not model progress.** An upstream that sends its own keepalives while stalled keeps progress
  events going, as it keeps the router's timers going today. The bound is D1's cap.
- **Hiding a stuck upstream.** See D1: without a cap, a degenerate reasoning loop runs until the model stops or the
  adapter's 16 MiB / 200 000-event limits end the stream.

<!-- codex-review-status: complete -->