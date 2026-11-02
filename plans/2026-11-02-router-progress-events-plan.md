# llm-router: a real Responses event while the upstream thinks (progress events)


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
   - **Meaning:** an execution-wide count of raw upstream body chunks that only grows (it never restarts with a new
     attempt), visible only while the running attempt has received a chunk: `undefined` before any attempt starts,
     while the running attempt has received nothing, and once its read has ended (completed, failed, timed out,
     cancelled) until the next attempt receives one. Because the count never restarts, a new attempt's first chunk can
     never equal a mark the transport took during an earlier attempt.
   - **Ownership:** each attempt's `onActivity` closure carries its attempt id; a callback from an attempt that is no
     longer the running one (a failover's abandoned provider calling late, after `closeGrace`) does not count, though
     it still reaches its own, stopped watchdog as today. The attempt clears its state in the inner `finally` that
     stops its watchdog (before the awaited close and the failure recording), and again in the attempt's outer
     `finally`, which every exit reaches, an `open()` that throws before its read begins included.
   - **No clocks:** the transport compares the value with the one it saw at its last event (inequality only), so the
     engine's injectable clock and `Date.now()` are never compared.
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
   Backpressure: the tick writes nothing at all, comment or progress, while `writableNeedDrain` is set or the main
   loop is `draining` (after the timer's own write returned false nobody else awaits that drain, so the comment branch
   needs the guard too). The timer therefore adds at most one frame above the high-water mark, and the main loop's
   `sendLine` submits at most one more frame before it awaits the drain, as it does today. A client that disconnects is
   noticed by the main loop's next write (as today); the timer never reports errors.
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
- **What a cap is:** a whole-request budget, not a thinking limit. It counts a cold load, every attempt, the visible
  output and the time the client takes to read it, so it can end a valid response after output began (phase
  `max_duration`). It applies to every protocol on those routes: Chat Completions and non-streaming requests too.
- **What a cap does not cover:** requests that name a model directly (`local/qwen3.8-27b`, enabled since rev 2198) run
  on a virtual direct-model policy built from `defaults.timeouts`, which has no total cap, and a request that reaches
  these deployments through another route's fallback chain takes that route's settings. Those paths get progress
  events too and stay uncapped unless the owner also caps them; the release records which were accepted as uncapped.
- **The owner decides** before the roll PR: the caps as proposed, other values, or no cap with the gap accepted, and
  the direct-model paths above.

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

1. **Failing tests first** (fail on `main`, pass on the branch; `tests/stream-timeouts.test.ts` unless noted). The
   interval objects are restored explicitly in `cleanup` (restoreAllMocks does not undo plain assignments). The writer
   tests use real time with generous margins (activity every 10 ms, progress interval 40 ms, keepalive 10 ms), never
   fake timers: the transport's quiet and deadline checks read `Date.now()`.
   - A thinking phase then text: ≥ 3 progress events, all after `response.created` and before the first
     `response.output_item.added`; `sequence_number`s strictly increasing; each progress envelope equals the created
     one (`id`, `model`, `created_at`, `output: []`); the reply and `response.completed` intact.
   - Adapter path (`tests/codex-chat-routes.test.ts`, the captured Codex 0.166 request): the fake llama.cpp server
     streams only `reasoning_content`, each SSE frame split across two writes, then a tool call: ≥ 2 progress events
     before the item; the `function_call` item and `response.completed` exactly as without the thinking phase.
   - Silent upstream (no body bytes) then text: comments only.
   - One burst after `response.created`, then a stall: exactly one progress event. A burst before `response.created`:
     none (that event already accounts for it).
   - After the first output item, a quiet phase with activity: comments only.
   - A tool call (announced with empty arguments, then argument deltas) and a refusal as the first item: progress
     before it, none after, the item intact.
   - A reply that ends with no output (`finish: length`): progress events, then one `response.incomplete` last.
   - Chat Completions with the same thinking upstream: exactly the role, content and finish chunks, then `[DONE]`.
   - Native routes: the existing passthrough tests (`tests/codex-native.test.ts`) stay green.
   - Delivery while the upstream is held (a real socket): the client reads two progress events before the gate opens;
     the upstream keeps sending bytes while held, the reader is bounded (5 s) and the gate is released in `cleanup`.
   - Two concurrent requests, one upstream active and one silent: progress for the active one only.
   - Terminal boundaries: the whole-request cap during an endless thinking phase ends it with `response.failed`
     (`router_error_code: TIMEOUT`) as the last event; a client that leaves mid-thinking cancels the upstream.
   - Backpressure (a real socket, the response's `writableNeedDrain` shadowed and `response.created`'s write reporting
     a full buffer until a drain 150 ms later): no write at all until that drain, the first progress event only after
     it; then 200 ms of "needs drain" after the first progress event: no timer write (comment or progress) in that
     window, progress resumes after; sequence numbers strictly increasing. A mutation run with the two
     `writableNeedDrain` guards removed fails it (18 writes in the window).
   - One `created_at` on every envelope across a second boundary (a 1.1 s thinking phase).
   - Engine: `upstreamActivity()` is `undefined` before an attempt, counts its chunks (3), is `undefined` once its read
     ended; across a failover it is `undefined` for the new attempt until its own first chunk, a late callback of the
     abandoned attempt is ignored, the new attempt's first chunk reads 2 (never the earlier mark 1), and
     `stream.first_content` is recorded once, for the second attempt's real content.
   - Not written as separate tests: an `Execution` without `upstreamActivity` (the transport reads it with optional
     chaining; every `Execution` the engine returns has it) and a SHUTDOWN during a thinking phase (the progress gate
     checks `live.signal`, whose abort is how a SHUTDOWN reaches the stream; the existing shutdown tests cover the cut).
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
   `get_final_response()`, for a reasoning-then-text reply, a reasoning-then-tool-call reply, a reply that ends with no
   output (`finish: length`, `response.incomplete`) and a router timeout (`response.failed`): final text, parsed arguments, usage and the failure surface, compared with `main` to separate
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
  events going, as it keeps the router's timers going today. The bound is a D1 cap where the owner sets one.
- **Hiding a stuck upstream.** See D1: on a path without a cap (no D1 cap on its policy, or a direct-model request), a
  degenerate reasoning loop runs until the model stops or the adapter's 16 MiB / 200 000-event limits end the stream.

<!-- codex-review-status: finalized -->