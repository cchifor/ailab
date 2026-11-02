# llm-router: forward the upstream's reasoning (thinking) to the client

## Codex Review

- Round 2 still needs explicit protocol-specific accumulation, discarded-reasoning budget semantics, and rollback handling for saved histories.
- Leading-only forwarding fits the existing writer, including tool/refusal boundaries and incomplete terminals; the proposed input subset covers the supplied Codex echo.
- Remaining coverage gaps include empty-argument tool announcements, failures after reasoning has closed, backpressure during early closure, and transport regressions with forwarding disabled.
- The preflight-budget objection is dropped: one leading item removes the segment-growth concern, and preserving the existing post-send guard is reasonable.
- Reviewed read-only against `08c0fc3`; concurrent working-tree edits were excluded. No implementation, SDK probes, or verification gates were run.

## Context

The owner asked (2026-11-02) for the router to forward a local model's thinking, so it behaves like a pass-through
rather than hiding it. Today, on every chat-backed route (local vLLM/llama.cpp, DeepSeek, OpenAI chat accounts):

- **The adapter drops it.** `packages/adapters/providers/chatcompletions.ts` `runSse`/`runJson` read only
  `delta.content`, `delta.refusal` and `delta.tool_calls`; `reasoning_content` (vLLM, llama.cpp, DeepSeek) and
  `reasoning` (newer vLLM) are never read. There is no reasoning type in `InferenceEvent`
  (`packages/contracts/index.ts:249`).
- **What it costs.** An agent's thinking is invisible: a run that "goes silent for 7 minutes" cannot be told apart from
  a stall. Until router #131 (progress events, live since 2026-11-02 13:33Z as
  `router-0.1.0-20261102-progress-events`) it also killed long-thinking turns at Codex's 300 s idle timeout.
- **Native Codex routes are already a pass-through** (`type: 'native'` events forwarded as the upstream sent them).

Facts verified for this plan (do not re-derive):

- **The real Codex CLI accepts reasoning items without `encrypted_content`** (docker, 2026-11-02, fake Responses
  server streaming `output_item.added {type: reasoning, summary: [], content: []}`, `content_part.added`
  (`reasoning_text`), `reasoning_text.delta`, the done events, then a function call): Codex 0.162.1 and 0.166.0 exit 0
  over two turns; with `show_raw_agent_reasoning = true` the text shows in `codex exec` output, with the default it is
  hidden but accepted.
- **Codex sends the item back next turn** as `{type: 'reasoning', id, summary: [], content: [{type: 'reasoning_text',
  text}], encrypted_content: null}` (with `include: ['reasoning.encrypted_content']`, `reasoning.summary: 'auto'`).
  `adaptCodexResponses` already drops `reasoning` input items for Codex callers (`protocol/index.ts:135`).
- **Replays are refused today:** a non-Codex Responses `reasoning` input item is `UNSUPPORTED_INPUT`
  (`protocol/index.ts:62`); a chat assistant message with `reasoning_content` is `UNSUPPORTED_MESSAGE_FIELD`
  (`message()`, `protocol/index.ts:8`, shared by both protocols).
- **The output limiter counts only text, refusal and tool deltas** (`outlimit/index.ts:125`); other events pass.
- **Every admitted event sets `emitted`** (usage included, `inference/index.ts:641`), so any forwarded reasoning event
  switches the attempt to the idle timer and ends failover eligibility.
- **Consumers that switch on event types:** api-openai (both protocols, the accumulator, the 8 MiB stream guard),
  `agent-llm/loop.ts` (calls `silence.end()` on every event, then handles text/refusal/tool/usage), `admin-bridge`
  (charges every serialized event against 2 MiB, keeps text/usage/finish/tools), `agent-image` and the image endpoint
  (image events only), `router-engine/tools.ts` `test_route` (text/usage), `chat-api` (through the agent). Trajectory
  recording consumes `TrajectoryEvent`, not `InferenceEvent`. The repo pins `openai` 7.23.0 for its tests.

## Approach

