# llm-router: forward the upstream's reasoning (thinking) to the client

## Codex Review

- Needs revision before implementation: reasoning segment boundaries, item indexes and exactly-once done events are underspecified for interleaved output.
- The account switch changes timers, retry eligibility and internal consumers even when those consumers discard reasoning; the Assistant is not behaviorally unchanged.
- Input acceptance needs a narrow, protocol-specific definition; globally accepting replayed reasoning contradicts an unconditional “nothing changes until enabled” claim.
- Budget, progress coverage and rollback claims need correction, with explicit regression coverage for disabled accounts.
- Reviewed `main` at `08c0fc3` read-only, accepting the supplied Codex findings. No implementation, SDK runtime probes or verification commands were run.

## Context

The owner asked (2026-11-02) for the router to forward a local model's thinking, so it behaves like a pass-through
rather than hiding it. Today, on every chat-backed route (local vLLM/llama.cpp, DeepSeek, OpenAI chat accounts):

- **The adapter drops it.** `packages/adapters/providers/chatcompletions.ts` `runSse`/`runJson` read only
  `delta.content`, `delta.refusal` and `delta.tool_calls`; `reasoning_content` (vLLM, llama.cpp, DeepSeek) and
  `reasoning` (newer vLLM) are never read. There is no reasoning type in `InferenceEvent`
  (`packages/contracts/index.ts:249`), so nothing downstream could carry it anyway.
- **What it costs.** An agent's thinking is invisible: a Codex run that "goes silent for 7 minutes" cannot be told apart
  from a stall. Until router #131 (progress events, rolled 2026-11-02 as `router-0.1.0-20261102-progress-events`) it
  also killed long-thinking turns at Codex's 300 s idle timeout.
- **Native Codex routes are already a pass-through** (`type: 'native'` events forwarded as the upstream sent them).

Facts verified for this plan (do not re-derive):

- **The real Codex CLI accepts reasoning items without `encrypted_content`.** Measured in docker on 2026-11-02 with a
  fake Responses server that streams `response.output_item.added` `{type: 'reasoning', summary: [], content: []}`,
  `response.content_part.added` (`reasoning_text`), `response.reasoning_text.delta`, then the done events, then a
  function call: Codex 0.162.1 and 0.166.0 both exit 0 over two turns. With `show_raw_agent_reasoning = true` the text
  shows in `codex exec` output; with the default it is hidden but accepted.
