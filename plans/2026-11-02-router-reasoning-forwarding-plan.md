# llm-router: forward the upstream's reasoning (thinking) to the client

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

## Approach

1. **Contract.** `InferenceEvent` gains `{ type: 'reasoning'; text: string }` (the upstream's raw thinking, in order).
   `Account` gains `forwardReasoning?: boolean`: unset or false keeps today's behavior exactly (the adapter drops the
   reasoning); true makes the chat adapter yield it. Both are additive contract changes; the owner asked for this
   feature on 2026-11-02.
2. **Chat adapter** (`chatcompletions.ts`), only when the account forwards: a stream's `delta.reasoning_content`, or
   `delta.reasoning` when that is a string, becomes a `reasoning` event; a non-streaming reply's
   `message.reasoning_content` / `message.reasoning` too. Other adapters (Anthropic, Claude seat, Codex translated) do
   not emit reasoning in this release.
3. **Engine.** Reasoning events pass through like other output:
   - they set `emitted`, so the idle timer replaces the wait timer and a failover after the client has seen reasoning
     does not happen (the client has output);
   - `stream.first_content` stays visible output only (text, tool, refusal, image, native): its meaning and the
     time-to-first-content histogram do not change;
   - `attempt.completed.output` gains `reasoningChars` (a count; the journal never records content).
4. **Responses transport** (api-openai, non-native):
   - **Streaming:** the first reasoning event opens a reasoning output item at the next output index:
     `response.output_item.added` `{id: 'rs_<id>', type: 'reasoning', summary: [], content: []}`, then
     `response.content_part.added` `{type: 'reasoning_text', text: ''}`; each event is a
     `response.reasoning_text.delta`. The item closes (`response.reasoning_text.done`, `response.content_part.done`,
     `response.output_item.done` with the full item) when the first non-reasoning output item opens, or before the
     terminal event. Reasoning that resumes after other output opens a new reasoning item (`rs_<id>_2`, ...).
   - **Terminal and non-streaming:** `response.completed` / the response body list the reasoning item(s) in output
     order, `{type: 'reasoning', id, summary: [], content: [{type: 'reasoning_text', text}]}`, no
     `encrypted_content`.
   - **Budget:** reasoning counts toward the existing 8 MiB stream budget (a runaway thinking loop then ends with
     `RESPONSE_TOO_LARGE` instead of growing without bound; the adapter's 16 MiB limit stays below that in practice).
   - **Progress events** (#131) stay as they are: the reasoning item is an output item, so once it opens the stream
     carries reasoning deltas, not progress events. Progress keeps covering accounts that do not forward and upstreams
     that send only pings.
5. **Chat transport:** streaming `delta: {reasoning_content: text}` chunks; non-streaming `message.reasoning_content`.
   `reasoning_content` is the field vLLM, llama.cpp and DeepSeek send, so a client sees what it would see talking to
   the server directly.
6. **Input:**
   - Responses: a `reasoning` input item is accepted from any caller and dropped (the model never sees its past
     reasoning, exactly as today). Codex callers keep their existing adaptation note.
   - Chat: `reasoning_content` / `reasoning` on an assistant message are accepted and dropped.
7. **Unchanged:** native routes, accounts without `forwardReasoning`, the Assistant (ignores reasoning events), image
   requests, the journal's content policy.

## Rollout (owner decision R1 before the config step)

The image changes nothing until an account sets `forwardReasoning: true`. Proposed after the roll, through the admin
API with `If-Match` and a read-back: `lan-192.168.0.28-18020` (cloud2, `qwen3.8-27b`) and `lan-192.168.0.26-8080`
(cloud3). Every client of those accounts then receives reasoning: the dev workers' Codex (Responses), and any chat
client (dsh, the platform eval key) gets `reasoning_content` deltas, as it would from vLLM directly. The owner decides
which accounts. Rollback of the image: the old account schema strips the unknown field.

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

## Verification

1. **Failing tests first**, then passing:
   - adapter: reasoning read when the account forwards (streaming, both field names, non-streaming), ignored when not;
   - Responses streaming: the event sequence and indexes (reasoning item 0, then message/tool items), the done events
     before the next item and before the terminal event, `response.completed` output order, resumed reasoning as a
     second item, sequence numbers strictly increasing;
   - Responses non-streaming body; chat streaming and non-streaming fields;
   - input: a non-Codex Responses request echoing reasoning items, and a chat request echoing `reasoning_content`, are
     accepted and the upstream never receives the reasoning;
   - engine: `emitted` after reasoning (no failover after it), `stream.first_content` only for visible output,
     `reasoningChars` in `attempt.completed`; the output limiter does not count reasoning;
   - progress events do not appear once a reasoning item opened; the budget counts reasoning;
   - accounts without the flag: byte-identical streams to before (the existing suites stay green).
2. **CI gates in docker** (node:24): typecheck, test, build, build:lib, test:production, check:architecture, against the
   `main` baseline.
3. **The real Codex CLI end to end** (0.162.1 and 0.166.0, the in-process router, a fake vLLM-style server streaming
   `reasoning_content` then a tool call, then an answer): exit 0 over two turns; with `show_raw_agent_reasoning = true`
   the thinking appears in the output; the second upstream request carries no reasoning; with a 20 s idle timeout and
   45 s of thinking the turn completes in one request (reasoning deltas are events).
4. **SDK helpers** (OpenAI Node and Python): Responses `stream().finalResponse()` / `get_final_response()` with a
   reasoning item in output; a second turn that sends `response.output` back as input is accepted; chat streaming
   exposes `reasoning_content` without error; an echoed assistant message with `reasoning_content` is accepted.
5. **Live, after the roll and R1:** a dev worker's Codex on qwen-27b and flash-next with `show_raw_agent_reasoning`
   shows the thinking; the journal's `reasoningChars` is non-zero; `validate-live` at the known baseline.

## Risks

- **Clients that store and replay output.** Covered by accepting reasoning input; verified with both SDK helpers.
- **Volume.** A 20k-token thinking phase is ~80 KB of deltas per turn; transcripts that log raw reasoning grow.
- **Behavior change for chat clients on enabled accounts** (dsh, the platform eval key): extra `reasoning_content`
  deltas. Gated per account by R1.

<!-- codex-review-status: pending -->