1. **Contract.** `InferenceEvent` gains `{ type: 'reasoning'; text: string }` (raw thinking, in upstream order).
   `Account.forwardReasoning?: boolean` (`z.boolean().optional()`, no injected default): it controls only whether the
   chat-completions adapter yields reasoning; it is independent of `reasoning_effort` and `reasoning.summary`. Unset or
   false: the adapter yields exactly what it yields today. Both are additive; the owner asked for this feature.
   Configured through the admin API (`PUT /admin/v1/config`); `inspect_account`/`update_account` are not extended.
2. **Chat adapter** (`chatcompletions.ts`; an explicit `forwardReasoning` boolean threaded from `run()` into
   `runSse`/`runJson`):
   - per choice, `delta.reasoning_content` when it is a non-empty string, else `delta.reasoning` when that is a
     non-empty string (never both; null, empty and non-string values are ignored); one `reasoning` event per choice,
     yielded before that choice's text, refusal and tool deltas;
   - non-streaming: `message.reasoning_content`, else `message.reasoning`, the same rules, before the content;
   - usage, finish and the malformed-stream checks are unchanged. Other adapters emit no reasoning.
3. **Engine.** No new emitted logic (every admitted event already sets it). With forwarding on, the first reasoning
   event therefore moves the attempt to the idle timer and ends failover for that request, for every caller including
   non-streaming ones and internal consumers that discard reasoning. That is the accepted cost of forwarding, stated in
   the docs. `first` / `stream.first_content` stay visible output only (the spill-warming branch and the
   time-to-first-content histogram are unchanged); HTTP telemetry's `ttfbMs` moves to the first reasoning event.
   `attempt.completed.output.reasoningChars` (string length, present only when non-zero, never used to infer tokens).
   Optional request capture keeps recording text only, never reasoning.