- **Codex sends the item back next turn** as `{type: 'reasoning', id, summary: [], content: [{type: 'reasoning_text',
  text}], encrypted_content: null}`, with `include: ['reasoning.encrypted_content']` and `reasoning.summary: 'auto'`.
  `adaptCodexResponses` already drops `reasoning` input items for Codex callers (`protocol/index.ts:135`, #127).
- **A non-Codex Responses caller that echoes a reasoning item is refused today**: `parseRequest` throws
  `UNSUPPORTED_INPUT` for any input item type other than message/function_call/function_call_output
  (`protocol/index.ts:62`). **A chat caller that echoes an assistant message with `reasoning_content` is refused
  too**: `message()` rejects unknown fields (`UNSUPPORTED_MESSAGE_FIELD`, `protocol/index.ts:8`).
- **The output limiter counts only text, refusal and tool deltas** (`outlimit/index.ts:125` `isContent`), so reasoning
  would pass it uncounted, as the upstream's own reasoning tokens are today.
- **The Assistant's loop** (`agent-llm/loop.ts:210-219`) handles text, refusal, tool and usage and ignores other types.
  <!-- codex: It ignores their payload, but every event first calls silence.end(), so reasoning permanently stops the waiting timer and may change the working label to “Thinking...” (packages/plugins/agent-llm/loop.ts:208; packages/plugins/agent-llm/loop.ts:140). Explicitly accept this behavior or skip reasoning before that call; do not describe the Assistant as unchanged. -->

## Approach

1. **Contract.** `InferenceEvent` gains `{ type: 'reasoning'; text: string }` (the upstream's raw thinking, in order).
   `Account` gains `forwardReasoning?: boolean`: unset or false keeps today's behavior exactly (the adapter drops the
   reasoning); true makes the chat adapter yield it. Both are additive contract changes; the owner asked for this
   feature on 2026-11-02.
   <!-- codex: Define the flag as chat-adapter output forwarding only, independent of reasoning_effort and reasoning.summary, with z.boolean().optional() and no injected default (packages/runtime/config.ts:24; packages/modules/protocol/index.ts:81). A per-request opt-in would be a separate contract and parser change; it is unnecessary unless the owner wants individual callers to control forwarding. -->
2. **Chat adapter** (`chatcompletions.ts`), only when the account forwards: a stream's `delta.reasoning_content`, or
   `delta.reasoning` when that is a string, becomes a `reasoning` event; a non-streaming reply's
   `message.reasoning_content` / `message.reasoning` too. Other adapters (Anthropic, Claude seat, Codex translated) do
   not emit reasoning in this release.
   <!-- codex: Specify precedence when both aliases exist, handling of empty/null/non-string values, and deterministic ordering when reasoning shares a chunk or JSON message with text/refusal/tools; emit at most one reasoning delta per choice and put it before that choice's answer fields (packages/adapters/providers/chatcompletions.ts:243; packages/adapters/providers/chatcompletions.ts:284). runSse/runJson currently receive no account flag, so thread an explicit boolean through their calls at packages/adapters/providers/chatcompletions.ts:169. -->
3. **Engine.** Reasoning events pass through like other output:
   - they set `emitted`, so the idle timer replaces the wait timer and a failover after the client has seen reasoning
     does not happen (the client has output);
     <!-- codex: No new event-specific emitted logic is needed: every admitted event, including usage today, already sets emitted before yielding (packages/modules/inference/index.ts:624; packages/modules/inference/index.ts:641). This also disables failover for non-streaming requests and consumers that discard reasoning, before any client sees it; state that broader semantic change explicitly (packages/modules/inference/index.ts:714). -->
     <!-- codex: The timers measure upstream silence, with raw body activity resetting either phase, and run only while awaiting the provider; reasoning changes the selected silence threshold rather than starting a duration timer (packages/modules/inference/index.ts:595; packages/modules/inference/index.ts:606). Preserve slow-reader suspension and maxDurationMs, and test reasoning followed by silence and by pings against different wait/idle settings. -->
   - `stream.first_content` stays visible output only (text, tool, refusal, image, native): its meaning and the
     time-to-first-content histogram do not change;
     <!-- codex: Keep first separate from emitted: this branch also warms a spilled conversation account, so adding reasoning to it would change placement behavior as well as metrics (packages/modules/inference/index.ts:626). HTTP telemetry's ttfbMs will nevertheless move to the first reasoning event, while the first-content histogram remains later or absent on reasoning-only replies (packages/plugins/api-openai/index.ts:495; packages/plugins/api-openai/index.ts:555; packages/plugins/metrics/index.ts:134). -->
   - `attempt.completed.output` gains `reasoningChars` (a count; the journal never records content).
     <!-- codex: Emit reasoningChars only when nonzero to preserve existing journal shapes, define it consistently with textChars as string.length, and never infer reasoningTokens from it (packages/modules/inference/index.ts:637; packages/modules/inference/index.ts:679; packages/plugins/metrics/index.ts:129). “The journal never records content” is too broad: optional capture stores requests and text responses, so explicitly exclude raw reasoning from both captured data and event metadata (packages/modules/trajectory/index.ts:29; packages/modules/inference/index.ts:640). -->
     <!-- codex: Activity and explain_request explicitly select existing output fields and will omit reasoningChars, making reasoning-only completion look empty; either include a count-only presentation or document this limitation (apps/web/pages/Activity.tsx:139; packages/plugins/trajectory-recorder/tools.ts:46). The count exists only on completed attempts, so it does not diagnose a thinking turn that stalls or fails (packages/modules/inference/index.ts:701). -->
     <!-- codex: Activity currently says “Safe to retry: nothing was sent” whenever d.retryable is true, although attempt.failed records upstream retryability separately from emitted (apps/web/pages/Activity.tsx:132; packages/modules/inference/index.ts:701). A retryable upstream failure after reasoning must not produce that explanation; guard it with emitted and test the post-reasoning failure case. -->
4. **Responses transport** (api-openai, non-native):
   - **Streaming:** the first reasoning event opens a reasoning output item at the next output index:
     `response.output_item.added` `{id: 'rs_<id>', type: 'reasoning', summary: [], content: []}`, then
     `response.content_part.added` `{type: 'reasoning_text', text: ''}`; each event is a
     `response.reasoning_text.delta`. The item closes (`response.reasoning_text.done`, `response.content_part.done`,
     `response.output_item.done` with the full item) when the first non-reasoning output item opens, or before the
     terminal event. Reasoning that resumes after other output opens a new reasoning item (`rs_<id>_2`, ...).
     <!-- codex: Specify complete envelopes: content-part and reasoning-text events need the same item_id/output_index, content_index: 0 for each segment's sole part, and the shared sequence_number; raw text belongs in content, never summary or reasoning_summary events ([OpenAI streaming reference](https://developers.openai.com/api/reference/resources/responses/streaming-events)). Route them through responseEvent, which owns sequencing and awaited writes (packages/plugins/api-openai/index.ts:476). -->
     <!-- codex: Closing only when a new item opens is insufficient: text → reasoning → more text, or tool arguments → reasoning → more arguments for the same tool, reuses an existing item (packages/plugins/api-openai/index.ts:504; packages/plugins/api-openai/index.ts:508). Close the active reasoning segment before any subsequent text/refusal/tool delta, even when no item opens; usage must neither close nor split it. -->
     <!-- codex: The terminal loop currently treats every non-function item as a message and emits output_text.done for every non-refusal part, so adding reasoning to responseObject alone emits the wrong done event and can close an already-closed segment twice (packages/plugins/api-openai/index.ts:517). Add explicit reasoning dispatch and per-item closure state, preserving open message/tool items that may still receive later deltas. -->
   - **Terminal and non-streaming:** `response.completed` / the response body list the reasoning item(s) in output
     order, `{type: 'reasoning', id, summary: [], content: [{type: 'reasoning_text', text}]}`, no
     `encrypted_content`.
     <!-- codex: The accumulator currently has one message, numeric tool keys, and no reasoning segments; both it and the writer independently determine output order (packages/plugins/api-openai/index.ts:235; packages/plugins/api-openai/index.ts:482). Use distinct tagged order entries and one consistent segment/index assignment for streaming, done items and response bodies, avoiding collisions with upstream tool indexes. -->
     <!-- codex: Define “output order” as item first appearance or explicitly require chronological message segmentation: the existing singleton message merges text on both sides of intervening reasoning, so the final output cannot preserve that full delta chronology (packages/plugins/api-openai/index.ts:236). Do not silently rewrite legacy message/tool grouping or split an unfinished function call merely to force all items into contiguous runs. -->
     <!-- codex: Cover response.incomplete and response.failed as well: responseObject derives incomplete from length/content_filter, while the failure path serializes accumulated output separately (packages/plugins/api-openai/index.ts:251; packages/plugins/api-openai/index.ts:535). Define how already-closed reasoning snapshots survive those terminals, and do not synthesize successful done events for an interrupted segment merely because a failure is terminal. -->
   - **Budget:** reasoning counts toward the existing 8 MiB stream budget (a runaway thinking loop then ends with
     `RESPONSE_TOO_LARGE` instead of growing without bound; the adapter's 16 MiB limit stays below that in practice).
     <!-- codex: The existing “8 MiB” stream guard counts UTF-16 code units, not bytes, while non-streaming counts JSON.stringify(event).length; the adapter independently counts raw UTF-8 SSE bytes, including framing (packages/plugins/api-openai/index.ts:438; packages/plugins/api-openai/index.ts:513; packages/adapters/providers/sse.ts:163). The 16 MiB raw limit can fire first for small framed deltas, so neither its ordering nor RESPONSE_TOO_LARGE is guaranteed; preserve old thresholds for disabled accounts rather than silently changing budget semantics. -->
     <!-- codex: The current stream guard runs after sending and accumulating the event, and response.failed then serializes that accumulator (packages/plugins/api-openai/index.ts:501; packages/plugins/api-openai/index.ts:513; packages/plugins/api-openai/index.ts:535). Define preflight charging for reasoning, include retained segment/item overhead, and maintain incremental totals rather than rescanning every segment per delta; otherwise many tiny alternating segments defeat the intended memory/work bound. -->
     <!-- codex: Every reasoning delta and close event must await the existing send/responseEvent path before reading another engine event; detached writes would bypass drain handling and cancellation (packages/plugins/api-openai/index.ts:469; packages/plugins/api-openai/index.ts:514). Test blocked drains during reasoning and large done/terminal frames, including disconnect and maxDurationMs cleanup, without introducing another queue or timer. -->
   - **Progress events** (#131) stay as they are: the reasoning item is an output item, so once it opens the stream
     carries reasoning deltas, not progress events. Progress keeps covering accounts that do not forward and upstreams
     that send only pings.
     <!-- codex: That coverage is only before the first output item: one reasoning delta followed by a long ping-only phase gets no more response.in_progress events, although raw pings keep the engine alive (packages/plugins/api-openai/index.ts:489; packages/modules/inference/index.ts:595). Document and test this limitation; reusing #131's empty createdEnvelope after output would erase a snapshot-replacing client's accumulated items, so broadening progress is a separate design change. -->
5. **Chat transport:** streaming `delta: {reasoning_content: text}` chunks; non-streaming `message.reasoning_content`.
   `reasoning_content` is the field vLLM, llama.cpp and DeepSeek send, so a client sees what it would see talking to
   the server directly.
   <!-- codex: This canonicalizes upstream reasoning into reasoning_content, so it is not literal field-preserving pass-through for servers that send reasoning (packages/adapters/providers/chatcompletions.ts:243). Document one canonical output field, emit neither alias when no reasoning was forwarded, and preserve role-first, finish, optional usage and [DONE] ordering in the existing chat writer (packages/plugins/api-openai/index.ts:494; packages/plugins/api-openai/index.ts:501; packages/plugins/api-openai/index.ts:516). -->
6. **Input:**
   - Responses: a `reasoning` input item is accepted from any caller and dropped (the model never sees its past
     reasoning, exactly as today). Codex callers keep their existing adaptation note.
     <!-- codex: Define a narrow accepted shape: at minimum the returned item and supplied Codex echo, with string id when present, summary: [], optional content containing only {type:'reasoning_text',text:string}, and absent/null encrypted_content; explicitly decide whether foreign summary-only or non-null encrypted items are also supported. Validate only that new branch, then drop it without changing adjacent assistant/function-call grouping, the remaining-message requirement, or unsupported-item rejection (packages/modules/protocol/index.ts:62; packages/modules/protocol/index.ts:65). -->
     <!-- codex: Keep this acceptance independent of the selected account so replay still works after a flag change or fallback, but do not generalize Codex's whole adaptation to other callers: include, reasoning.summary/context, unknown controls and native-only items retain their current restrictions (packages/modules/protocol/index.ts:31; packages/modules/protocol/index.ts:82; packages/modules/protocol/index.ts:107). Preserve the existing Codex input:reasoning note before parsing, including its established treatment of encrypted items (packages/modules/protocol/index.ts:135). -->
   - Chat: `reasoning_content` / `reasoning` on an assistant message are accepted and dropped.
     <!-- codex: message() is shared by chat and Responses, so extending its global allowlist would also loosen Responses message validation (packages/modules/protocol/index.ts:6; packages/modules/protocol/index.ts:53; packages/modules/protocol/index.ts:62). Accept only string/null values on chat assistant messages, strip those two fields on a copy, and preserve all existing content/tool validation and rejection for other roles, protocols and unknown fields. -->
7. **Unchanged:** native routes, accounts without `forwardReasoning`, the Assistant (ignores reasoning events), image
   requests, the journal's content policy.
   <!-- codex: Add admin-bridge to the explicit behavior decision: it discards unknown event payloads but charges every serialized event against 2 MiB, so enabled reasoning can fail an otherwise small completion without appearing in its response (packages/plugins/admin-bridge/index.ts:121). Either document this bridge exclusion and limit effect or change it deliberately; “every client receives reasoning” is currently false. -->
   <!-- codex: The remaining consumers need an explicit audit disposition, not blanket edits: chat-api carries ChatFrame through the agent, agent-image and the image endpoint select only image events, and test_route reads only text/usage, so it can report a successful empty reply for reasoning-only output (packages/plugins/chat-api/index.ts:204; packages/plugins/agent-image/index.ts:82; packages/plugins/api-openai/index.ts:583; packages/plugins/router-engine/tools.ts:29). The Claude-seat validator deliberately rejects unknown wire types, so extending InferenceEvent must not accidentally imply that bridge now supports reasoning (packages/adapters/providers/claude-seat.ts:129). -->
   <!-- codex: Trajectory recording and its SSE endpoint consume TrajectoryEvent, not InferenceEvent; keep raw reasoning out of them and add only the proposed aggregate count (packages/modules/trajectory/index.ts:40; packages/plugins/trajectory-recorder/index.ts:110). There is no need for per-delta journal events, a new Execution activity counter, or new token metrics for this feature (packages/contracts/index.ts:424; packages/plugins/metrics/index.ts:129). -->

## Rollout (owner decision R1 before the config step)

The image changes nothing until an account sets `forwardReasoning: true`. Proposed after the roll, through the admin
API with `If-Match` and a read-back: `lan-192.168.0.28-18020` (cloud2, `qwen3.8-27b`) and `lan-192.168.0.26-8080`
(cloud3). Every client of those accounts then receives reasoning: the dev workers' Codex (Responses), and any chat
client (dsh, the platform eval key) gets `reasoning_content` deltas, as it would from vLLM directly. The owner decides
which accounts. Rollback of the image: the old account schema strips the unknown field.

<!-- codex: “Changes nothing” must be limited to previously accepted requests and their output: the proposed parser changes newly accept reasoning-bearing histories globally, before account selection (packages/plugins/api-openai/index.ts:411). R1 should explicitly cover all models/callers on the enabled accounts, changed retry/timer behavior, and internal consumers; show_raw_agent_reasoning is only a client display preference, not a per-request forwarding opt-in. -->
<!-- codex: Image rollback does not itself strip the saved field: SQLite returns parsed config directly, and config-store installs it without validateConfig at startup (packages/adapters/sqlite/sqlite-worker.mjs:139; packages/plugins/config-store/index.ts:142). Old code ignores the field until a validated write strips it, and a later upgrade can see it again; specify an explicit flag-off rollback with If-Match/read-back and test restart/rollback persistence. -->

## Out of scope

- Reasoning for the Anthropic and Claude-seat adapters (thinking blocks), and an Anthropic `/v1/messages` endpoint.
- Sending past reasoning back to the model (the history stays as today).
- Reasoning summaries (`reasoning.summary`): the router forwards raw reasoning text only.
- An Assistant UI for thinking.

## Critical files

llm-router (branch off `main` `08c0fc35`):
- `packages/contracts/index.ts` - the event type, `Account.forwardReasoning`.
- `packages/runtime/config.ts` - the account schema field.
- `packages/adapters/providers/chatcompletions.ts` - read reasoning when the account forwards.
- `packages/modules/inference/index.ts` - pass-through, `reasoningChars`.
- `packages/plugins/api-openai/index.ts` - the reasoning item lifecycle (stream and body), chat deltas, the
  accumulator's reasoning segments, the budget.
- `packages/modules/protocol/index.ts` - accept and drop reasoning input (Responses items, chat assistant fields).
- Tests next to each; README (the streaming and account sections), `docs/implementation-status.md`.

<!-- codex: Include the consumer fixes/dispositions above and their existing integration suites, especially tests/http-protocol.test.ts, tests/stream-timeouts.test.ts and tests/codex-chat-routes.test.ts; merely adding adapter tests will miss the shared transport behavior. If configuration remains admin-API-only, say so: inspect_account omits the new flag and update_account's strict schema rejects it unless deliberately extended (packages/plugins/config-store/tools.ts:126; packages/plugins/config-store/tools.ts:398). -->

## Verification

1. **Failing tests first**, then passing:
   - adapter: reasoning read when the account forwards (streaming, both field names, non-streaming), ignored when not;
     <!-- codex: Add both aliases together, null/empty/non-string fields, mixed reasoning+text+tool fields in one frame, reasoning-only replies, fragmented UTF-8/SSE, and the existing malformed termination cases (packages/adapters/providers/chatcompletions.ts:243; packages/adapters/providers/chatcompletions.ts:258; packages/adapters/providers/chatcompletions.ts:299). Exercise unset, false and true independently, and verify usage/finish remain unchanged. -->
   - Responses streaming: the event sequence and indexes (reasoning item 0, then message/tool items), the done events
     before the next item and before the terminal event, `response.completed` output order, resumed reasoning as a
     second item, sequence numbers strictly increasing;
     <!-- codex: Test text → reasoning → same message, tool 0 → reasoning → tool 0 continuation, alternating tool indexes, refusal interleaving, usage between reasoning deltas, and reasoning-only finish. Assert every referenced item/part was opened, ids/indexes match the terminal snapshot, each done occurs exactly once, and no delta follows its item's done; initial reasoning-then-tool alone does not cover these transitions (packages/plugins/api-openai/index.ts:482; packages/plugins/api-openai/index.ts:517). -->
   - Responses non-streaming body; chat streaming and non-streaming fields;
   - input: a non-Codex Responses request echoing reasoning items, and a chat request echoing `reasoning_content`, are
     accepted and the upstream never receives the reasoning;
     <!-- codex: Add rejection tests for malformed reasoning payloads, reasoning fields on non-assistant chat roles and Responses messages, unknown fields/items, and still-unsupported non-Codex top-level controls (packages/modules/protocol/index.ts:8; packages/modules/protocol/index.ts:32). Test reasoning-only input after dropping, call grouping across dropped items, both chat aliases, no mutation of the raw body, and no raw reasoning in captured normalized requests. -->
   - engine: `emitted` after reasoning (no failover after it), `stream.first_content` only for visible output,
     `reasoningChars` in `attempt.completed`; the output limiter does not count reasoning;
     <!-- codex: Also test retryable rejection before reasoning versus after reasoning, reasoning followed by an idle timeout, cancellation, maxDurationMs and consumer cleanup, with flag-off counterparts (packages/modules/inference/index.ts:610; packages/modules/inference/index.ts:714). Keep OutputLimiter unchanged: it already returns non-content events, and the engine must still stop immediately when a subsequent visible-output limit is hit, without counting reasoning or fabricating reasoning-token usage (packages/modules/outlimit/index.ts:152; packages/modules/inference/index.ts:645). -->
   - progress events do not appear once a reasoning item opened; the budget counts reasoning;
     <!-- codex: Extend #131 coverage to progress before the first reasoning item, progress followed by failover before any output, exact created-envelope preservation, no post-terminal timer writes, and the reasoning-then-pings limitation (tests/stream-timeouts.test.ts:531). Budget tests need non-ASCII data, many tiny segments, mixed output at the boundary, both protocols and stream modes, plus provider/admission cleanup after overflow. -->
   - accounts without the flag: byte-identical streams to before (the existing suites stay green).
     <!-- codex: Green existing tests do not establish byte identity: compare captured baseline streams/bodies for unset and false using fixed ids, time and heartbeat scheduling, including tool/refusal ordering, errors, incomplete output and usage (tests/http-protocol.test.ts:8; tests/codex-chat-routes.test.ts:79). Also assert true-with-no-upstream-reasoning introduces no empty fields/items, and retain native-route fixtures unchanged. -->
2. **CI gates in docker** (node:24): typecheck, test, build, build:lib, test:production, check:architecture, against the
   `main` baseline.
   <!-- codex: Name pnpm 10.32.1, frozen-lockfile installation and pnpm verify explicitly, and run the gates on the candidate as well as recording the baseline (AGENTS.md:40; package.json:37). Report unavailable PostgreSQL auth coverage as skipped, add browser verification if Activity changes, and keep temporary servers/containers cleaned up; no CI workflow change is needed for these checks. -->
3. **The real Codex CLI end to end** (0.162.1 and 0.166.0, the in-process router, a fake vLLM-style server streaming
   `reasoning_content` then a tool call, then an answer): exit 0 over two turns; with `show_raw_agent_reasoning = true`
   the thinking appears in the output; the second upstream request carries no reasoning; with a 20 s idle timeout and
   45 s of thinking the turn completes in one request (reasoning deltas are events).
   <!-- codex: #131 already lets this long-thinking test pass without forwarding, so require observed reasoning_text deltas and displayed raw text, not just successful completion (packages/plugins/api-openai/index.ts:488). Distinguish the CLI idle timeout from the router's upstream idle timer, count requests per turn with client retries disabled, and include resumed reasoning/interleaved-tool coverage beyond the already-verified simple fixture. -->
4. **SDK helpers** (OpenAI Node and Python): Responses `stream().finalResponse()` / `get_final_response()` with a
   reasoning item in output; a second turn that sends `response.output` back as input is accepted; chat streaming
   exposes `reasoning_content` without error; an echoed assistant message with `reasoning_content` is accepted.
   <!-- codex: Pin Node's existing openai 7.23.0 and an explicit Python version, and execute the actual stream helpers rather than only JSON-decoding SSE (package.json:89). Assert exact reasoning text/content, item order and ids in helper results, inspect intermediate event handling, and separately test raw chat chunks versus any chat accumulator: accepting an extra delta field does not prove a helper preserves it in its final message. -->
5. **Live, after the roll and R1:** a dev worker's Codex on qwen-27b and flash-next with `show_raw_agent_reasoning`
   shows the thinking; the journal's `reasoningChars` is non-zero; `validate-live` at the known baseline.
   <!-- codex: Tie each live assertion to the actual serving account/model and approved flag value, since route names and fallbacks do not establish which account served (scripts/validate-live.mjs:45). Existing validation reads chat delta.content and does not prove reasoning forwarding; the shared-routes non-stream check also accepts any content[].text, which could now mistake reasoning-only output for an answer (scripts/validate-live.mjs:75; scripts/validate-shared-routes.mjs:45). -->

## Risks

- **Clients that store and replay output.** Covered by accepting reasoning input; verified with both SDK helpers.
  <!-- codex: This is an acceptance criterion, not completed verification: the supplied evidence establishes the two Codex versions, not the SDK helper matrix. Replay also remains subject to the existing 2 MiB Responses subset body cap, so a response within the output budget can still be too large to echo next turn; document that limit without expanding upload scope (packages/plugins/api-openai/index.ts:413). -->
- **Volume.** A 20k-token thinking phase is ~80 KB of deltas per turn; transcripts that log raw reasoning grow.
  <!-- codex: That estimate covers approximate text payload only; SSE framing, repeated full text in done/terminal events, segment objects and replayed history increase wire and retained sizes (packages/plugins/api-openai/index.ts:476; packages/plugins/api-openai/index.ts:517). Keep the estimate illustrative and use measured byte counts in budget/backpressure tests. -->
- **Behavior change for chat clients on enabled accounts** (dsh, the platform eval key): extra `reasoning_content`
  deltas. Gated per account by R1.

<!-- codex-review-status: complete -->