4. **Responses transport** (api-openai, non-native): **the leading reasoning segment only.**
   - The first reasoning event, when no output item exists yet, opens one reasoning item at output index 0 through
     `responseEvent` (awaited, sequenced): `response.output_item.added {id: 'rs_<id>', type: 'reasoning', summary: [],
     content: []}`, `response.content_part.added {item_id, output_index: 0, content_index: 0, part: {type:
     'reasoning_text', text: ''}}`; each reasoning event is `response.reasoning_text.delta {item_id, output_index: 0,
     content_index: 0, delta}`. Raw text goes in `content`, never in `summary`.
   - The item closes exactly once: `response.reasoning_text.done`, `response.content_part.done`,
     `response.output_item.done {item: {id, type: 'reasoning', summary: [], content: [{type: 'reasoning_text', text}]}}`
     before the first visible delta (text, refusal or tool) is written, or, if the stream ends with only reasoning,
     before `response.completed` / `response.incomplete`. Usage events neither close nor split it. On
     `response.failed` it is not closed with done events; the failed snapshot lists it as it stood.
     <!-- codex: round-2: Keep item closure separate from routerTerminal: setting that flag when reasoning closes before visible output would change shutdown handling for a response that is still running (packages/plugins/api-openai/index.ts:479; packages/plugins/api-openai/index.ts:541). Close reasoning-only completed/incomplete output inside the normal terminal path at line 517, and leave the failure path at line 535 snapshot-only. -->
   - Reasoning that arrives after visible output began is not forwarded on this protocol (counted in
     `reasoningChars`). vLLM's and llama.cpp's reasoning parsers emit reasoning before the answer; interleaved
     segments would need open-item bookkeeping this release does not take on.
   - The accumulator gains `reasoning: string` and a tagged order entry `'reasoning'` that is always first; the terminal
     done loop skips the already-closed reasoning item; `response.completed`, `response.incomplete`, `response.failed`
     and non-streaming bodies list it first. Message/tool grouping is unchanged.
     <!-- codex: round-2: accumulate() is shared by both protocols and both streaming modes, so specify its protocol-dependent retention policy rather than only filtering Responses deltas (packages/plugins/api-openai/index.ts:236; packages/plugins/api-openai/index.ts:438; packages/plugins/api-openai/index.ts:513). Responses must retain only the leading text and never append to it after closure, while Chat must retain every segment; otherwise terminal/non-streaming snapshots can resurrect suppressed reasoning or Chat can lose later segments. -->
   - Budget: reasoning counts in the existing stream guard (same code units, same post-send check) and in the
     non-streaming size check; accounts that do not forward never carry reasoning, so their thresholds are unchanged.
     <!-- codex: round-2: Decide explicitly whether discarded post-visible reasoning counts toward the Responses stream budget: the current streaming guard sums retained fields, whereas non-streaming charges every serialized event before accumulation (packages/plugins/api-openai/index.ts:438; packages/plugins/api-openai/index.ts:513). If all reasoning is to count, track its cumulative length separately from the retained leading string, without storing the discarded text. -->
   - Progress events (#131) are unchanged: once the reasoning item opens, the stream carries deltas, not progress. Limit:
     one reasoning delta followed by a long ping-only phase gets no further progress events (keepalive comments only);
     broadening progress after output is a separate design.
5. **Chat transport:** streaming chunks `{delta: {reasoning_content: text}}` in upstream order (all segments; chat has
   no items); non-streaming `message.reasoning_content` (the concatenated reasoning). One canonical field,
   `reasoning_content` (what vLLM, llama.cpp and DeepSeek send), whichever alias the upstream used; no field when there
   was no reasoning. Role first, finish, optional usage chunk and `[DONE]` keep their order.
6. **Input** (independent of the selected account, so a replay still works after a flag change or a fallback):
   - Responses (non-Codex callers): a `reasoning` input item of exactly the shape the router emits or Codex echoes is
     accepted and dropped: `type: 'reasoning'`, optional string `id`, `summary` an array (empty, or of `{type:
     'summary_text', text: string}`), optional `content` an array of `{type: 'reasoning_text', text: string}`, and
     `encrypted_content` absent or null. Anything else in such an item (an encrypted item, other part types) stays
     `UNSUPPORTED_INPUT`. Dropping it does not change the grouping of the messages and calls around it. Codex callers
     keep their existing adaptation (`input:reasoning`, encrypted items included). No other Responses control is
     loosened.
   - Chat: on assistant messages only, `reasoning_content` and `reasoning` with a string or null value are removed on a
     copy before `message()`; `message()`'s allowlist is unchanged, so Responses messages and other roles still reject
     them.
   - The Responses subset's 2 MiB body cap still applies to a replayed history.
7. **Internal consumers:**
   - `agent-llm/loop.ts`: reasoning ends the waiting timer and may show "Thinking..." while the model thinks (accepted,
     documented); the answer handling is unchanged.
   - `admin-bridge`: reasoning events are skipped before the size charge (they are discarded anyway), so they cannot
     fail a small completion.
   - `test_route` (`router-engine/tools.ts`): a reasoning-only reply is reported as an empty reply, as today
     (documented).
   - `agent-image`, the image endpoint, `chat-api` and the Claude-seat wire validator: unchanged (no reasoning reaches
     them, or they select other event types).
   - Activity: the `attempt.failed` explanation says "nothing was sent" only when `emitted` is false; Activity and
     `explain_request` show `reasoningChars` as a count.
   <!-- codex: round-2: The consumer audit still misses scripts/validate-shared-routes.mjs:45, which accepts any output item's content[].text as an answer; a reasoning-only completed response will now pass that check. Require a message/output_text part there so the validation continues to prove visible answer output. -->

## Rollout (owner decision R1)

The image changes nothing for requests and outputs that were accepted before, until an account sets
`forwardReasoning: true`. The parser does newly accept reasoning-bearing histories (point 6) for every caller.

<!-- codex: round-2: The Activity correction is another global behavior change: retryable failures after text, tools or usage will lose the old explanation even on accounts without forwarding (apps/web/pages/Activity.tsx:132; packages/modules/inference/index.ts:641; packages/modules/inference/index.ts:701). Keep the correction, but disclose it alongside parser acceptance rather than retaining this unconditional compatibility claim. -->

- **Proposal R1:** after the roll, `forwardReasoning: true` on `lan-192.168.0.28-18020` (cloud2) and
  `lan-192.168.0.26-8080` (cloud3) through the admin API (`If-Match`, read-back, revision recorded). Every caller and
  model on those accounts then gets reasoning: the dev workers' Codex (Responses), the Assistant, and chat clients (dsh,
  the platform eval key) as `reasoning_content` deltas, the field they would get from vLLM directly. Those requests also
  lose failover once reasoning has started (point 3).
  <!-- codex: round-2: Distinguish internal event delivery from user-visible reasoning here: the Assistant, admin-bridge and test_route still discard the payload (packages/plugins/agent-llm/loop.ts:210; packages/plugins/admin-bridge/index.ts:123; packages/plugins/router-engine/tools.ts:29). The statement that every caller gets reasoning otherwise contradicts point 7 and the excluded Assistant thinking UI. -->
- **Rollback:** first `forwardReasoning: false` on those accounts (`If-Match`, read-back), then the image. The old image
  does not strip the saved field until a validated config write, so the flag must be turned off explicitly.
  <!-- codex: round-2: Flag-off stops new reasoning but does not remove it from clients' saved histories: rolling back the parser makes non-Codex Responses replays and chat assistant replays fail with 400 again (packages/modules/protocol/index.ts:8; packages/modules/protocol/index.ts:62). Specify a rollback image retaining input compatibility or a documented history-cleanup/new-conversation procedure, and verify a history produced before rollback; Codex's existing adaptation already handles its own replay. -->

## Out of scope

- Reasoning segments after visible output on the Responses protocol; broadening progress events after output.
- Reasoning for the Anthropic and Claude-seat adapters, and an Anthropic `/v1/messages` endpoint.
- Sending past reasoning back to the model; reasoning summaries.
- `inspect_account`/`update_account` support for the flag; an Assistant UI for thinking.

## Critical files

llm-router (branch `feat/forward-reasoning` off `main` `08c0fc35`):
- `packages/contracts/index.ts`, `packages/runtime/config.ts` - the event type and the account field.
- `packages/adapters/providers/chatcompletions.ts` - read reasoning when the account forwards.
- `packages/modules/inference/index.ts` - `reasoningChars`.
- `packages/plugins/api-openai/index.ts` - the Responses reasoning item, chat fields, accumulator, budgets.
- `packages/modules/protocol/index.ts` - accept and drop replayed reasoning (both protocols, narrowly).
- `packages/plugins/admin-bridge/index.ts`, `apps/web/pages/Activity.tsx`, `packages/plugins/trajectory-recorder/tools.ts`.
- Tests: next to each, and `tests/http-protocol.test.ts`, `tests/stream-timeouts.test.ts`,
  `tests/codex-chat-routes.test.ts`. Docs: README (streaming, account), `docs/implementation-status.md`.

## Verification

1. **Failing tests first**, then passing:
   - adapter: unset / false / true; both aliases (precedence), null/empty/non-string, reasoning+text+tool in one
     chunk (reasoning first), reasoning-only replies, fragmented SSE frames, non-streaming; usage and finish unchanged;
     with the flag off, the event sequence is identical to today's for the same body;
     <!-- codex: round-2: Adapter event equality does not cover regressions introduced by changing the shared accumulator and terminal loop (packages/plugins/api-openai/index.ts:236; packages/plugins/api-openai/index.ts:517). Add unset/false HTTP regressions for both protocols and modes, including refusal/tool order, incomplete and failed Responses, and unchanged native output. -->
   - Responses streaming: reasoning item 0 then message/tool items at 1..n; the three done events exactly once, before
     the first visible item's `output_item.added`; reasoning-only replies closed before `response.completed` and before
     `response.incomplete`; not closed before `response.failed`; usage between reasoning deltas; reasoning after output
     not forwarded but counted; ids and indexes equal the terminal snapshot; sequence numbers strictly increasing; no
     delta after its item's done;
     <!-- codex: round-2: Explicitly test a tool announcement with empty arguments, refusal-first output, and reasoning sharing an upstream chunk with each: tool metadata already opens a visible item, so closure cannot wait for non-empty arguments (packages/plugins/api-openai/index.ts:508; tests/codex-chat-routes.test.ts:36). Also test visible output followed by the first reasoning event, which must never create a reasoning item. -->
     <!-- codex: round-2: Qualify “not closed before response.failed” to a reasoning item still open when the failure occurs: reasoning → visible output → error must retain its earlier close exactly once, with the same text in the failed snapshot (packages/plugins/api-openai/index.ts:535). Cover both length and content_filter incomplete endings, and assert that the failure handler never starts or repeats the close sequence. -->
   - non-streaming Responses body; chat streaming (canonical field, order of role/finish/usage/[DONE]) and
     non-streaming; true-with-no-reasoning adds no empty field or item;
     <!-- codex: round-2: Exercise reasoning A → text/tool → reasoning B in both modes: Responses snapshots must contain only A, while Chat's accumulated reasoning must contain A+B (packages/plugins/api-openai/index.ts:438; packages/plugins/api-openai/index.ts:513). Checking only streamed delta suppression would miss leakage through the terminal or non-streaming body. -->
   - input: a non-Codex Responses history echoing the router's reasoning items and Codex's echo shape is accepted, the
     upstream never receives reasoning, calls stay grouped; encrypted items, other part types, reasoning fields on
     Responses messages and on non-assistant chat roles are refused; chat assistant `reasoning_content`/`reasoning`
     accepted and stripped; the raw body is not mutated;
   - engine: failover allowed before reasoning, not after; reasoning then silence hits the idle timer; reasoning then
     pings stays alive; `maxDurationMs` still ends it; `reasoningChars` only when non-zero; the limiter still stops at
     the visible-output limit and never counts reasoning; capture holds no reasoning;
   - progress: before the first reasoning item as in #131, none after; the budget counts reasoning (non-ASCII, at the
     boundary);
     <!-- codex: round-2: Add controlled write/drain tests during a reasoning delta and the early close events, including disconnect and maxDurationMs while full-text done frames await drain (packages/plugins/api-openai/index.ts:469; tests/stream-timeouts.test.ts:679). Assert no next provider read or visible-item write while blocked, and no attempt to finish a partially written close sequence after cancellation; ordinary SSE decoding does not exercise this new path. -->
   - admin-bridge skips reasoning in its size charge; Activity's "nothing was sent" only when `emitted` is false.
2. **CI gates in docker** (node:24, pnpm 10.32.1, `pnpm install --frozen-lockfile`): typecheck, test, build,
   build:lib, test:production, check:architecture on the candidate, against the `main` baseline; PostgreSQL auth tier
   reported as skipped when absent.
3. **The real Codex CLI end to end** (0.162.1 and 0.166.0, the in-process router with `forwardReasoning: true`, a fake
   vLLM-style server streaming `reasoning_content` then a tool call, then reasoning then an answer, client retries
   disabled): exit 0 over two turns with exactly two upstream requests; with `show_raw_agent_reasoning = true` the
   thinking text appears in the output (observed `reasoning_text` deltas, not just completion); the second upstream
   request carries no reasoning.
4. **SDK helpers:** Node `openai@7.23.0` (the repo's pin) and a pinned Python `openai`: Responses
   `stream().finalResponse()` / `get_final_response()` return the reasoning item first with its exact text; a second
   turn that sends `response.output` back as input is accepted; chat: the raw chunks carry `reasoning_content`, and the
   helpers do not fail on it; an echoed assistant message with `reasoning_content` is accepted.
5. **Live, after the roll and R1:** a dev worker's Codex on qwen-27b with `show_raw_agent_reasoning = true` shows the
   thinking; the journal of that request names the account that served it, with `reasoningChars` non-zero.
   `validate-live` at the known baseline (it does not exercise reasoning).

## Risks

- **Failover after reasoning:** forwarding accounts lose it once thinking has started (point 3).
- **Volume:** a 20k-token thinking phase is roughly 80 KB of text plus SSE framing and the repeated full text in the
  done and terminal events; transcripts that log raw reasoning grow. Measured sizes go in the budget tests.
- **Chat clients on enabled accounts** get `reasoning_content` deltas; gated per account by R1.

<!-- codex-review-status: complete -->