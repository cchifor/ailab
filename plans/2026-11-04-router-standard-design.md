<!-- status: proposed (design only; no llm-router code or config changed) -->
# llm-router: one standard for every protocol and every upstream

**Date:** 2026-11-04, revised 2026-11-05 (the owner's decisions, §13; reviewer-claude's findings on PR #1602
`ff2bbe36`; the night's validation runs, §4.7; the owner's validation requirements, §9.6-9.7) · **Scope:** the whole `cchifor/llm-router` request path: every client protocol it serves and
every upstream it routes to. The code is `origin/main` `247b19f` (#136). **Extends:** the adopted native-first design
(ailab PR #1599, `plans/2026-11-04-router-native-first-design.md`, decisions D1-D5, and D6 there), which is
local-model-centric. Its sections are cited as "native-first §n". **Audience:** the owner, and whoever implements it.

The owner's brief, verbatim: *"We must have a robust implementation for the router that works with both local models
(e.g. qwen) and remote models (e.g. claude, codex). Check the web and github, find the best solution / standard to
implement the router. Avoid workarounds. Cross validate with codex."*

Evidence labels: **[src]** read in source at the stated tag; **[doc]** vendor or project documentation; **[issue]**
issue or PR text; **[anecdote]** a report not verified; **[infer]** reasoning from source, not run; **[opinion]**.
Web sources were accessed 2026-11-04 unless a date is given. Nothing was sent to any model while writing this.

---

## 0. Summary

**There is no single standard to adopt, and the research says there will not be one soon.** Three wire protocols are
the de-facto standards, each native to a different set of the router's clients and upstreams:

| Protocol | Clients | Native at |
|---|---|---|
| OpenAI **Responses** (Open Responses is its open, stateless profile) | the Codex CLI | the ChatGPT Codex backend, the OpenAI API, llama.cpp, vLLM |
| **Chat Completions** | Open WebUI, dsh, the eval key, the reviewer bots | llama.cpp, vLLM, DeepSeek, the OpenAI API |
| Anthropic **Messages** | Claude Code (llm-router #128) | the Anthropic API, llama.cpp and vLLM (`/v1/messages`), DeepSeek (`/anthropic`) |

Open Responses (Jan 2026, now under the Agentic AI Foundation) is the nearest thing to a multi-provider standard, but
Anthropic and Google are not in it; its core forbids replaying plaintext reasoning and has only function tools; and its
compliance suite is 17 server-side smoke tests (§2.1).

**Recommendation: native per protocol, plus one lossless internal model for translation, plus state domains.**
1. **Three front doors, each native-first (D1).** `/v1/responses` and `/v1/chat/completions` as native-first already
   specifies, and **a native-first `/v1/messages` for the local routes only** (owner, §13), which replaces #128's
   translate-everything plan. A request whose
   protocol the target speaks is forwarded as bytes, with `model` patched and declared mutations only. Every production
   gateway that serves several protocols does exactly this: Envoy/Agent Router, agentgateway, LiteLLM (§2.7, §2.9).
2. **When the protocols differ, translate through one IR shaped like Open Responses items** (messages, reasoning, tool
   calls, tool results), following OpenAI Responses where the two differ. Provider-only state rides on the items as
   **opaque payloads tagged with a state domain** (an Anthropic signature or `redacted_thinking`, an OpenAI
   `encrypted_content`, a Gemini thought signature): the AI SDK's `providerMetadata` idea, applied to a wire router.
   Each pair is a decoder plus an encoder with a fidelity contract, qualified before use (D4). The IR replaces today's
   Chat-Completions-shaped `InferenceRequest`, which cannot carry any of this (§1.3).
3. **Reasoning state has a provenance, and a conversation that holds it is pinned to where it is valid.** The router
   records where each piece of state came from (a digest-only **provenance ledger**, or the MAC'd envelope below),
   because neither the bytes nor an upstream's acceptance prove it: local servers emit look-alike signatures, and OpenAI
   and Anthropic silently ignore state they cannot read. State is replayable where the compatibility key matches
   (producer, model family, credential scope from configuration, format: e.g. the Pro ChatGPT seats, on the 2026-10-12
   measurement). It is returned unchanged and never converted. **Agent routes always pin a conversation that holds
   such state to the seat that produced it, whatever the scope** (queue, then fail loudly). **Chat routes** may spill
   it to a seat with a matching key, where it travels as is, or move it anywhere else and strip it as a declared
   mutation (owner, §13). State of unknown origin is refused on agent routes. The router never invents a signature and
   never passes one model's reasoning off as another's (§4.3, §6).
4. **When a translated attempt produces opaque state in a format the client's slot does not hold, it reaches the
   client through that protocol's own opaque slot:** a versioned, key-identified, MAC'd router envelope in Responses
   `encrypted_content` or a Messages `signature`; OpenRouter-style `reasoning_details` on chat, opt-in. Envelopes are
   verified before routing and restored before any upstream sees them, on native paths too, and they bind the
   principal and conversation. It is a router-owned extension, qualified per client; LiteLLM runs the same mechanism in
   production (§2.5, §4.5). Nothing the estate runs today needs one, so it is built only with the first encoder that
   does (R5).
5. **Cache stability is an invariant, not an optimisation** (§4.7). The router never deletes, reorders or rewrites an
   earlier part of a conversation (reasoning, system text, tool items, images), in either direction and for any
   provider: every transformation is prefix-stable across turns. Measured on the production router on 2026-11-04: a
   sliding reasoning window (`replayReasoning: {lastTurns: 1}`) made flash-next and qwen3.8 (hybrid models with
   recurrent layers) re-prefill 50-80k tokens per turn, 777,850 uncached prompt tokens over 43 requests against about
   85k append-only, and a 15-minute agent task missed its cap. Context is bounded by the **client's** compaction, never
   by router trimming. This retires the sliding `reasoning-window`, the numeric `lastTurns`, and every hoist and merge.
6. **Tools and streams follow the same split** (§7, §8): function tools translate losslessly everywhere; namespaces
   flatten with a reverse map; grammar tools and hosted tools are refused on translation, by name; native streams are
   verbatim with a per-protocol tap; translated streams come from one terminal-state table that keeps the raw value.

**Why this and not a normaliser** (§3). The vendors now make the normaliser's bug class a protocol error, not just a
quality problem:
- **Anthropic.** On Claude 5.1+, a thinking block is invalid once a gateway changes `system`, `tools` or an earlier
  turn. Its gateway guidance says "Pass through what you don't recognize" and "Leave a `role: "system"` message where
  the caller put it".
- **OpenAI.** Reasoning is bound to a model family (documented) and to an org (reported).
- **DeepSeek.** It 400s if `reasoning_content` is missing from a tool loop.

None of these needs a new format; each needs the router to forward, and to know where state is valid (§2.3).

**How it sits with D1-D6.** It contradicts none of D1-D5, and amends D6 on new evidence:
- **D1:** native-first now covers Messages.
- **D2:** unknown fields are forwarded on every native path and refused by name on translated ones.
- **D3:** the served `model` is returned verbatim on every native path.
- **D4:** becomes checkable, and is extended, argued in §6 R5, from translators to reasoning state.
- **D5:** holds wherever the router owns the template. **One argued extension:** for a third-party API, mid-conversation
  system text is an encoder contract line, and that line is **in place** (Responses `developer` items; Anthropic
  inline `system` messages), never a hoist (§3, §6 R10).
- **D6, amended and confirmed by the owner (§4.7, §13):** D6 put a router-side bound (`lastTurns: N`, starting at N=1) on translated local paths. The
  2026-11-04 production measurement shows that a sliding bound defeats the upstream prompt cache. So replay is
  append-only (`lastTurns: "all"`, or llama.cpp's native `/v1/responses`) or off, fixed for the life of a conversation,
  and the context is bounded by client compaction (Codex `model_auto_compact_token_limit`, set against concurrent
  capacity: native-first's context gate, now for both paths). The live config already does this (rev 2228: cloud3
  `{lastTurns: "all"}`, cloud2 replay removed), and it passed the night's validation: P1 delivered its plan in about
  20 min with 67k uncached prompt tokens (§4.7). Native paths keep D6's "no router-side bound".

**What it finds in today's code beyond the local bugs** (§1.2):
- **The codex translated path neither asks for, keeps nor replays encrypted reasoning.** Chat and Assistant callers on GPT seats therefore
  lose their reasoning every turn.
- **The codex translated path folds every system/developer message into `instructions`.**
- **The `anthropic` adapter sends no `thinking` and hoists every system message.** Under the prefix check, that hoist
  would break thinking on Claude 5.1+.
- **The `openai` adapter uses Chat Completions.** For GPT-6 Astra and GPT-6.1 Sol, Chat Completions has no function
  calling.
- **llama.cpp b12020's `/v1/messages` has gaps a native Claude Code wire must qualify around:**
  - it ignores adaptive thinking, effort and `thinking: disabled`;
  - it reorders tool results after user text;
  - it drops the name from a forced `tool_choice`, and ignores `tool_choice: none` (tools stay callable),
    `disable_parallel_tool_use`, `strict` and `is_error`.

  vLLM 0.28 moves tool-result images into a separate user message and signs local thinking with random hex (§2.8).
- **The `claude` seat is a narrow translated target by the adapter's design** (text and function tools; no reasoning
  state), and stays one (§5.5).

**Migration** (§10): the native-first critical path stays first and unchanged. Then, in order:
0. **R0b**, now: the config validator refuses a numeric `lastTurns` (only `true`/`"all"` or off), and the native-first
   `reasoning-window` registry entry is withdrawn (§4.7).
1. **R1, R2, R3a**, with no behaviour change: the IR beside a shim, the provenance ledger and state facts in the
   journal, the scope declarations.
2. **R3b**, the pin, per route: the first behaviour change.
3. **R4**, the codex encoder: reasoning kept across turns, system text in place, images as parts. No envelope needed.
4. **R6a-R6c**, `/v1/messages` for the local routes, split along #128's prerequisites.
5. **Unscheduled:** R5 and R7 (no OpenAI or Anthropic API-key account is planned, owner §13), translated Messages pairs,
   Gemini.

**Tests** (§9):
- the golden pairs, with round-trip properties for every lossless line;
- render checks through the real template;
- the Open Responses suite and llm-comply as cheap first checks;
- then ≤ 15-minute real-client checks (Codex CLI, Claude Code, Open WebUI);
- **a direct-vs-router A/B for every provider** (deterministic diff plus a quality comparison against a predeclared
  margin), which gates every behaviour-changing step and canary, and a direct and routed re-run of the planning tasks
  on both local models (§9.6, §9.7);
- then long runs.

**Owner decisions** (§13, 2026-11-04): the D6 amendment confirmed; agent routes pin a conversation's state to its
seat, chat routes may move and strip (declared); no API-key accounts planned; `/v1/messages` for local routes only.

**Codex cross-validation** (§12): two rounds on the workstation (`gpt-6-astra`). Round 1: adopt with changes, three
blockers (provenance, envelope dispatch, Anthropic block order). Round 2, which also reviewed the cache-stability
requirement: adopt with changes, the D6 amendment judged sufficiently argued, two blockers left (provenance fallback,
envelope principal binding). Every finding of both rounds is answered in this version; the round-2 fixes are not yet
re-reviewed.

---

## 1. What the router serves today (llm-router `origin/main` `247b19f`, #136)

### 1.1 Front doors

| Front door | Clients in the estate | Path today | Code |
|---|---|---|---|
| `POST /v1/responses` | Codex CLI 0.166 (dev workers `dw-N`, `codex-reviewer`), `local-agent` | **Native** on Codex-native routes (`codexNative.routes` / `RoutePolicy.native`) to `codex` accounts; otherwise the **subset parser** (`parseRequest`), after `adaptCodexResponses` drops what the subset cannot carry | `api-openai/index.ts:391-428`, `protocol/index.ts` |
| `POST /v1/chat/completions` | Open WebUI, dsh, the platform eval key, reviewer bots on `pool-*` | Subset parser → canonical `InferenceRequest` → adapter | `api-openai/index.ts:800` |
| `POST /v1/images/generations`, `/v1/images/edits` | the Assistant, API callers | Image endpoint, OpenAI Images shape | `api-openai/index.ts:722-802`, `adapters/providers/images.ts` |
| **`POST /v1/messages` (Anthropic)** | (Claude Code) | **Does not exist.** llm-router #128 plans it as a translator (`parseMessages` → `InferenceRequest`, thinking blocks dropped) | issue #128 (2026-11-01) |
| canonical (in-process) | the Assistant (`agent-llm`, `chat-api`), admin bridge, `test_route` | `InferenceRequest` built directly | `contracts/index.ts:254` |

### 1.2 Upstream adapters

| Provider kind | Adapter | Upstream wire | What the translation does today | Reasoning / thinking state |
|---|---|---|---|---|
| `local` (llama.cpp b12020, vLLM 0.28 via llama-swap; cloud2 vLLM) | `chatcompletions.ts` | Chat Completions | canonical → chat; `developer`→`system` (`:64-65`); optional `systemMessages: merge`; neutral-value drops; `reasoning_effort` dropped unless `Account.reasoningEffort` (`compat:54`) | out: `reasoning_content` deltas when `forwardReasoning`; in: `Message.reasoning` → `reasoning_content` when `replayReasoning` (#135/#136) |
| `openai` (API key) | `chatcompletions.ts` | **Chat Completions** to `api.openai.com` | canonical → chat | none: Chat Completions has no reasoning item, so GPT reasoning never round-trips (§2.2) |
| `deepseek` (API key) | `chatcompletions.ts` | Chat Completions | `developer`→`system`; no seed/penalties | same flags as `local` |
| `codex` (ChatGPT subscription) | `codex.ts` | Responses, `chatgpt.com/backend-api/codex/responses` | **native:** body as sent with `model`, `store:false`, `stream:true` (`:471`). **Translated:** every system/developer message folded into `instructions` (`deriveInstructions`, `:252`, so mid-conversation text moves to the head); **no `include`**, reasoning output items are not kept (`runSse`: "other item types (message, reasoning, ...) need no state", `:577`), and none is replayed (`:364-376`); tool outputs with content parts are `JSON.stringify`-ed into a string (`common.ts:354`), so an image in a tool result becomes base64 text; output limit enforced by the router (`compat:55`) | native: verbatim; translated: **dropped every turn** |
| `anthropic` (API key) | `anthropic.ts` | Messages | every system/developer message folded into top-level `system` (`common.ts:269`); **no `thinking` parameter is sent, yet current models think by default** (Opus 5.5 and Sonnet 5.5 cannot turn it off, §2.3), **and the adapter discards the thinking blocks and signatures it receives** (`thinking_delta` ignored, `anthropic.ts:254`); `reasoningEffort: false` in the table (`compat:48`); no `cache_control`, so no prompt caching; `strict` tools refused; unknown `stop_reason` (`pause_turn`, `model_context_window_exceeded`, ...) → `stop` (`anthropic.ts:81-93`); cache tokens folded into input (`:219`) | none |
| `claude` (Pro/Max seat) | `provider-claude` + `claude-sdk` / `seat-host` | the **Claude Code CLI** (`print` single-turn, or `sdk-resume`: history written as a synthetic transcript JSONL, tools exposed through a schema-only MCP bridge, effort and output limit as env vars) | text only, by the pinned adapter's choice: images, schemas, `temperature`, forced tool choice refused (`claude-sdk/index.ts:45-54`). These are adapter and estate-policy restrictions, not CLI limits (the CLI has `--json-schema`, for one) | none: "thinking and internal records are never passed on" (`seat-host/index.ts:100`); the synthetic transcript has no thinking blocks to replay |
| `claude-seat` | `claude-seat.ts` | an external bridge (`/infer`, canonical in, router events out) | single-turn | none |
| image | `images.ts` | OpenAI Images (`/images/generations`) | prompt/size/seed; edits load validated inputs | n/a |

**The live estate** (runbook): `codex-*` ChatGPT seats, `claude-*` Claude seats on the `sdk-resume` transport, and the two
local accounts (cloud3 `lan-192.168.0.26-8080`, cloud2). The `openai`, `anthropic` and `deepseek` API-key providers are
supported but not configured in the runbook. The design covers them because the router ships them.

### 1.3 The internal model is the root limitation

The canonical request is a Chat-Completions-shaped subset:
- `Message { role, content, tool_calls?, tool_call_id?, reasoning?: string }` (`contracts/index.ts:245`);
- events `text | tool | refusal | reasoning(text) | image | usage | finish(reason) | native` (`:310`).

It has no place for:
- **an origin:** which provider, account and model produced a piece of reasoning;
- **opaque state:** an Anthropic `signature`, a `redacted_thinking` payload, an OpenAI `encrypted_content` and its item
  id, a Gemini thought signature;
- **more than one reasoning block per assistant turn, or their order** relative to text and tool calls (interleaved
  thinking);
- **tool kinds other than `function`:** custom/freeform, namespaces, hosted tools;
- **item ids** and tool-result content parts, including images.

So every translator built on it loses the same things, for every provider: this week's local bugs (native-first §1), and
on the remote side the codex-translated, `anthropic` and `claude` rows above. Native pass-through removes the problem
where the protocols match. Where they do not, **the internal model has to become lossless before a translator can be.**
That is the gap this design fills.

---

## 2. Is there a standard? What the web and the sources say

### 2.1 Open Responses: a vocabulary, not yet a standard to build on alone

Accessed 2026-11-04 unless dated.

- **What it is.** "an open-source specification and ecosystem for building multi-provider, interoperable LLM interfaces
  based on the OpenAI Responses API" ([openresponses.org](https://www.openresponses.org/)) [doc]. Launched 2026-01-15
  ([Hugging Face](https://huggingface.co/blog/open-responses),
  [Simon Willison](https://simonwillison.net/2026/jan/15/open-responses)) with Hugging Face, OpenRouter, Vercel, LM
  Studio, Ollama and vLLM as partners; Anthropic and Google are not
  ([InfoQ, 2026-02-02](https://www.infoq.com/news/2026/02/openai-open-responses/)).
- **Versions and governance** [src]. Two dated spec releases, 2026-01-15 and 2026-04-24 (the second added WebSocket
  transport, `POST /v1/responses/compact` and the assistant `phase` field;
  [CHANGELOG](https://raw.githubusercontent.com/openresponses/openresponses/main/CHANGELOG.md)); no tags. An LF project
  under a TSC charter adopted 2026-10-21; contributed to the Agentic AI Foundation on 2026-10-22
  ([SD Times](https://sdtimes.com/aaif/61588/)). Nine maintainers (OpenAI ×2, Hugging Face ×2, Databricks, Amazon,
  Ollama, OpenRouter, Ramp); none from vLLM, llama.cpp, Anthropic or Google. ~75 commits, last 2026-10-29, nearly all by
  one author; several substantive issues unanswered for months (#12, #42, #50, #51, #56, #63, #86).
- **Relation to OpenAI Responses** [spec]: a compliant API "implements this spec … or is a proper superset of Open
  Responses". In practice it has forked in places: its reasoning stream events are `response.reasoning.delta/.done`
  where OpenAI's (and llama.cpp's) are `response.reasoning_text.delta` (PR #42 to align, open since 2026-02-02);
  `"item": null` is allowed in `output_item.*` (#86); required fields differ from the OpenAI SDK (#63). It has no
  `conversation` parameter and never says "stateless" (#50 asks for a stateless tier).
- **Reasoning** [spec/src]. "Providers MAY surface the model's raw reasoning trace directly through the `content`
  field"; `encrypted_content` is opaque ("Clients are expected to treat this field as opaque and non-inspectable; it is
  intended … to be round-trippable"); all three fields optional. **But the input `ReasoningItemParam` declares
  `content?: null`: raw reasoning cannot legally be sent back** (schema/items.tsp; issue #12 / PR #13 open since
  2026-01-17). That is exactly what Codex, llama.cpp and vLLM do (native-first §5), and what local models need (§1 of
  native-first). Nothing says what to do with another provider's encrypted content.
- **Extensibility** [spec]: "New item types that are not part of this specification MUST be prefixed with the
  implementor slug" (e.g. `openai:web_search_call`); clients "SHOULD be prepared to encounter unknown item types";
  extended events are slug-prefixed and ignorable. Extra fields "MUST NOT alter required core behavior".
- **Tools** [src]: the core tool union is **`function` only**; hosted tools are extensions ("Hosted tools must have
  corresponding item types"). `function_call_output.output` may be a string or `input_text`/`input_image`/`input_file`
  parts. `tool_choice` includes `allowed_tools`, which "Servers MUST enforce … as a hard constraint".
- **Streaming** [spec]: "The stream MUST include a terminal response event (`response.completed`, `response.failed`, or
  `response.incomplete`)"; "Any error incurred while streaming will be followed by a `response.failed` event"; `[DONE]`
  optional; every event has `sequence_number`. Whether `response.completed` may carry `status: incomplete` (vLLM's
  behaviour) is not specified.
- **The compliance suite** [src]: `src/lib/compliance-tests.ts`, 17 tests (basic response, phase, text-only streaming,
  seven WebSocket cases, system prompt, tool calling, which checks only that a `function_call` item appears, base64 image
  input, multi-turn, compaction). Mostly Zod schema checks. **Nothing on reasoning, structured output or a tool
  round-trip.** Runs as `bun run test:compliance --base-url http://localhost:8080/v1 --api-key X --model <name>`, so it
  can target the router or a local server; servers only; its SSE parser overwrites multi-line `data:` fields (#56).
  An independent MIT suite, **llm-comply**, covers Open Responses (16), OpenAI Responses (12), Chat Completions (13),
  Anthropic Messages (19) and Gemini, each with `--base-url`
  ([README](https://raw.githubusercontent.com/Oaklight/llm-comply/master/README_en.md)) [src].
- **Who implements it**:

  | Implementer | Claim | Known gaps |
  |---|---|---|
  | Vercel AI Gateway | "supports the OpenResponses API specification" [doc, 2026-09-08] | extends requests with `providerOptions` |
  | Databricks | `/serving-endpoints/open-responses`, Claude and Gemini behind it [doc, 2026-10-02] | stateless; function tools only; Gemini needs `encrypted_content` on `function_call` items (a non-core field) |
  | LM Studio 0.3.39 | "compatible with the Open Responses specification" [doc, 2026-01-15] | no published results |
  | OGX (ex-Llama Stack) | "100% compliance" [doc, 2026-03-20] | on the old 6-test suite |
  | OpenRouter, Hugging Face, Ollama ≥0.13.3 | an "OpenAI-compatible Responses API" | stateless; `previous_response_id` rejected |
  | vLLM | RFC to align (#33381) closed stale 2026-06-21 [issue] | |
  | llama.cpp | `/v1/responses` "(partial)" (#18486) [src]; a compliance PR (#21174: "sequence_number to ALL streaming events", "24 missing fields") closed unmerged 2026-09-19 | |
  | LiteLLM, NVIDIA NIM | no claim found | |

  The project's own capability matrix (#80) is a proposal: "Provider supporters … must not automatically be presented as
  implementations."

**Verdict on Open Responses.** It is the right *vocabulary* for the router's internal item model and stream (items,
reasoning with `content`/`summary`/`encrypted_content`, slug-prefixed extensions, a terminal-event rule), because the
router's two most important upstream kinds and its most important client already speak its parent dialect. It is not a
complete canonical model: its core forbids replaying plaintext reasoning, has only function tools, and names reasoning
events differently from every implementation the router talks to. And its suite is a smoke test, not a conformance
suite. So: **adopt the item model and the extension rule, follow OpenAI Responses (the superset Codex, llama.cpp and vLLM
actually implement) where the two differ, and run the suite as a cheap first check (§9.2), not as qualification.**

### 2.2 OpenAI Responses and Chat Completions as de-facto standards

- **Responses** is the de-facto agent protocol: Codex speaks nothing else since `wire_api = "chat"` was removed in
  rust-v0.95.0 (native-first §5.3); llama.cpp, vLLM, Ollama, LM Studio, OpenRouter and Hugging Face all serve a stateless
  variant (above); it is the only one of the three with typed reasoning items, opaque state, item ids and a semantic
  stream. Its stateful features (`previous_response_id`, `store`, `conversation`) are the part nobody else implements,
  and the router already refuses them.
- **Chat Completions** is the de-facto lowest common denominator: every local server and most chat UIs speak it, and
  `reasoning_content` is a de-facto extension (llama.cpp, vLLM, DeepSeek). But it has no slot for opaque reasoning, and
  OpenAI itself is moving agents off it ("Chat Completions does not support function calling with GPT-6 Astra or GPT-6.1
  Sol", §2.3). It is a fine wire to chat servers and from chat UIs; it is the wrong *internal* model, which is the
  router's position today (§1.3).

### 2.3 What the vendors require of whoever sits in the middle

All accessed 2026-11-04. Anthropic quotes are verbatim from the pages' markdown; the OpenAI, Vertex, DeepSeek and codex-rs
quotes passed through a summarising fetch and are near-verbatim.

**Anthropic Messages** ([thinking](https://platform.claude.com/docs/en/build-with-claude/thinking),
[preserved thinking](https://platform.claude.com/docs/en/build-with-claude/preserved-thinking),
[streaming](https://platform.claude.com/docs/en/build-with-claude/streaming),
[stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)) [doc]:
- **Signed state.** A `thinking` block's `signature` is "an encrypted copy of the full reasoning that you pass back
  unchanged". `display: "omitted"`, the default on Opus 4.7+ and the 5.x models, returns `thinking: ""` with only the
  signature. `redacted_thinking.data` goes back the same way; "Filtering on `block.type == "thinking"` alone silently
  drops `redacted_thinking` blocks and breaks the multi-turn protocol."
- **When it must come back.** "Required: within a tool-use turn, pass thinking blocks back. Recommended: across turns,
  pass everything back. Allowed: outside tool use, omit prior turns' thinking." Within the latest assistant message the
  blocks "must match what the model generated… you can't rearrange, edit, or partially drop them". Opus 4.5+, Sonnet
  4.6+ and Haiku 5.5 keep all prior turns' thinking; older models strip it automatically.
- **The prefix check (Claude 5.1+).** A block stays valid "only while the top-level `system` prompt, the `tools`, and
  the messages before it are unchanged" ("JSON formatting and key order don't matter; the values do"). For gateways:
  "Pass through what you don't recognize… return `input_transformations`"; "Leave a `role: "system"` message where the
  caller put it"; to turn tools off "send `tool_choice: {"type": "none"}`. Don't remove `tools`." Accounts created on or
  after 2026-08-31 get a 400 on a mismatch ("The block is bound to a different conversation"). **So a router that
  hoists a system message, drops a tool or rewrites an earlier turn now breaks Claude's thinking outright.** That is the
  vendor stating native-first's rule 1 as a protocol requirement.
- **`input_transformations` exists** (beta `thinking-binding-controls-2026-08-01`): "a top-level `input_transformations`
  array on every response", entries such as `{type: "thinking_dropped", path, reason: "prefix_binding_mismatch" |
  "model_binding_mismatch" | "organization_binding_mismatch"}`. The vendor itself discloses what it dropped.
- **Portability.** "`signature` values are compatible across platforms, including the Claude API, Amazon Bedrock, and
  Claude Models on Google Cloud… except that thinking blocks from Claude Sonnet 5.5 and Claude Haiku 5.5 work only in the
  account that produced them or in an account linked to it." Across models, "the API ignores or drops the blocks the
  target model can't read": silent, not an error.
- **Effort and thinking.** `output_config.effort` = `low|medium|high|xhigh|max`; adaptive thinking makes interleaving
  automatic; models from 4.7 on reject `thinking.type: "enabled"` with a 400. Thinking is **on by default** in the
  Messages API, and whether it can be turned off depends on the model: "Claude Fable 5.1, Claude Mythos 5.1, Claude Fable
  5, Claude Mythos 5, Claude Opus 5.5, and Claude Mythos Preview reject `thinking: {type: "disabled"}`"; Sonnet 5.5
  rejects it too and its lowest setting is `between_tools` (progress updates still come back); Opus 5 and Haiku 5.5
  accept `disabled` only at effort `high` or below. The Models API reports `capabilities.thinking.types.*.supported`
  ([thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)).
- **Tools.** `tool_result` blocks "must come FIRST in the content array" of the user message; results may hold `text`,
  `image`, `document`, `search_result`; `disable_parallel_tool_use`; `strict: true` on the tool; Opus 5.5, Sonnet 5.5,
  Fable 5.1 and Mythos 5.1 reject a forced `tool_choice` with a 400. Server tools (`web_search`, `web_fetch`,
  `code_execution`, `tool_search`) run at Anthropic, and their results carry `encrypted_content` "that you must pass
  back". The MCP connector (`mcp_servers` + an `mcp_toolset` tool, beta) supports tool calls only.
- **Stream.** `message_start` → `content_block_*` → `message_delta` → `message_stop`, `ping` anywhere, `event: error`
  mid-stream (`overloaded_error`), one `signature_delta` "just before the `content_block_stop`", and "new event types may
  be added… handle unknown event types gracefully". Stop reasons: `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`,
  `pause_turn`, `refusal` (HTTP 200), `model_context_window_exceeded`; "No other stop reason values are documented."
- **Its OpenAI compatibility layer is not a target.** It is "primarily intended to test and compare model capabilities,
  and is not considered a long-term or production-ready solution"; "The response doesn't include Claude's thinking";
  system/developer messages "are hoisted and concatenated into the top-level `system` field"; `strict` and
  `reasoning_effort` are ignored; no prompt caching
  ([OpenAI SDK compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)).
- **Claude Code's gateway contract** ([LLM gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol.md)):
  `/v1/messages` (posted with `?beta=true`) and `/v1/messages/count_tokens` ("the only optional" one); `/v1/models` for
  discovery; "Forward `anthropic-version` and `anthropic-beta` unchanged… don't allowlist individual values"; "pass
  `anthropic-*` request headers and request body fields through unchanged"; do not reshape the `system` array; forward
  error bodies unmodified (Claude Code's retries match on their wording); do not buffer the stream or drop `ping` events.
  And: Anthropic "doesn't support routing Claude Code to non-Claude models through any gateway." **#128's translator
  plan breaks most of this contract** (it drops betas, rewrites `system`, drops thinking). A native front door keeps it.

**OpenAI** ([reasoning guide](https://developers.openai.com/api/docs/guides/reasoning),
[Responses create](https://developers.openai.com/api/reference/resources/responses/methods/create),
[Chat create](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)) [doc]:
- With `store: false` (or ZDR), reasoning items "include an `encrypted_content` property by default" (the `include`
  parameter is still accepted). Pass back "all reasoning items, function call items, and function call output items,
  since the last `user` message". "Persisted reasoning can be reused only within the same model family"; "the API omits
  incompatible reasoning" silently.
- **Org binding [anecdote]:** LiteLLM's 2026-02-24 incident report quotes `invalid_encrypted_content`, "Encrypted
  content organization_id did not match the target organization."
  ([LiteLLM](https://docs.litellm.ai/blog/responses-api-encrypted-content-incident)). The runbook's Codex routes already
  assume account binding.
- `reasoning.context` (`auto|current_turn|all_turns`; `all_turns` default on GPT-5.6) and `phase`
  (`commentary|final_answer` on assistant message items: "If you replay assistant history manually, preserve each
  original `phase` value") both exist. Dropping `phase` "can make preambles count as final answers".
- **Chat Completions carries no reasoning state** in either direction (only `reasoning_tokens` in usage), and "Chat
  Completions does not support function calling with GPT-6 Astra or GPT-6.1 Sol" (the codex review also found it in the
  [latest-model guide](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.1-sol); third-party relay
  docs report the same 400). **For GPT models, Chat Completions is not a viable upstream wire for agents.** Today's
  `openai` provider uses it.
- Responses tool types: `function`, `custom` (`text`, or `grammar` with `lark|regex`), `namespace`, `tool_search`,
  `web_search`, `mcp`, `apply_patch`, `shell`, `local_shell`, `file_search`, `code_interpreter`, `image_generation`,
  `computer`, `programmatic_tool_calling`. Terminal events `response.completed`, `response.incomplete`,
  `response.failed`, plus `error`; every event carries `sequence_number`
  ([openai-python types](https://github.com/openai/openai-python/tree/main/src/openai/types/responses)) [src].
- **The ChatGPT Codex backend** has no public documentation. codex-rs (`main`) [src]: base
  `https://chatgpt.com/backend-api/codex`, `store: false`, `stream: true`, `include: ["reasoning.encrypted_content"]`,
  no `max_output_tokens` and no `previous_response_id`. LiteLLM's ChatGPT provider [src, 3rd party]: "The Codex backend
  400s on {"type": "message", "role": "system"} items ("System messages are not allowed") but accepts role=developer".

**Google Gemini** ([Vertex thought signatures](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures))
[doc]: with parallel calls "only the *first* `functionCall` part will contain the `thought_signature`"; "If a required
thought signature is not returned when using Gemini 3 models, the model will return a `400` error"; the escape hatch is
`skip_thought_signature_validator`, "a last resort as it will negatively impact model performance"; on the
OpenAI-compatible endpoint a tool call carries `extra_content.google.thought_signature`. No Gemini provider exists in
the router; the IR has the slot.

**DeepSeek** ([thinking mode](https://api-docs.deepseek.com/guides/thinking_mode),
[Anthropic API](https://api-docs.deepseek.com/guides/anthropic_api)) [doc]:
- Without `tools`, `reasoning_content` "does not need to be passed back; even if passed to the API, it will be ignored".
  **With `tools`, "the reasoning_content must be fully passed back to the API in all subsequent requests"**, new user
  turns included; otherwise "the API will return a 400 error". So DeepSeek with tools needs every reasoning block back,
  which append-only replay (§4.7) gives anyway.
- DeepSeek also serves a **stateless Responses API** at `https://api.deepseek.com`
  ([Responses API](https://api-docs.deepseek.com/guides/responses_api/)): reasoning items accepted on input, their
  plaintext "merged into the adjacent assistant message"; `encrypted_content`, `summary` generation, `store` and
  `previous_response_id` unsupported; `function` tools, a `custom` tool limited to `apply_patch`, `web_search` on
  `deepseek-v4-pro` only; terminal events `response.completed | incomplete | failed`; and **"Unsupported parameters are
  silently ignored rather than causing errors"** (e.g. `include`, `truncation`, `service_tier`), and **"`developer`
  is treated as `user`"** (`instructions` becomes the first system message), so a native wire there must list those as
  qualification losses.
- `https://api.deepseek.com/anthropic` is an Anthropic-compatible endpoint: thinking blocks supported (pass back the full
  assistant content in a tool loop), `redacted_thinking` "Not Supported", signatures not mentioned, `any` treated as
  `auto`, `cache_control` / `is_error` / `mcp_servers` / `container` ignored, `anthropic-version` ignored.

**Qwen3.6** ([model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)) [doc]: by default "only the thinking blocks
generated in handling the latest user message is retained"; `chat_template_kwargs: {"preserve_thinking": true}` keeps
the rest. (Qwen3.8's GGUF template renders past reasoning by default: native-first §1.)

**State that must round-trip, by vendor:**

| Vendor | State | Must come back | Portable | If dropped or changed |
|---|---|---|---|---|
| Anthropic | `thinking.signature` (text may be empty), `redacted_thinking.data` | within a tool-use turn always; all turns recommended | across platforms; across models silently dropped where unreadable; Sonnet/Haiku 5.5: the producing (or linked) account only | 400 "cannot be modified"; on 5.1+ a changed prefix → 400 "bound to a different conversation" (newer accounts) |
| Anthropic | server-tool `encrypted_content`, `encrypted_index` | later turns | — | 400 |
| OpenAI | reasoning item `encrypted_content` (+ `phase` on messages) | since the last user message (all turns under `all_turns`) | same model family; org-bound [anecdote] | omitted silently, or `invalid_encrypted_content` |
| Gemini | `thoughtSignature` | current turn's function calls | Flash-Lite cannot read others' | 400; escape value |
| DeepSeek | `reasoning_content` | every later request when tools are present | — | 400 |
| Qwen (self-hosted) | `reasoning_content` | template decides (`preserve_thinking`) | — | silently not rendered |

### 2.4 The Vercel AI SDK provider spec (LanguageModelV3)

Read at `@ai-sdk/anthropic@3.0.138` / `ai@6.0.322` (V3) and `main` `27c44878` (2026-11-04; `main` has moved to `v4/`)
[src]. Quotes came through a summarising fetch; re-check against the raw files before an ADR cites them.

- **Model.** Prompt roles `system` (a string), `user` (`text | file`), `assistant` (`text | file | reasoning | tool-call |
  tool-result`), `tool` (`tool-result | tool-approval-response`). Every part and message carries `providerOptions`;
  generated parts carry `providerMetadata` (`Record<providerName, JSONObject>`). The reasoning part is
  `{type: 'reasoning', text, providerOptions?}`.
- **Round trip.** `to-response-messages.ts` copies `providerOptions: part.providerMetadata` back onto reasoning, text and
  tool-call parts, and keeps empty-text reasoning parts (an Anthropic `redacted_thinking` becomes `text: ''` with
  `{anthropic: {redactedData}}`; a signature becomes `{anthropic: {signature}}`; OpenAI reasoning carries `itemId` and
  `reasoningEncryptedContent`; Google carries `thoughtSignature` on text, reasoning and tool-call parts).
- **Stream.** `stream-start{warnings}`, `text-*`, `reasoning-*` (with `providerMetadata`, so a `signature_delta` is a
  `reasoning-delta` with empty text), `tool-input-*`, `tool-call`, `tool-result`, `finish{usage, finishReason}`,
  `raw`, `error`. **V3's `finishReason` is `{unified, raw}`** and its usage is
  `inputTokens{total, noCache, cacheRead, cacheWrite}`, `outputTokens{total, text, reasoning}`.
- **Foreign reasoning on the next request:**
  - to Anthropic: parts without an `anthropic` signature or `redactedData` are dropped with the warning
    `'unsupported reasoning metadata'`;
  - to OpenAI Responses: "Non-OpenAI reasoning parts are not supported. Skipping reasoning part:"; with `store: false`
    "Reasoning parts without encrypted content are not supported when store is false.";
  - to OpenAI chat: dropped silently (no `reasoning` case);
  - **to Gemini: sent as `thought: true` text with no signature, no warning**, and on Gemini 3 an unsigned tool call gets
    `skip_thought_signature_validator` plus a warning;
  - **to OpenAI-compatible servers: appended to `reasoning_content`, whatever produced it, no warning.**

**What to take:** provider-keyed opaque metadata on every part, warnings for every drop, a `{unified, raw}` finish
reason and a split usage record. **What not to take:** its OpenAI-compatible and Gemini converters pass another
provider's reasoning on as the target model's own, silently; and as a model it is a client SDK's, which rebuilds requests
(option D, §3).

### 2.5 LiteLLM: the closest prior art for state, from the incidents it had

Read at `main` `b18188de` (2026-11-04) [src] and its docs.
- **Internal format.** OpenAI chat messages plus `reasoning_content`, `thinking_blocks` (`{type: "thinking", thinking,
  signature}` / `{type: "redacted_thinking", data}`), `reasoning_items` (`{id, encrypted_content, summary}`) and
  `provider_specific_fields` (`litellm/types/utils.py`). Gemini thought signatures ride in `provider_specific_fields`, or
  inside the tool-call id after a `__thought__` separator, so they survive clients that drop unknown fields.
- **Opaque state in another protocol's opaque slot.** OpenAI `encrypted_content` travels inside an Anthropic block's
  `signature` or `data`, prefixed `litellm_encrypted_reasoning:`, because "Anthropic clients echo a thinking block's
  `signature` … verbatim". **That is the router envelope of §4.5, already in production elsewhere.**
- **Affinity = a state domain.** `encrypted_content_affinity` wraps item ids as `encitem_` + base64(model id) and prefixes
  `encrypted_content` with `litellm_enc:`, then routes follow-ups back to the deployment that produced them. It came from
  an incident: "Encrypted content organization_id did not match the target organization."
  ([2026-02-24](https://docs.litellm.ai/blog/responses-api-encrypted-content-incident)).
- **Strip, never convert.** `_replayable_thinking_blocks` drops blocks with "a null, empty, or missing signature (e.g. from
  an open-source reasoning model)", and blocks carrying another provider's encrypted reasoning, because "keeping its text
  as an unsigned thinking block only moves the 400 from the router to the provider". PR #40280 (merged 2026-09-09): when
  the pool has no deployment of the origin model group, it strips `encrypted_content` and `id` and keeps the readable
  summary; unavailability inside the origin group "keeps the existing 429/503 fail-fast".
- **The bridge mints no ids:** "No item carries an `id`: the Responses API 404s on any id it did not mint itself."

LiteLLM reached state domains, pinning, envelopes and strip-on-failover one incident at a time. The router can start
from where it ended up, and make each of those a declared, tested rule rather than a fix.

### 2.6 OpenRouter, Portkey, LangChain

- **OpenRouter** [doc]: `reasoning_details` entries `reasoning.summary{summary}`, `reasoning.encrypted{data}`,
  `reasoning.text{text, signature}`, each with `id`, `format`, `index`; formats include `openai-responses-v1`,
  `anthropic-claude-v1`, `google-gemini-v1`, `xai-responses-v1` and `unknown`. The sequence "must match the outputs
  generated by the model during the original request". It documents nothing for reasoning of a different format, nor for
  reasoning under model fallback. Its Responses API is "stateless" (`store: true` and `previous_response_id` are 400s).
  Its AI SDK provider drops `anthropic-claude-v1` and `google-gemini-v1` text entries that lack a signature [src].
  `reasoning_details` is the most widely used chat-side extension for opaque reasoning, which is why §4.5 uses its
  shape.
- **Portkey** [src, `main` `669825cb`]: Anthropic assistant `content_blocks` are copied through unchanged, so another
  provider's thinking blocks reach Anthropic as they are (a likely 400, inferred); thinking output needs
  `strict_open_ai_compliance: false` (beta). No reasoning-aware fallback.
- **LangChain v1** standard content blocks: `ReasoningContentBlock{reasoning, extras}` with provider data in `extras`;
  `ChatAnthropic` sends a reasoning block only when `model_provider == "anthropic"` [src].

### 2.7 Infrastructure gateways: routing over bodies, not a semantic standard

Accessed 2026-11-04; years marked * were inferred from GitHub's day-month display.

- **Kubernetes Gateway API Inference Extension.** v1.6.2 (2026-09-17*). v1.6.0 moved the full Endpoint Picker, Body-Based
  Routing and the latency predictor to llm-d, and removed `InferenceObjective` (ex-`InferenceModel`); GAIE keeps the
  `InferencePool` API and a lightweight EPP for conformance
  ([v1.6.0](https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/tag/v1.6.0)) [doc]. The EPP parses
  exactly one of Completions, ChatCompletions, Responses, Conversations or Embeddings; **no Anthropic Messages**
  ([types.go @v1.5.0](https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.5.0/pkg/epp/framework/interface/scheduling/types.go)) [src].
  BBR's `bodyfieldtoheader` copies a top-level field into `X-Gateway-Model-Name` and **does not mutate the body** [src];
  GKE notes headers may not be injected above ~250 KB
  ([GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-body-based-routing)) [doc].
  **Relevance here: low** [opinion]. `InferencePool` selects pods; the estate's servers are VMs/LXCs on another cluster, and
  the EPP's value (load-aware choice among replicas of one model) needs replicas. What it contributes is the principle:
  route on the body, do not rewrite it.
- **Envoy AI Gateway, now Agent Router** (v1.2.0, 2026-10-07*) [doc]: client endpoints `/v1/chat/completions`,
  `/anthropic/v1/messages` (+ `count_tokens`), `/v1/responses` (to OpenAI-compatible backends only), embeddings,
  completions ([supported endpoints](https://theagentrouter.ai/docs/capabilities/llm-integrations/supported-endpoints)).
  Translators are pairwise files (`anthropic_openai.go`, `openai_gcpanthropic.go`, …); **no Responses↔Anthropic
  translator exists** ([internal/translator](https://github.com/envoyproxy/ai-gateway/tree/main/internal/translator))
  [src]. Same-schema routes, OpenAI→OpenAI and Anthropic→Anthropic alike, are the original bytes with one
  `sjson.SetBytesOptions(original, "model", …)` patch ([anthropic_anthropic.go](https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/internal/translator/anthropic_anthropic.go))
  [src]. Anthropic client → OpenAI-compatible backend: thinking history goes as structured content parts "to preserve
  thinking blocks", and replies "Prefer ThinkingBlocks (structured, preserves signatures)", else "Fall back to
  ReasoningContent (used by AWS Bedrock, Gemini, Qwen, DeepSeek)", which yields **unsigned** thinking blocks
  ([openai_helper.go](https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/internal/translator/openai_helper.go)) [src].
  Retries are re-translated per attempt ([data plane](https://theagentrouter.ai/docs/1.0/concepts/architecture/data-plane)).
- **agentgateway** (v1.7.0, 2026-10-27*) [doc]: route types `completions`, `messages`, `responses`, `realtime`,
  `embeddings`, `models`, `anthropicTokenCount`, `gemini`, `passthrough`. A Messages request goes to a provider that
  speaks Messages "natively, with no conversion"; otherwise to Responses, then Chat, then Bedrock Converse. And the
  losses are documented per path: through Responses it "drops extended-thinking history" and "the reply has no `thinking`
  block"; through Chat it "carries extended-thinking history in both directions" as `reasoning_content`, but "The
  signature is carried only when the turn has exactly one signed block", using a non-standard `reasoning_signature`
  field, and `redacted_thinking` is "Dropped"
  ([messages](https://agentgateway.dev/docs/standalone/main/documentation/llm/api-types/messages.md)).

The two production gateways that serve all three protocols draw the same line as this design: **native same-schema
pass-through with a model patch; translation pairwise, with signed thinking carried only where a slot exists, and the
losses documented.** Neither publishes a qualification suite; neither pins conversations to a state domain.

### 2.8 The local servers speak all three protocols, by converting to chat

| Server | Chat | Responses | Messages | Thinking signatures it emits | Source |
|---|---|---|---|---|---|
| llama.cpp | yes | `/v1/responses` "(partial)", #18486 (2026-01-21) | `/v1/messages` + `count_tokens`, #17570 (2025-11-28) | `""` (non-streaming `{"signature", ""}  // empty signature for local models`; an empty `signature_delta` because "Anthropic API requires a signature_delta before closing thinking blocks") | `server-task.cpp` L734-755, L921-930 (b12020 = b12110 here) [src] |
| vLLM | yes | yes | `/v1/messages` + `count_tokens`, #22627, in v0.11.1 | `uuid.uuid4().hex`, a random value that looks like a signature | `vllm/entrypoints/anthropic/serving.py` L646, L704 @v0.28.0 [src] |
| Ollama | yes | ≥0.13.3, stateless | ≥0.14.0 (2026-01-16) | not documented; `budget_tokens` "accepted but not enforced" | [docs](https://docs.ollama.com/api/anthropic-compatibility) [doc] |
| LM Studio | yes | 0.3.29 (stateful) | 0.4.1 (2026-01-30) | not documented | [changelog](https://lmstudio.ai/changelog/lmstudio-v0.3.29) [doc] |
| DeepSeek (cloud) | yes | yes, stateless, plaintext reasoning only | `api.deepseek.com/anthropic` | thinking blocks (signatures not documented); `redacted_thinking` unsupported | [Anthropic API](https://api-docs.deepseek.com/guides/anthropic_api), [Responses API](https://api-docs.deepseek.com/guides/responses_api/) [doc] |

What llama.cpp b12020 and vLLM 0.28 do with a Messages request, read in source here (`server_chat_convert_anthropic_to_oai`,
`../server-chat-b12020.cpp` L353-L650; vLLM `anth-serving.py` @v0.28.0):
- **llama.cpp:** `thinking` blocks → `reasoning_content`, `signature` never read, `redacted_thinking` dropped silently;
  only `thinking.type == "enabled"` maps (to `thinking_budget_tokens`), so Claude Code's `adaptive` and
  `output_config.effort` are ignored; top-level `system` blocks are concatenated with no separator; within a user
  message, the converted text is emitted **before** the `tool` messages built from its `tool_result` blocks (L536-554),
  so Claude Code's "results first, then a system-reminder text" becomes text first; every tool, server tools included, is
  converted to a function tool by name (L560-580); `tool_choice: {type: tool}` becomes `required` (the name is lost, L582-590);
  **`tool_choice: {type: none}` is not mapped at all**, so the request runs with the default (`auto`) and tools stay
  callable; `disable_parallel_tool_use`, a tool's `strict`, `tool_result.is_error` and `thinking: {type: disabled}` are
  not read either (found by the codex review, confirmed at L582-590 and L614-621); `id_slot` and `chat_template_kwargs`
  pass through. `stop_reason` is only `end_turn`/`tool_use`/`max_tokens` (a stop sequence reads as `end_turn`,
  `server-task.cpp` L734-737).
- **vLLM 0.28:** `thinking` → `reasoning`, `redacted_thinking` skipped (`pass`, L361-366); images in a `tool_result`
  are **moved into a separate user message after the tool message** (L406-450), and `is_error` is not read;
  `disable_parallel_tool_use` (L543) and a tool's `strict` (L580) are kept; `output_config.effort` →
  `reasoning_effort` (L513-514); role `system` allowed inside `messages` (`protocol.py` L70), and **merged into the leading
  system block when the template rejects an inline one** (`_detect_merge_inline_system` renders `[system, user, system,
  user]` against the template, L144-171). Once D5's template fix is live, that check passes and vLLM keeps inline system
  messages in place: D5 fixes this path too.

**Claude Code against them** [doc]: Anthropic "doesn't support routing Claude Code to non-Claude models through any
gateway" ([llm-gateway](https://code.claude.com/docs/en/llm-gateway)), but it is designed to survive some rejections: on
a rejected thinking signature it "removes earlier thinking blocks… retries", and on a rejected mid-conversation
`role: "system"` it "retries once without those"
([protocol](https://code.claude.com/docs/en/llm-gateway-protocol.md)). Claude Code ≥ 2.1.154 broke older vLLM validation
("Input should be 'user' or 'assistant'"; [NVIDIA forum](https://forums.developer.nvidia.com/t/claude-code-2-1-154-breaks-with-vllm-anthropic-compatible-endpoint-on-dgx-spark/371797))
[anecdote]; 0.28 accepts the role. vLLM has a Claude Code guide
([docs](https://docs.vllm.ai/en/latest/serving/integrations/claude_code/)) [doc].

**Consequence for the router:** a non-empty signature proves nothing. The domain of a thinking block is known from where
the router sent the turn (§4.3), never from its bytes; a local server's thinking blocks are plaintext state and must not
reach Anthropic.

### 2.9 What the research settles

1. **No single wire standard exists for all three client protocols, and none is coming soon.** Open Responses is the
   nearest, and Anthropic and Google are not in it.
2. **Every production gateway that serves several protocols forwards same-schema requests natively, with one `model`
   patch** (Envoy/Agent Router, agentgateway, LiteLLM's `supported_endpoints`, native-first §4), and translates pairwise
   otherwise, with documented losses.
3. **Opaque reasoning state is never converted between providers.** Everyone who handles it correctly replays it to its
   producer and drops it elsewhere (AI SDK, LiteLLM, LangChain); the vendors require exactly that (§2.3). The mistakes on
   record are converting it (Vercel's OpenAI-compatible and Gemini paths, Portkey's pass-through) or losing it on a move
   (LiteLLM before its affinity fix, the router's codex-translated path today).
4. **State has a domain, and a router must know it before it routes.** LiteLLM learned this from an incident; Anthropic's
   prefix and account binding (2026-08) and OpenAI's family and org binding make it a protocol fact.
5. **Local servers' signatures are placeholders** (`""`, random hex). The domain must come from where the router sent the
   turn, not from the bytes.
6. **Conformance suites are thin.** Open Responses has 17 server-side smoke tests; llm-comply adds Anthropic and chat
   coverage; nobody publishes translation golden tests. The router's golden corpus remains the real qualification.

---

## 3. Options and the recommendation

The question has two halves, and the alternatives differ on each:
- **the wire:** what the router speaks to each client and each upstream;
- **the internal model:** what a translation goes through when the two wires differ.

| Option | Wire | Internal model | For | Against | Verdict |
|---|---|---|---|---|---|
| **A. Today** | two front doors, translate to each upstream | a Chat-Completions-shaped subset (`InferenceRequest`) | one engine; any client to any target | lossy by construction (§1.3): no origin, no opaque state, one reasoning string per message, function tools only. This week's bugs, and on the remote side the codex-translated and `anthropic` losses | reject as the internal model |
| **B. Native-first (adopted, D1)** with A as the fallback | native wherever the protocols match | A, as the fallback | removes translation where most traffic is | leaves the fallback lossy; says nothing about Messages, remote state or failover across providers | **keep; this design completes it** |
| **C. One front protocol** (e.g. everything speaks Responses; Claude Code only through a translator) | one | any | one parser | Claude Code's contract is Messages, and Anthropic's gateway rules (§2.3) are violated by any translation; Open WebUI and dsh speak chat | reject |
| **D. AI SDK V3 as the internal model**, possibly its provider packages as adapters | translate always | `LanguageModelV3` prompt and stream parts | proven across 30+ providers in TypeScript; reasoning parts with `providerOptions`/`providerMetadata` carry signatures and encrypted content (§2.4) | it is a **client SDK's** model: providers rebuild requests from it, so it is a normaliser (option A's bug class) for anything it does not model (Codex `namespace` and `additional_tools`, llama.cpp reasoning items, Anthropic betas); importing its packages makes the router's upstream bodies a dependency's choice | **borrow its `providerMetadata` idea; do not adopt it as the model or as adapters** |
| **E. LiteLLM's format, or a gateway in front** (LiteLLM, Envoy AI Gateway, agentgateway) | the gateway's | OpenAI chat + bolt-ons (`reasoning_content`, `thinking_blocks`, `provider_specific_fields`) | moves code out | chat-shaped, so lossy where it matters; documented bridge bugs (native-first §4.1); Envoy's and the Inference Extension's model is body-based routing over OpenAI shapes, not a semantic standard (§2.7); an extra hop that cannot see the router's placement and budgets | reject (as native-first did) |
| **F. Native per protocol + an Open Responses item IR with domain-tagged opaque state** | three front doors, native where the protocols match (D1) | Open Responses items (OpenAI Responses where they differ) + `Opaque {domain, format, value}` + state domains + a router envelope for the protocols' opaque slots | lossless where state is portable, loud where it is not; the Responses encoder is near-identity; the vendors' own rules (§2.3) become checkable invariants; one vocabulary shared with the upstreams and Codex | the IR is new code; three front doors to keep qualified | **recommended** |

**Why F.**
1. **The vendors now require it.** Anthropic's prefix check makes thinking blocks invalid when a gateway changes
   `system`, `tools` or an earlier turn, and its gateway guidance is "pass through what you don't recognize"; OpenAI says
   to preserve `phase` and reasoning items; DeepSeek 400s without `reasoning_content` in tool loops (§2.3). A router that
   rebuilds bodies from a lossy model cannot meet those rules; a router that forwards natively and translates through a
   lossless model can, and can prove it in tests.
2. **The state problem is a domain problem, not a format problem.** No format makes an OpenAI ciphertext readable by
   Anthropic or a Qwen thought valid for Claude. The industry answer is the same everywhere it is written down: carry it
   opaque, return it unchanged to its producer, drop it (declared) elsewhere (AI SDK, OpenRouter, Anthropic's own
   `input_transformations`; §2.4-2.6). F makes the domain explicit, so routing can keep a conversation where its state
   is valid instead of discovering the loss afterwards.
3. **The Responses item model is the only one rich enough, and it is what the upstreams speak.** Chat cannot hold the
   state; Messages has no item ids and no opaque slot for non-Anthropic state; Responses has both, and llama.cpp, vLLM
   and Codex already exchange plaintext reasoning in it. Open Responses gives the extension rule (slug-prefixed item
   types) for what the core lacks.
4. **It extends D1-D5 without contradicting them.**
   - D1: native-first now covers the third protocol (`/v1/messages`).
   - D2: unknown fields are forwarded on every native path; the IR refuses them on translated paths (as native-first
     §7.4 already says).
   - D3: the served `model` verbatim on every native path, Messages included.
   - D4: "qualified translators only" is now checkable: a translator is a decoder + an encoder over one IR, each with a
     fidelity contract and a golden suite, and state-domain rules decide failover eligibility.
   - D5: unchanged where the router owns the template. **One argued extension:** for a third-party upstream the template
     is not ours, so mid-conversation system text becomes an encoder contract line, lossless where the API allows it in
     place (Responses `developer` items; Anthropic's `role: "system"` messages, which its gateway guidance says to leave
     where the caller put them) and declared where it does not (§6 R10). The router still never merges silently.

---

## 4. The architecture

### 4.1 Four layers, one decision per attempt

```
 front doors            facts (read-only)        routing                  per attempt: wire plan              transports
 ───────────            ─────────────────        ───────                  ──────────────────────              ──────────
 /v1/responses   ─┐                                                       native?  bytes, patched `model`  ─► protocol transport
 /v1/chat/compl. ─┼─► envelope + facts ───────► alias → chain ──► target ─┤          (D1, D2, D3)              (one generic byte
 /v1/messages    ─┤   (protocol, tools/kinds,    gates on facts           │                                     loop + a tap per
 canonical (IR)  ─┘   images, state domains,     placement + domain pin   ├► translate? decoder(client)→IR     protocol)
                      output limit, effort)      (§6)                     │            →encoder(target)       ─► upstream wire
                                                                          │            (qualified pair only, D4)
                                                                          └► neither?  skip, reason named
```

- **Front doors.** Three wire protocols plus the in-process canonical caller. `/v1/messages` is new (§4.6), and
  native-first like the other two.
- **Facts.** Read-only, per protocol, never a body. They add **reasoning state and its provenance** to the
  native-first facts (§4.3): which state the request carries, whose it is (from the ledger), and every envelope,
  verified (§4.5).
- **Routing.** Unchanged in kind. Gates read facts; placement is extended so that a conversation holding opaque state is
  pinned to its domain (§6).
- **The wire plan, per attempt.** Native when the target speaks the client's protocol (native-first §7). Otherwise a
  translation **through the IR**, from a decoder for the client's protocol to an encoder for the target's, and only for
  a qualified pair on routes that require one (D4). Otherwise the target is skipped, with the reason named.
- **Transports.** One byte-preserving, backpressured loop (native-first §7.3) with a protocol-specific tap: Responses
  events, chat deltas, Anthropic SSE.

### 4.2 The IR: Open Responses items plus provider-scoped opaque state

Replace `Message[]` (§1.3) with an item list. The item and event vocabulary is Open Responses' (§2.1), because it is the
only one of the three that already has first-class reasoning items, tool-call items with ids, and a semantic stream.
Anthropic-only and Gemini-only state rides on the items as **opaque payloads tagged with a state domain**. That is the
AI SDK's `providerOptions` / `providerMetadata` idea (§2.4) and OpenRouter's `reasoning_details.format` (§2.6), applied
to a wire router.

```ts
// packages/contracts (IR v2). A sketch, not a spec.
type Domain =                            // who can validate or consume a piece of opaque state (§4.3)
  | { kind: 'plaintext'; scope: string } // reasoning text a chat template renders (llama.cpp, vLLM, DeepSeek); scope = reasoning family
  | { kind: 'openai'; scope: string }    // encrypted_content: the seats (codex) or org (API) that can read it
  | { kind: 'anthropic'; scope: string } // thinking signature / redacted_thinking
  | { kind: 'gemini'; scope: string };   // thought signatures (no provider today; the slot exists)

interface Provenance {                   // who produced a piece of state; from the ledger or an envelope (§4.3, §4.5)
  domain: Domain; producer: string /* provider kind */; accountId: string; modelFamily: string; format: string }

interface Opaque { provenance: Provenance; format: 'openai.encrypted_content' | 'anthropic.signature'
                 | 'anthropic.redacted_thinking' | 'gemini.thought_signature'; value: string; ids?: Record<string, string> }

type Part = { type: 'text'; text: string }
          | { type: 'refusal'; text: string }                                     // a refusal is content, not a finish reason
          | { type: 'image'; url: string; detail?: 'low' | 'high' | 'auto' }      // data: or https:
          | { type: 'file'; name?: string; url: string; mediaType: string };

// Every item records where it came from in the client's body, so an encoder can rebuild the target's message and block
// boundaries exactly (Anthropic's prefix binding compares the values of every earlier block, §2.3).
interface At { msg: number; block?: number }     // source message index, and block index within it (Messages, chat parts)

type Item = (
  | { type: 'message'; role: 'system' | 'developer' | 'user' | 'assistant'; parts: Part[]; id?: string; phase?: string }
  | { type: 'reasoning'; id?: string; text?: string; summary?: string[]; opaque?: Opaque;
      provenance: Provenance | 'unknown' } // set by the decoder from the ledger or an envelope, never from the bytes (§4.3)
  | { type: 'tool_call'; callId: string; id?: string; kind: 'function' | 'custom'; name: string; namespace?: string;
      input: string /* JSON text for function, free text for custom */; opaque?: Opaque }
  | { type: 'tool_result'; callId: string; parts: Part[]; isError?: boolean }
  | { type: 'hosted'; domain: Domain; raw: unknown }    // web_search_call, server_tool_use, mcp_call...: native only
) & { at: At };

type Tool = { kind: 'function'; name: string; namespace?: string; description?: string; parameters: object; strict?: boolean }
          | { kind: 'custom'; name: string; description?: string; format?: { type: 'text' } | { type: 'grammar'; syntax: string; definition: string } }
          | { kind: 'hosted'; domain: Domain; raw: unknown };

interface IRRequest {
  model: string; items: Item[]; tools: Tool[];
  toolChoice?: 'auto' | 'none' | 'required' | { name: string; namespace?: string }; parallelToolCalls?: boolean;
  controls: { maxOutputTokens?: number; temperature?: number; topP?: number; topK?: number; stop?: string[]; seed?: number;
              effort?: 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'; thinking?: 'on' | 'off' | 'adaptive';
              responseFormat?: object };
  providerOptions?: Record<string, Record<string, unknown>>;  // canonical callers only, keyed by provider family
}
```

**Rules of the IR.**
1. **Order and boundaries are the conversation.** Items stay in the order the client sent them, and each keeps its
   source position (`at`). A decoder may decompose a message into items (an Anthropic assistant message of
   `thinking, text, tool_use, thinking, tool_use` becomes five items), but never reorders or merges them. An encoder
   rebuilds the target's messages from the boundaries, and groups adjacent items only where the target's grammar requires
   it (chat: an assistant message with its tool calls; Anthropic: every tool result first in the next user message).
   Each grouping is a contract line with a render check (native-first Step 1, item 3).
2. **Opaque state is never decoded, edited or invented.** An encoder re-emits an `Opaque` only to a target in the same
   domain, byte for byte. To any other target it is foreign (§6).
3. **Plaintext reasoning is scoped too.** `reasoning.text` with no `opaque` goes as `reasoning_content` to targets of the
   same plaintext scope (a declared reasoning family, e.g. `qwen3.8` on both local accounts if the owner wants spills
   between them to carry reasoning). It never becomes visible assistant text, never becomes a signed block, and is never
   handed to a model of another family as that model's own reasoning: the AI SDK's OpenAI-compatible converter does that
   silently, and it is the trap §2.4 records.
4. **Unknown fields are not carried.** The IR is for translation only. A client field with no IR slot is refused by name
   on a translated attempt (native-first §7.4, D4). The native path forwards it (D2).
5. **A refusal is content.** `refusal` parts stay typed (chat `refusal`, Responses `refusal` content, Anthropic's
   `refusal` stop reason with its text). The terminal status and the finish reason are separate fields (§8 S3).
6. **The IR's stream is Open Responses' stream:** `output_item.added/done`, `output_text.delta`, `reasoning_text.delta`,
   `function_call_arguments.delta`, a terminal `completed | incomplete | failed` with the reason. The Responses encoder is
   therefore close to the identity, and the chat and Anthropic encoders are mapping tables (§8).

**What this replaces in the code.** `Message.reasoning?: string` (the #135/#136 stopgap's slot) becomes reasoning items;
the `reasoning` event gains an item index and an opaque terminator; `tool_calls: unknown[]` becomes `tool_call` items with
a kind and a namespace. The Assistant constructs IR v2 directly. An `IRv2 → v1` shim keeps today's adapters running
during the migration (§10, Step R1).

### 4.3 State domains

| Domain | State | Produced by | Valid at | Evidence the facts extractor uses |
|---|---|---|---|---|
| `plaintext` | reasoning text (`reasoning_content`; Responses reasoning `content[].text` with empty or no `encrypted_content`; an Anthropic-shaped `thinking` block from a local server or DeepSeek) | llama.cpp, vLLM, DeepSeek | targets of the same declared reasoning family (default: the served model) | the shape, **and** the conversation's bound target: llama.cpp signs Anthropic thinking blocks with `""`, vLLM with a random `uuid4().hex` (§2.8), so a non-empty signature is not proof of Anthropic |
| `openai` | reasoning items with `encrypted_content` (and their ids), same model family only | the ChatGPT Codex backend, the OpenAI API with `store:false` | **codex:** measured portable between ChatGPT Pro seats (runbook, 2026-10-12, the README fix in #101; Pro ↔ Pro Lite still unmeasured). **API:** the producing org [anecdote, §2.3]. Never a local server (vLLM 400s on non-empty `encrypted_content`, native-first §5.2) | non-empty `encrypted_content` on a native Responses path; the conversation's placement binding gives the scope |
| `anthropic` | `thinking` + `signature`, `redacted_thinking.data` | the Anthropic API. (DeepSeek's `/anthropic` thinking blocks are **DeepSeek plaintext state**, not Anthropic state: their signatures are undocumented and they are not valid at Anthropic, §2.8) | the Anthropic API, Bedrock and Google Cloud alike (signatures are portable across platforms); a model that cannot read a block drops it silently; Sonnet/Haiku 5.5 blocks only in the producing or a linked account (§2.3) | Messages blocks with a signature, on a conversation bound to an Anthropic-family target |
| `gemini` | thought signatures on parts | Gemini | Gemini | (no provider today) |

**A scope is configuration, set on evidence.** Each account declares `stateScope` (default: its own id). It says
where a seat's state is valid. **Only chat routes use it to move state:** there, accounts with the same scope may
exchange opaque state, so a chat route's pool can spill within it. **Agent routes never move state, whatever the
scope.** They pin a state-holding conversation to the seat that produced it (§6 R5, owner §13). Today's evidence would put the Pro
`codex-*` seats in one scope and leave the Pro Lite seats (`codex-3`, `codex-4`) in their own until measured; each
Anthropic API account serving Sonnet/Haiku 5.5 is its own scope (§2.3). Widening a scope is an owner decision backed by a
measurement, like the 2026-10-12 one.

**Provenance comes from the router, never from the bytes, and never from acceptance.** Opaque payloads carry no
readable origin; local servers emit look-alike signatures; and upstreams *accept* state they cannot use (OpenAI omits
incompatible reasoning silently, Anthropic drops unreadable blocks, llama.cpp and vLLM ignore signatures, §2.3, §2.8). So
"the first target that accepts it" proves nothing. The router establishes provenance in two ways:
- **The provenance ledger** (new, small, in the router's store): when a native tap or a translated encoder sees a
  reasoning item or block leave an upstream, it records `sha256(value or text) → {domain, scope, producer, accountId,
  modelFamily, format}`, keyed by conversation. Digests only: no content enters the ledger. It survives restarts.
- **Retention follows the conversation's replay lifetime, not placement's.** Placement forgets a conversation after
  24 idle hours (`placement/index.ts:4`); a Codex session resumed after a day off (`codex resume`) still replays every
  item. So a ledger entry is refreshed whenever a replayed item hits it, and expires only after the conversation has
  been idle for `ledgerRetentionDays` (default 30; a few hundred digest rows per conversation). A resumed conversation
  whose placement expired is **re-bound from the ledger** to the seat that produced its state, and on an agent route it
  is pinned there (§6 R5). Only beyond retention is the state unknown-origin: an agent route then refuses with
  `reasoning_unknown_origin` and names the remedy (`/compact` in Codex, which replaces the replayed history [infer], or a
  new session) until the client takes it; a chat route strips once and increments the epoch. With a 30-day default
  that is the rare case, not a session resumed after a day off.
- **The envelope** (§4.5), for state the router itself translated: its provenance is inside the MAC.
- **What is digested, and when.** On a native Responses stream the tap hashes each `response.output_item.done`
  reasoning item's `encrypted_content` (or, when empty, its `content[].text`); on a native Messages stream, each
  `thinking` block's signature and text, and each `redacted_thinking.data`; on a translated attempt the encoder records
  what it emitted. On a request, the facts extractor hashes the replayed items the same way and looks them up. That is a
  few hundred SHA-256 calls over a few hundred kilobytes per turn, and one small row per item.
- **Provenance is only ever observed or authenticated.** A ledger entry is written only from the router's own
  observation of an upstream's output (the native tap, non-streaming JSON included; for native chat, the
  `reasoning_content` of the assistant message; on translated paths, the encoder), or read from a verified envelope.
  A binding never stands in for it: a client can append imported ciphertext to a conversation that has always used one
  key, and no placement history can tell.
- **Write ordering: durable per item, before the item becomes replayable.** A client can keep a state item as soon as
  the event that completes it arrives, long before the terminal event, so committing at the terminal event is too late:
  a disconnect or crash in between would leave replayable state the ledger lacks. The tap (or, on a translated path, the
  encoder) therefore commits each item's digest durably (one store transaction) **before releasing the event that
  completes it**, and holds only that event (and, for signed state, the final fragment) while it writes:
  - **opaque state:** Responses `response.output_item.done` carrying `encrypted_content`; Anthropic `signature_delta`
    and the `content_block_stop` after it; a `redacted_thinking` block, which arrives whole in its `content_block_start`;
  - **plaintext reasoning:** deltas flow as they come; the completing event (`response.output_item.done`, Anthropic
    `content_block_stop`, the chat chunk carrying `finish_reason`) is held until the digest of the full text is durable.
    An **open-item row** (conversation, seat, the item id where the protocol has one) is written before the item's
    first event is released, so a fragment a client kept from an interrupted stream is attributed to its seat
    (`provenance: interrupted`, journaled), not left unknown.

  **Held, never rewritten:** a held event is released byte for byte as received; only its timing changes. Native-first
  §7.3 permits timing-only differences for the start-of-stream hold; this per-item hold is a second one, added to the
  same list, and the verbatim-modulo-declared stream test covers it. **If the write fails,** the held event is never
  released: the stream ends with the protocol's error event (`response.failed`, Anthropic `event: error`, a chat error frame), journaled as
  `ledger_write_failed`, before the item became replayable. **Crash consistency:** a crash after the commit and before the
  release leaves a row for state the client never received (harmless; it expires with retention); a crash before the
  commit releases no completing event (an open-item row covers any plaintext fragment), so the client sees a broken stream and retries the turn (Codex: "stream closed before
  response.completed"). **The hold rule and the failover boundary** (native-first §7.3, §9.1): releasing a held
  completing event is a flush like any other. If it is the attempt's first, the attempt commits there; a write failure
  before the first flush has delivered nothing, so the attempt may still fail over, and one after it ends the stream.
  The cost is one local store write per state item, a few milliseconds.
- **Legacy state** (forwarded before the ledger existed: every Codex session in flight the day R2 ships) is a ledger
  miss. It is not promoted. During R2-R3a it is journaled as `provenance: legacy`; R3b treats it as unknown-origin,
  except on routes the owner lists for a bounded transition as `legacyState: 'allow'`, journaled as weaker assurance.
  **R3b's readiness is measured, not timed:** a route is enforced once the journal shows its replayed opaque items hit
  the ledger (say ≥ 99 % over a day of its traffic), or once its legacy sessions have been drained or compacted.

**The ledger key, and what an envelope binds.** Both use the composite `(namespace, principalId, conversation,
epoch)`. Today's `conversationHash` hashes only the client-supplied id (`api-openai:46`), while placement keys add the
principal and namespace (`inference:318`); provenance follows placement, not the bare hash. Callers without a
conversation id (most chat clients, canonical callers) are keyed by `(namespace, principalId)` and the item digest
alone. A fork (`parentHash`) inherits its parent's entries when the parent's provenance verifies under the same
principal. The route is **not** part of the key: a route change to a compatible target restores the state; an
incompatible one refuses or strips it (§6 R5). `epoch` increments at every declared reset (§4.7), so state from before a
reset is never silently mixed with state after it.

**Compatibility is a key, not an account.** State is replayable to a target when `(producer kind, model family,
credential scope, format)` all match: the provider's rule (§2.3), the account's declared `stateScope`, and the served
model's declared family (OpenAI's "same model family" rule; Anthropic's model-readability matrix).

**Unknown-origin state** (no ledger entry and no envelope: a ledger that expired, a history pasted from elsewhere) is
foreign everywhere. On agent routes the request is refused (`reasoning_unknown_origin`); on other routes the declared
`strip-foreign-state` applies if the route opts in, else the same refusal. Plaintext reasoning of unknown origin on a
plaintext target is the one exception the owner may allow per route (`plaintextUnknown: 'replay'`), since that is what a
client talking to llama.cpp directly does.

### 4.4 Wire declarations

The native-first `wire` declaration (native-first §7.1) gains `protocol: 'messages'`. Everything else is unchanged: per
account and model, a qualification manifest entry, `allowLoss`, `holdMs`, route-scoped activation (`nativeWires`).

| Provider | `responses` | `chat` | `messages` |
|---|---|---|---|
| `local` llama.cpp b12020 | native (qualify, native-first Step 5) | native | native (`/v1/messages`, qualify; §2.8 lists its gaps) |
| `local` vLLM 0.28 | native (qualify) | native | native (`/v1/messages`, qualify) |
| `codex` | native (today) | — | — |
| `openai` | **add** (native) | native | — |
| `anthropic` | — | — (the compatibility layer is not a target, §2.3) | native |
| `deepseek` | native (stateless; §2.3 lists the silently ignored parameters) | native | — (it has `/anthropic`, §2.8, but Messages is for local routes only, owner §13) |
| `claude` (Claude Code CLI) | — | — | — : a **translated-only** target with a narrow contract (§5.5) |

### 4.5 Opaque state through a client protocol that has a slot for it

When a translated attempt produces opaque state, the client must get it back in a field it will replay, or the next turn
loses it. Each protocol has exactly one such slot:

| Client protocol | Slot the client replays | What the encoder puts there |
|---|---|---|
| Responses | the reasoning item's `encrypted_content` (Codex replays it verbatim, for any provider, native-first §5.3) | a **router envelope** (below). The readable thinking text goes in `content[].text`, as a local server's does. |
| Messages | `thinking.signature`, `redacted_thinking.data` | the same envelope, when the upstream is not Anthropic; an Anthropic upstream's blocks pass natively |
| Chat Completions | none in the standard. De facto: OpenRouter's `reasoning_details` (§2.6), which clients that append the returned message object as-is (the OpenAI SDKs' usual loop) replay; `reasoning_content` for plaintext | `reasoning_content` for plaintext; `reasoning_details` entries in OpenRouter's shape for opaque state, if and only if the route opts in. Open WebUI's *Default* provider setting replays neither (native-first §10.5), so for it the state is lost, and that loss is declared, not hidden |

**The envelope.** `llmr1.<kid>.<b64url(payload)>.<b64url(mac)>`, where `payload` is
`{v: 1, kid, binding: {namespace, principalId, conversation, epoch}, provenance: {domain, scope, producer, accountId,
modelFamily}, state: {type: 'thinking' | 'redacted_thinking' | 'reasoning', format, itemShape, value, ids, textDigest},
issuedAt}` and the MAC (HMAC-SHA256 under a router key,
OpenBao-held) covers the version, the key id and the whole payload. It preserves what reconstruction needs: the original
state type, its format, the upstream's ids, and a digest of the readable text that must accompany it (an Anthropic
`thinking` block is valid only with its exact text). The MAC gives integrity, not confidentiality: the vendor's opaque
value is already encrypted by the vendor, and `accountId` is an opaque router handle, never a credential or an email.

**The envelope contract:**
1. **Recognised and verified before routing, on every path.** The facts extractor finds envelopes in the request
   (Responses `encrypted_content`, Messages `signature` / `data`), checks size bounds first (a payload over the bound is
   rejected unparsed), then the MAC, then the provenance. Only then does routing see the domain.
2. **An envelope never reaches an upstream.** If the attempt goes to a compatible target (§4.3), the wire plan restores
   the original. On a native path that is the declared request mutation `restore-envelope`, and it is a **byte
   replacement only when the original format and the enclosing item's shape both match the client's item** (an
   envelope holding an OpenAI `encrypted_content` inside a Responses reasoning item, going to a Responses target).
   Otherwise (an Anthropic signature inside a Responses item) the attempt must take a qualified translated path that
   rebuilds the original item from `itemShape`, `ids` and the accompanying text, whose digest must match `textDigest`
   (mandatory for Anthropic `thinking`), or it is refused. Any other target makes it foreign state (§6 R5). The check
   runs per attempt, so it holds when a route's configuration changes between turns.
3. **Distinct outcomes, distinct reasons:** `envelope_tampered` (MAC fails: always a 400, never stripped);
   `envelope_key_retired` (a key id the router no longer verifies); `envelope_conversation_expired` (the conversation
   has been idle past its replay lifetime); `reasoning_foreign:<domain>` (valid, but not for this target). **There is
   no per-item expiry:** an item's age never matters, because expiring old items one by one would delete history from
   the front, turn after turn, which is a sliding window (§4.7).
4. **Origin is not permission.** A valid envelope proves where state came from; it grants nothing. Key, route and
   namespace authorisation apply as for any request, and an envelope copied into another principal's or namespace's
   request, or another conversation's, is foreign there: the MAC covers `binding`, which is checked against the
   request's own `(namespace, principalId, conversation)`.
5. **Rotation without stranding.** New envelopes use the newest key. A key stays verifiable until every conversation
   that holds an envelope signed by it has been idle past the replay lifetime, measured from the conversation's **last
   use**, not from the key's creation (the ledger records the key id per conversation). The only exception is a
   compromised key: retiring it is a declared reset. Recovery from a reset is **one** explicit conversation-level event
   (a refusal that says to compact or start a new session, or, where the route allows it, one `strip-foreign-state` of
   everything the reset invalidated, followed by an `epoch` increment), never a rolling per-item strip.
6. **Readers before writers.** The verifier ships, and stays, before any encoder emits envelopes (§10 R5a/R5b).
   Turning emission off is the rollback; reading stays on as long as envelopes may be in flight.
7. **It is a router-owned extension, qualified per client.** It relies on the client replaying the opaque slot
   verbatim. Codex does (native-first §5.3); Claude Code replays signatures; each client is qualified in §9.3 before a
   route emits envelopes to it.

This is the protocols' own opaque slot, used for what it is for (state the client must return and must not read), so it
is not a workaround. It is prior art, not a standard: LiteLLM does the same in production (`litellm_encrypted_reasoning:`
inside an Anthropic `signature`, `litellm_enc:` and `encitem_` ids for affinity, §2.5), and Envoy and agentgateway carry
signatures in non-standard chat fields for the same reason (§2.7). That precedent shows the mechanism works with real
clients. It does not show that every client preserves the slot.

### 4.6 `/v1/messages`: native-first, replacing #128's translator plan

#128 planned a Messages front door that translates everything to chat and drops thinking blocks. Under D1 it becomes:
- **native** to the local Messages-speaking targets only (owner, §13): llama.cpp `/v1/messages` and vLLM `/v1/messages`
  (both convert to their chat path next to the template, as their `/v1/responses` do), each after its quirks below are
  qualified. Not DeepSeek's `/anthropic`, and not the Anthropic API (no API account is planned, and #128 keeps the
  subscription direct);
- **translated** through the IR only to qualified pairs, and refused on agent routes otherwise (D4);
- **not** to the `claude` (Claude Code CLI) provider, by estate policy: #128 keeps Claude Code on the subscription
  directly, so a Claude Code process routed back into a Claude Code CLI run would only add a lossy hop (a second
  process, not a routing cycle).

#128's codex-review requirements are kept as they are: they are transport requirements (the `x-api-key` authenticator,
the body cap and admission budget, Anthropic's error envelope before and after headers, the telemetry protocol union,
the Messages-shaped `/v1/models`, `X-Claude-Code-Session-Id` into placement, `count_tokens`), and they apply to a native
front door just as much. Its protocol-module requirements (findings 3-13) apply only to the translated path.

**What a native local Messages target does with what Claude Code sends** (from source, §2.8; to be pinned by
qualification):

| Claude Code sends | llama.cpp b12020 `/v1/messages` | vLLM 0.28 `/v1/messages` |
|---|---|---|
| `thinking` blocks on replay | → `reasoning_content` (signature ignored) | → `reasoning` (signature ignored) |
| `redacted_thinking` | dropped | skipped |
| `thinking: {type: adaptive}`, `output_config.effort` | **ignored** (only `type: enabled` maps, to a budget) | effort → `reasoning_effort` |
| `thinking: {type: disabled}` | **ignored** (the template's default applies) | to be pinned |
| top-level `system` blocks | concatenated **with no separator** | joined |
| a `system`-role message inside `messages` | passed on as a system message (Qwen template raises: D5) | **merged into the leading system block when the template needs it** (auto-detected): the lossy merge D5 retires, done upstream |
| `tool_result` blocks followed by text in one user message | the text message is emitted **before** the tool messages (order changed) [src] | to be pinned |
| server tools (`web_search_*`, ...) | turned into plain function tools by name [src] | to be pinned |
| `tool_choice: {type: tool, name}` | → `required` (the name is lost) | kept (L532) |
| `tool_choice: {type: none}` | **not mapped: tools stay callable** | to be pinned |
| `disable_parallel_tool_use`, a tool's `strict` | not read | kept (L543, L580) |
| `tool_result` with images | kept as image parts in the tool message | images **moved to a separate user message** after the tool message |
| `tool_result.is_error` | not read | not read |
| `id_slot`, `chat_template_kwargs` | passed through (`id_slot` is operator-reserved, native-first §7.2) | `chat_template_kwargs` passed |
| output thinking blocks | signature `""` | signature `uuid4().hex` |
| `stop_reason` | `end_turn` / `tool_use` / `max_tokens` only (a stop sequence reads as `end_turn`) | to be pinned |

So a native Messages wire to llama.cpp b12020 would qualify, at best, for plain Claude Code sessions, with the order,
`tool_choice`, `is_error` and thinking-control losses declared and the request-sensitive ones (`tool_choice: none`,
`disable_parallel_tool_use`, `strict`) checked per request: a request that uses one is not eligible for that wire. Those are upstream bugs to report, and
the reason qualification, not the protocol name, decides eligibility.

### 4.7 The cache-stability invariant

**Requirement (owner, 2026-11-04, from a production measurement).** Every upstream caches the prompt prefix it has
seen: llama.cpp's slot cache and context checkpoints, vLLM's prefix cache, OpenAI's automatic prefix caching (keyed by
`prompt_cache_key`), Anthropic's `cache_control` breakpoints. Every one of them reuses work only up to the first
changed token. Hybrid models with recurrent layers (flash-next, qwen3.8) are the extreme case: llama.cpp cannot rewind
their recurrent state to an arbitrary point, so a change anywhere before the end of the previous prompt forces a
re-prefill from the last usable checkpoint before it, which in an agent session is tens of thousands of tokens.

**Measured tonight** on the production router (coordinator's report): with `replayReasoning: {lastTurns: 1}`, the
previous turn's reasoning is removed from every request, and flash-next and qwen3.8 re-prefilled 50-80k tokens per
turn: **777,850 uncached prompt tokens over 43 requests, against about 85k** with no replay or with append-only replay.
A 15-minute agent task then failed its cap. Append-only history (`lastTurns: "all"`, or llama.cpp's native
`/v1/responses`) kept the cache and passed. Live config rev 2228 now runs cloud3 with `{lastTurns: "all"}` and cloud2
with replay removed.

**Validated the same night, on that config** (coordinator's report):
- **P1 through the production router**, append-only replay on cloud3: the plan was delivered at about 20 min and the
  session ended at 21.2 min. 37 requests, 46.6k output tokens, 124k peak context, **67k uncached prompt tokens**. (Earlier
  that day, through the router with replay dropped, it delivered no plan in 90 min; native-first §12.1.)
- **The journal shows `input:reasoning_replayed:1` through `:36`:** every turn's reasoning replayed, none dropped.
- **The 12-file non-regression task passed in 10.1 min.** Its append-only baseline is 75k uncached tokens over 51
  requests, only 2 of them over 3k.
- **One failed `apply_patch` write:** the first attempt lacked `*** End Patch`, and the model recovered on the next turn.
  That is a model slip handled by the client's tool loop, not a router effect.

**The invariant.** For consecutive requests of one conversation to one upstream, under a fixed encoding and replay
policy ("the policy epoch", below), the upstream request for turn N+1 contains the upstream request for turn N as a
prefix at complete message and turn boundaries, by value (system text, tools, every earlier item), and appends only.
Formally, for a router transformation `f` and a client history `h` extended by `δ`: `f(h) ≼ f(h ⊕ δ)` at those
boundaries. The encoding of a new suffix may depend on earlier context; the encoding of earlier parts may not depend on
what came after them, on the turn count, or on the position from the end. Cache annotations (Anthropic `cache_control`
markers) are excluded from the comparison: moving a breakpoint forward is legitimate and is not part of thinking-prefix
validation (§2.3).

**Three contracts, tested separately** (§9.1):
1. **content:** every earlier part the router submitted is submitted again, unchanged in value;
2. **rendered tokens:** for local targets, the token sequence the template produces for turn N (images and other
   multimodal inputs included) is a prefix of turn N+1's;
3. **the generated reply:** the next prompt equals the previous prompt **plus the tokens the model generated**, except
   at boundaries the qualification lists (a template that re-renders the reply, the generation prompt). This depends on
   what the client replays, and is where replaying the model's own reasoning matters. For recurrent models a mismatch
   inside the reply can force a restore from a much earlier checkpoint (llama.cpp's checkpoint and full-prefill
   fallbacks, `server-context.cpp` L4105, L4252, L4292, per the codex review), so it is not bounded by one reply in
   general; tonight's no-replay arm happened to stay near 85k.

**Concretely, the router must never, on any path, in either direction, for any provider:**
- **delete** an earlier part: reasoning (a sliding window), a tool, a tool result, an image, a system message;
- **reorder** one: hoisting a mid-conversation system message, moving images after a run of tool results;
- **rewrite** one: re-serialising differently from the previous turn, changing an id, re-mapping a value by a rule that
  can change between turns (a translator upgrade mid-conversation, an effort or thinking mode switched mid-conversation:
  Anthropic re-caches on any thinking-configuration change, §2.3).

**Both directions.** The response side decides what the client stores and replays, so it decides the next request's
prefix:
- native: verbatim (D1), so the client replays exactly what the upstream produced;
- translated: the client must receive everything it has to replay, in a form that decodes back to the same upstream
  items. A router that drops part of a reply (today: reasoning after visible output on translated Responses streams,
  `api-openai:490-492`; reasoning when `forwardReasoning` is off; an output-limit or drain cut) breaks contract 3 at the
  latest reply on every turn. That is a loss to fix (R4, R8), and for recurrent models it is not cheap in general.

**What it allows.**
- **Appending** at the end: a new turn, and request-level fields that are identical on every turn (`model`,
  `stream_options`, `return_progress`, an account's fixed `effort:<level>`).
- **A transformation that is a pure function of each item**, applied identically every turn **within a policy
  epoch**: `developer`→`system` for a template that needs it, namespace flattening (each historical call resolves from
  its own recorded namespace, independently of the current tool list), envelope restoration.
- **The policy epoch.** Whether an item is foreign depends on the target, the scope configuration and the ledger, all
  of which can change; a translator upgrade can change how history serialises. So the conversation's encoding and replay
  policy (translator version, replay setting, the strip decisions taken) is recorded with its ledger entries and
  **frozen for the epoch**. A change to any of them is a declared reset that increments the epoch. Within an epoch,
  `strip-foreign-state` strips exactly the items it stripped before.
- **Declared cache resets**, one-off and journaled as `cache.reset:<reason>`: a move to another account or server (its
  cache is cold anyway; the warm-spill logic already minimises these), the first `strip-foreign-state` after a move,
  the deliberate `cache-key-salt` after overloads, an envelope key retirement. Each happens at an event, not on every
  turn.
- **The client's own compaction.** The client may rewrite its history (Codex's compaction); that is the client's choice
  and the one sanctioned way to bound context.

**Audit of today's code and of the earlier designs against it.**

| Mutation | Where | Verdict | Fate |
|---|---|---|---|
| `replayReasoning: {lastTurns: N}` with numeric N (#136) | `compat`, `protocol` | **violates**: a sliding window removes the reasoning the previous request carried | numeric N refused by the validator (R0b); keep `true`/`"all"` and off |
| native-first's `reasoning-window` mutation ("the reasoning items of older turns are left out of `input`", its §8.2) and D6's "starting at N=1" | design | **violates** | withdrawn; client compaction instead (native-first's context gate, applied to both paths) |
| `replayReasoning` unset (all reasoning dropped) | `protocol` | **satisfies** contracts 1-2 (the same rule every turn); breaks contract 3 at each latest reply, whose cost on recurrent models is not bounded in general (tonight it stayed near 85k) | allowed; append-only replay is the default to aim for |
| `systemMessages: merge` | `compat:191`, `:234` | **violates**: a new mid-conversation system message changes the head | retired (D5) |
| codex translated `deriveInstructions` | `codex.ts:252` | **violates**: same, and it defeats OpenAI's prefix cache | retired (R4) |
| anthropic `toAnthropicInput` system hoist | `common.ts:269` | **violates**: also breaks Claude 5.1+'s prefix binding | retired (§6 R10, R5) |
| `adaptCodexResponses` moving tool-result images after the run, with an invented sentence | `protocol:194-210` | **violates** contract 1 when a previously submitted run is extended (the images move past the new results); a fixed reordering inside an already complete run is a semantic loss that need not change later prefixes | retired with the Codex adapter's drops (native-first Step 9) |
| `adaptCodexResponses` lifting `additional_tools` items into the leading `tools` | `protocol:193` | **violates**: a later `additional_tools` item changes the head | retired with the adapter |
| reasoning attachment and orphan dropping; assistant/tool-call grouping (#130) | `protocol:101` | **at risk**: attachment decided by what follows (a reasoning item is dropped as an orphan until output follows it) | the IR's boundary rule (§4.2 rule 1); tested at request boundaries |
| response-side reasoning suppression, `output-limit-cut`, `drain-cut` | `api-openai:490-492`, native-first §8.2 | break contract 3 (what the client can replay) | forward reasoning (R8); cuts are declared and rare |
| `cache-key-salt` kept only in memory | `inference:180`, `:228` | a router restart, expiry or eviction drops the salt and changes `prompt_cache_key` back: an undeclared reset | persist the salt with the conversation, or journal its loss as a reset |
| neutral-value drops, `developer→system`, `content: null → ''` | `compat`, `common` | satisfy (per item, every turn) | as native-first §8.3 decides |
| `cache-key-salt` after overloads | `inference:110-116` | a declared reset, by design; provider-specific (OpenAI's `prompt_cache_key` role is model-dependent and guarantees no residency) | kept, journaled, codex only |
| `thinking-off-in-tool-loop` (this design, §5.2) | design | would violate if switched mid-conversation | fixed on the conversation's **first request**, from whether that request carries `tools`, and never changed (chat clients send `tools` on every request) |
| Anthropic `cache_control` | encoder | the encoder places breakpoints by a fixed rule (end of `system`, end of the latest message), within Anthropic's limits (at most 4 breakpoints; a hit is searched about 20 blocks back from a breakpoint), as its automatic caching does; it never moves content, and markers are excluded from the prefix comparison | R5 |
| the native hold rule (native-first §7.3) | transport | compatible: it changes timing only, never content or order | kept; the failover boundary is the first flush |

**Stable content is necessary, not a cache-hit guarantee.** Prefix stability is what the router controls. Whether an
upstream actually hits its cache also depends on residency, slot assignment and the provider's own rules; the journal's
`cachedInputTokens` is the measurement (§9.3).

**Context is bounded by the client, not the router.** The router cannot shorten a history without changing its prefix.
The bound belongs where the history lives: Codex's `model_auto_compact_token_limit` (and the advertised context
window), set against the server's *concurrent* capacity, which is native-first's context gate (two slots share cloud3's
262,144-token pool). That gate now applies to translated paths as well as native ones. A server that still overflows
answers with a context error, which the client's compaction handles; the router never trims to avoid it.

**Templates can break it too.** A chat template that renders earlier reasoning only after the last user message
(Qwen3.6's default, §2.3) changes the rendered prefix once per user turn. That is server policy, fixed where D5 fixes
templates (`preserve_thinking: true` or a template that renders history the same way every turn). The render check
(§9.1 item 6) reports it, and the rendered-token check (item 5) fails on it.

---

## 5. Provider-by-provider matrix

**Legend.** **N** native (bytes forwarded, `model` patched, declared mutations only). **T** translated through the IR
(§4.2) under a fidelity contract. **—** not offered: refused with the reason named. "Today" says what the code does now
where it differs. Every N and T cell is *eligible only after qualification* (native-first §7.1; D4 on agent routes).

### 5.1 Client protocol: OpenAI Responses (`/v1/responses`; the Codex CLI)

| Upstream | Path | Preserved | Lossy (declared, journaled) | Refused (named) |
|---|---|---|---|---|
| llama.cpp b12020 | **N** `/v1/responses` | messages, leading `developer`, function tools/calls, parallel calls, tool-output images, reasoning items with text (→ `reasoning_content`), `reasoning.effort` (→ template), streaming events | `namespace`/`custom`/`web_search` tools (upstream drops them: skip unless `allowLoss`), `text.format` (ignored upstream), truncation reported `completed`. **No router-side reasoning bound on this native path (D6):** the context gate (advertised window and compaction against concurrent capacity) applies, and if it does not qualify the model stays translated with append-only replay (`lastTurns: "all"`), bounded by the same client compaction (§4.7) | encrypted or content-less reasoning (foreign domain), `additional_tools`, `previous_response_id`, `store:true` |
| vLLM 0.28 | **N** `/v1/responses` | as llama.cpp, plus `namespace` tools (flattened and restored upstream), `text.format` | `custom` tools (dropped in 0.28), stream ids differ from the final response | non-empty `encrypted_content`, `previous_response_id` |
| codex (ChatGPT) | **N** (today) | everything; `encrypted_content` round-trips within the account | `store-false` (validated, so a no-op), `cache-key-salt`, `overload-code`, `output-limit-cut` (no output limit upstream) | `store:true`, `previous_response_id`; opaque state from outside the account's `stateScope` (§4.3, §6 R4); on agent routes, state from any seat but this one (pinned, §6 R5) |
| OpenAI API | **N** (add a `responses` wire; today T via chat, losing all reasoning state) | everything incl. `encrypted_content` with `include` | none known | another org's encrypted state |
| Anthropic API | **T** (Responses → IR → Messages) | messages, images, function tools ↔ `tool_use`, parallel calls, tool-result images, `is_error`; Anthropic thinking **round-trips through the envelope** (§4.5) | `reasoning.summary` (Anthropic has none), effort level mapping (§6 R9), `developer` mid-conversation (§6 R10), `namespace` flattened with a reverse map | `custom` grammar tools, hosted tools (`web_search` is Anthropic's own server tool, a different contract), foreign opaque state |
| `claude` seat (Claude Code CLI) | **T**, narrow | text turns, function tools (MCP bridge) | everything about reasoning: no thinking is returned or replayable | images, schemas, `temperature`, forced `tool_choice` (the pinned adapter's restrictions, §1.2), `custom`/hosted tools; **agent routes: not eligible** (it does not carry the session's reasoning state) |
| DeepSeek | **N** `/v1/responses` (stateless, §2.3), after qualification, **with `developer` → `user` as a known loss** ("`developer` is treated as `user`", [DeepSeek](https://api-docs.deepseek.com/guides/responses_api/)): material for Codex's instructions, so an agent route needs it explicitly allowed or refuses; **T** (Responses → IR → chat) only for what the native wire does not qualify | messages, function tools, `custom` limited to `apply_patch`, reasoning items with text (merged into the assistant message upstream; DeepSeek needs **all** of them whenever tools are present, which append-only replay gives, §4.7), effort | parameters DeepSeek silently ignores (`include`, `truncation`, `service_tier`, …) are listed as qualification losses; no summaries | `encrypted_content`, `previous_response_id`, `store:true`, hosted tools other than `web_search` on `deepseek-v4-pro` |
| image | — | | | `image_generation` tool: refused (images are their own endpoint) |

### 5.2 Client protocol: Chat Completions (`/v1/chat/completions`; Open WebUI, dsh, eval)

| Upstream | Path | Preserved | Lossy (declared) | Refused |
|---|---|---|---|---|
| llama.cpp / vLLM | **N** | everything the server takes, `reasoning_content` both ways, `chat_template_kwargs`, sampling controls (D2) | `stream-usage` (router needs usage) | operator-reserved fields (native-first §7.2) |
| OpenAI API | **N** | everything | `stream-usage` | — (GPT reasoning has no chat slot: a property of the protocol). **Tools on GPT-6 Astra / GPT-6.1 Sol are not available on Chat Completions** (§2.3): such a request goes to the account's `responses` wire, translated (R7 of §10), or is refused |
| DeepSeek | **N** | everything incl. `reasoning_content` | `stream-usage` | — . With tools, DeepSeek 400s unless every earlier `reasoning_content` comes back (§2.3), so a client that drops it (Open WebUI on *Default*) fails there whatever the router does: qualify per client |
| codex (ChatGPT) | **T** (chat → IR → Responses) | messages; mid-conversation system/developer **in place** as `developer` input items (today: folded into `instructions`); tool-output images as `input_image` parts (today: JSON-stringified); `reasoning_effort` | GPT reasoning: returned as `reasoning_details` (opt-in) for clients that replay the message object; otherwise lost between turns, as today, but declared | `n>1`, `logprobs`, `audio` |
| Anthropic API | **T** (chat → IR → Messages) | messages, images, tools, tool-result images; thinking text as `reasoning_content` | signed thinking: carried only via `reasoning_details` (opt-in). **Thinking with tool use needs the signed blocks back** (§2.3), and current models think by default. For a client that cannot carry them, the route either turns thinking off from the conversation's **first request** (decided by whether it carries `tools`), declared as `thinking-off-in-tool-loop`, **only on models that accept `disabled` at that effort** (Opus 5, Haiku 5.5 at `high` or below), or refuses. Opus 5.5, Fable 5.1 and Sonnet 5.5 cannot turn thinking off (`between_tools` still thinks), so there it is a refusal. Never a silent switch | `n>1`, `logprobs`, `seed` (no such parameter), `response_format` unless the target model supports structured output |
| `claude` seat | **T**, narrow | text, function tools | reasoning | images, schemas, `temperature`, forced `tool_choice` |
| image | — | | | chat on an image model (`not_image_model`, today) |

### 5.3 Client protocol: Anthropic Messages (`/v1/messages`; Claude Code) — new

| Upstream | Path | Preserved | Lossy (declared) | Refused |
|---|---|---|---|---|
| llama.cpp b12020 | **N** `/v1/messages` | text, images, `tool_use`/`tool_result` (incl. images), plaintext thinking both ways (signature `""`), `count_tokens` | upstream gaps (§4.6): adaptive thinking, effort and `thinking: disabled` ignored, tool-result/text order, named `tool_choice` → `required`, `is_error` ignored, system-block separator. **Request-sensitive:** `tool_choice: none`, `disable_parallel_tool_use` and `strict` are not read, so a request that uses one is not eligible | `mcp_servers`, `container`, server tools (`web_search_*`, `code_execution_*`): they would become plain function tools upstream; foreign opaque state |
| vLLM 0.28 | **N** `/v1/messages` | as above; effort → `reasoning_effort`; `count_tokens` | inline system merged upstream when the template needs it (D5 says fix the template instead), tool-result images moved to a separate user message, `is_error` ignored, thinking signatures are random hex | as above |
| DeepSeek | **—** (owner, §13: Messages is for local routes only; the facts are kept for reference) `/anthropic` | thinking blocks (must be returned inside a tool loop), `tool_use`, images (model-dependent) | `any` → `auto`, `is_error` and `cache_control` ignored (DeepSeek's table) | `redacted_thinking`, `document`/`mcp_tool_*` blocks |
| Anthropic API | **—** (no API account is planned, §13) | everything, including betas and `cache_control` (Claude Code's gateway contract, §2.3) | — | Sonnet/Haiku 5.5 thinking from an unlinked account (state scope) |
| codex / OpenAI | **T** (Messages → IR → Responses) | text, images, tools, tool-result images | `cache_control` (OpenAI caches automatically: a dropped hint), Anthropic beta features | thinking blocks with real signatures (foreign), server tools, `mcp_servers`; not recommended: Claude Code is tuned for Claude |
| `claude` seat | — | | | estate policy (§4.6): Claude Code uses the seat directly |

### 5.4 Canonical (the Assistant, admin bridge, `test_route`)

The canonical caller builds an `IRRequest` directly. Every upstream is **T** by definition, and every encoder above
serves it. Two changes matter for the Assistant: plaintext reasoning replay becomes the IR's normal case rather than an
account flag (the stopgap's mapping), and an Anthropic API target gets thinking with signatures kept inside the agent loop
(today it gets no thinking at all).

### 5.5 Why the `claude` seat is a narrow translated target, by design

The Claude Pro/Max seat is used through the Claude Code CLI (`print` or `sdk-resume`). That is the sanctioned client for a
seat, and it is a translation by construction: the router writes history as a synthetic transcript, exposes tools through
a schema-only MCP bridge, and reads `stream-json` back (§1.2). As pinned, it carries no thinking blocks (a
resumed transcript would need the real signed blocks, which the router never receives from this transport), and the
adapter refuses images and schemas by choice (the CLI itself has `--json-schema`). The right answer is not to widen it but to qualify it for what it does: text and
function-tool turns for the reviewer bots and the Assistant, never an agent route that needs the session's reasoning,
never a failover target for a conversation holding another domain's state.

### 5.6 Image endpoint

`/v1/images/generations` and `/v1/images/edits` stay as they are: OpenAI's Images shape to the local image server,
already native in all but name. Responses' `image_generation` tool and chat image outputs are not routed there (refused
by name).

---

## 6. Rules: reasoning and thinking state, across turns and across failover

**R1. Native paths: verbatim, both ways.** The client holds the state and the router never edits it: not Anthropic
signatures, not OpenAI `encrypted_content`, not plaintext `reasoning_content`. Two declared exceptions, each an
explicit exception to native-first's ban on structural rewrites (its §8.1) and each journaled: `restore-envelope`, which
undoes the router's own extension (§4.5), and `strip-foreign-state`, on routes that opt in (R5). The router reads it for facts only
(presence, shape, domain). This is D1-D2 applied to state.

**R2. Translated paths: every reasoning item, in order, with its opaque payload.** Decoders keep every block, including
several per turn (interleaved thinking) and their order relative to text and tool calls. Encoders place them where the
target's grammar puts them:
- chat targets (local, DeepSeek): plaintext → `reasoning_content` on the assistant message it belongs to; several blocks
  in one message are joined in order (`reasoning-joined`, declared, since chat has one slot per message);
- Responses targets: reasoning items in place;
- Anthropic: `thinking` / `redacted_thinking` blocks **in their original positions** within the assistant message
  (interleaved and progress-update blocks sit before the `tool_use` they introduce), rebuilt from the IR's boundaries
  (§4.2 rule 1), unmodified. The test is upstream prefix equality across turns (§9.1), because Claude 5.1+ checks the
  values of every earlier block.

**R3. How much is replayed is the target's rule, not the router's.** The router sends everything the client sent, and
the target (its API, or its template) decides what the model sees. The vendors' rules are in §2.3: OpenAI, everything
since the last user message at least; Anthropic, the thinking blocks of the current tool loop must come back unmodified;
DeepSeek, every `reasoning_content` in every later request whenever tools are present (a 400 otherwise); local, the
template's `preserve_thinking`. **No exception.** The router does not bound replay on any path: a sliding window
violates the cache-stability invariant (§4.7), as measured. Replay on a translated local account is append-only
(`lastTurns: "all"`) or off, fixed for the conversation, and the context is bounded by the client's compaction against
the server's concurrent capacity (native-first's context gate, now for both paths).

**R4. A conversation that holds reasoning state is pinned to its domain.** When a request carries reasoning state
whose provenance the ledger or an envelope establishes (§4.3), the eligible targets depend on the route kind (R5):
- **agent routes:** only the seat that produced the state, whatever the scope, for every attempt, including failover;
- **chat routes:** targets with a matching compatibility key take the state as is, for spills, moves and failover;
  any other target only with a declared `strip-foreign-state`.

The scope is the accounts' declared `stateScope` (§4.3): for the ChatGPT seats, the Pro seats together
(measured portable on 2026-10-12; the `dw-gpt-*` routes still run `maxAttempts: 1`, from before that measurement); for
the OpenAI API, the org; for Anthropic, the platform, except Sonnet/Haiku 5.5, which bind to the producing (or a
linked) account. Plaintext reasoning is scoped by the declared reasoning family (§4.3); the two local accounts share a
scope only if the owner declares one.

**R5. When the pinned target cannot serve** (owner decision, 2026-11-04, §13):
- **Agent routes pin a conversation that holds provider state to its seat** (the account that produced it, not merely
  its scope): it **waits** in the admission queue (#133), then **fails loudly** (`reasoning_foreign:<domain>`,
  `reasoning_unknown_origin`, or the seat's own busy or unavailable reason). It is never moved and never stripped. This
  extends D4 from translators to state, deliberately: an agent that silently loses its reasoning mid-task is the failure
  this week was about (native-first §1, bug #2).
- **Chat routes may move it** to another seat. Within the same `stateScope` the state travels as is; outside it, the
  router applies **`strip-foreign-state`**: it removes the state the target cannot take, journals it and discloses it in
  `x-router-mutations`. That is a degraded continuation, as LiteLLM's fallback does, but declared, and it is a declared
  reset under the cache-stability invariant (§4.7).

Never: converting another provider's reasoning into visible assistant text, presenting it as the new model's own
reasoning, inventing a signature, or sending an envelope or ciphertext to a target that cannot read it. (Gemini's
documented dummy thought signature, §2.3, is Google's own escape hatch for histories that came from another model; a
Gemini encoder may use it because Google specifies it; no other encoder has an equivalent.)

**R6. Failover never happens after the first flush** (the hold rule's commitment boundary, native-first §7.3 and
§9.1; OpenRouter and Portkey stop at first output likewise). Before first
output it happens only within the pinned domain (R4).

**R7. Router-translated opaque state reaches the client only through the protocol's own opaque slot** (§4.5): the
envelope in Responses `encrypted_content` or Messages `signature`; `reasoning_details` on chat, opt-in. A client that
drops it loses the state; that loss is declared per route, not hidden.

**R8. Display is not state.** OpenAI reasoning summaries, Anthropic `display: "omitted"`, Codex's raw-reasoning display:
these decide what a user sees. A summary is never replayed as plaintext reasoning to another model.

**R9. Effort and thinking controls map by a table and are never dropped silently.**

| Client says | Responses target (`reasoning.effort`) | chat target (`reasoning_effort` → template) | Anthropic (`thinking` + `output_config.effort`) | Claude seat (`CLAUDE_CODE_EFFORT_LEVEL`) |
|---|---|---|---|---|
| a level the target lists | sent | sent (the template decides; llama.cpp and vLLM map it to `chat_template_kwargs.reasoning_effort`) | sent | sent if in `low…max` |
| a level it does not list | **refused** by name, or the account's declared `effort:<level>` | upstream 400 is the honest answer; or declared `effort:<level>` | refused / declared | refused (today) |
| `none` / thinking off | sent | `enable_thinking=false` (llama.cpp maps `none`) | `thinking: disabled` **only where the model accepts it at that effort** (Opus 5, Haiku 5.5 at `high` or below); Sonnet 5.5's lowest is `between_tools`; Opus 5.5 / Fable 5.1 / Mythos 5.1: refused (§2.3). The mode is fixed on the conversation's **first request**, from whether that request carries `tools` (chat clients send `tools` on every request), and never changed: switching it re-caches the prompt and breaks the invariant (§4.7) | refused unless Claude Code takes it |

No rounding to the nearest level and no silent removal: LiteLLM drops undeclared levels (native-first §4.1), and the
router's own `local: { reasoningEffort: false }` did the same (bug #3).

**R10. Mid-conversation system and developer messages** (D5 and its extension). Where the router owns the template
(cloud2, cloud3), D5 holds: the template renders them in place, and the router does not merge. Where the target's
"template" is a third-party API, D5 cannot be applied, so the encoder decides, as a contract line:
- **Responses targets** (codex, OpenAI): `developer`/`system` input items **in place**. Lossless. Today's
  `deriveInstructions` folds them into `instructions`, which moves them; that is retired (R4 of §10).
- **Anthropic:** the leading run goes to the top-level `system`; later ones stay **in place** as `role: "system"`
  messages, which Claude Code sends (#128's capture) and Anthropic's gateway guidance says to "Leave … where the caller put
  it" (§2.3). Lossless. Hoisting them, as `toAnthropicInput` does today (`common.ts:269`), would also break the prefix
  binding of every later thinking block on Claude 5.1+. Should a model refuse an inline system message, the fallback is a
  declared `system-as-user-reminder`, never a silent hoist.
- **Chat targets we do not own** (OpenAI, DeepSeek): in place; their APIs accept system messages anywhere.

**R11. Reasoning is content.** Captures follow native-first §9.5 (bounded, redacted, labelled; exact bodies only in the
separate diagnostic mode). Envelopes and opaque payloads are never journaled, only their presence and domain.

---

## 7. Rules: tools

| Tool feature | Responses | Chat | Messages | Translation |
|---|---|---|---|---|
| Function tools (name, description, JSON Schema) | `function` | `function` | custom tool `input_schema` | **lossless** among all three **for the schema subset both ends document** (OpenAI `strict` requires its documented subset; Anthropic's `input_schema` and the local servers' grammar conversion support their own). A keyword outside the target's subset is a qualification finding, and a request using it is refused, never silently relaxed |
| `strict` schemas | `strict` | `strict` | `strict: true` on the tool (§2.3) | mapped where the target supports it, **refused** where not (never dropped). Today's `anthropic` adapter refuses `strict` (`common.ts:321`); R5 of §10 maps it |
| Parallel calls | items | `tool_calls[]` | several `tool_use` blocks | **lossless**, grouped by call id (Anthropic: every `tool_result` in the next user message, before any text; chat: tool messages right after the assistant message) |
| `tool_choice` auto/none/required/named | yes | yes | `auto`/`none`/`any`/`tool` | **lossless**; `disable_parallel_tool_use` ↔ `parallel_tool_calls: false`. Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1 reject a forced `tool_choice` with a 400 (§2.3): refused before the network for those models |
| Tool results with images | `input_image` parts in `function_call_output` | text only in OpenAI's chat spec; llama.cpp's chat path takes image parts in tool messages | image blocks in `tool_result` | **lossless** Responses ↔ Messages ↔ llama.cpp chat; **lossy** to a chat target that takes text only: `tool-images-to-user` (declared) puts the images of a run of tool results into **one user message placed immediately after the complete run** (the tool messages answering one assistant message), never between tool messages (OpenAI-style chat requires the tool messages to follow their assistant message directly), never replaced by invented text. It is prefix-stable because a run is placed only when it is complete, and a complete run never changes on a later turn; a request that submits a **partial** run (some calls of that assistant message unanswered) with images is refused for such a target. This is the difference from `adaptCodexResponses` today, which places images after whatever part of a run has arrived (§4.7) |
| `is_error` | — | — | `tool_result.is_error` | **lossy** outside Messages: declared `tool-error-as-text` |
| Call ids | `call_id` + item `id` | `id` | `id` | **lossless**: call ids are never regenerated. The IR keeps client ids and provider-issued ids apart: an item id the router derives (from the call id, when the client protocol has none) is a router id, never sent upstream as if the provider had minted it (the Responses API 404s on ids it did not mint, §2.5) |
| Namespaces (Codex MCP and multi-agent groups) | `namespace` | — | — | **lossless for the client, visible to the model, under conditions:** flatten to `<namespace>__<name>`, keep the reverse map, re-wrap calls on output (`namespace-flatten`, declared). The mapping is not injective by itself, so the encoder **refuses** a request where two flattened names collide, or collide with a plain function name, or exceed the target's length limit; it never renames to resolve a collision. vLLM flattens too; llama.cpp b12020 drops namespaces (#31568 open) |
| Custom/freeform tools with a grammar (Codex `apply_patch`) | `custom` | — | — | **lossy**: as a function tool with one string argument the model loses the grammar. **Refused** unless a route declares `custom-as-function`. The cleaner fix is client-side: Codex already sends function-shaped tools for models outside its catalog (native-first §5.3) |
| Hosted tools: OpenAI `web_search`, `file_search`, `code_interpreter`, `image_generation`, hosted `mcp`, `tool_search`; Anthropic server tools (`web_search`, `web_fetch`, `code_execution`), `mcp_servers` | yes | — | yes | **impossible**: the provider runs them, and the other provider's results and billing differ. Native only; refused by name on translation, including their history items (`web_search_call`, `server_tool_use`, `*_tool_result`) |
| Anthropic-defined client tools (`bash_*`, `text_editor_*`, `computer_*`) | — | — | schema-less, model-trained | **impossible** to translate (no schema to send); refused |
| Deferred tools / tool search (`defer_loading`, `tool_search`) | `tool_search` | — | `defer_loading` | **impossible** unless the router executes the search; refused on translation (#128 finding 5) |
| MCP tool shapes (`inputSchema`, `structuredContent`) | via hosted `mcp` or client functions | function | function | client-executed MCP tools are function tools: lossless for the call; `structuredContent` results become JSON text (`mcp-structured-as-text`, declared) |

**T1.** A tool kind the target cannot run is a refusal, by name, before the network (D2 covers unknown *fields*, not
known *losses*; native-first §9.6).
**T2.** Tool definitions are never renamed, merged, deduplicated with a changed definition, reordered, or added and
removed between turns by the router, except the declared `namespace-flatten`, which is a pure per-tool mapping. The
`tools` list is part of every upstream's cached prefix (and of Claude 5.1+'s prefix binding), so it changes only when
the client changes it (§4.7).
**T3.** Tool names are checked against the target's constraints (`^[a-zA-Z0-9_-]{1,64}$`, as the router's own Claude
validator encodes it, `claude-sdk/index.ts:67`; OpenAI documents the same character set and length for function names);
a violation is refused, not rewritten.

---

## 8. Rules: streaming

**S1. Native: verbatim with a tap,** for all three protocols (native-first §7.3), with one tap per protocol:
- Responses: the terminal event **and its payload's** `status` / `incomplete_details` (vLLM sends `completed` with status
  `incomplete`), `usage`, the first output item;
- chat: `finish_reason`, the usage chunk (`stream-usage`), the first content or tool delta;
- Messages: `message_start.usage`, `message_delta.stop_reason` and usage, `content_block_start`, the `error` event.

**S2. Translated: the router owns the envelope.** One state machine per client protocol turns IR events into that
protocol's stream. Ids are deterministic per attempt; usage arrives where the protocol puts it (Anthropic needs input
usage in `message_start`, before the router knows it: the encoder sends the upstream's input count when the upstream
reports it first, otherwise `0` with the final count in `message_delta`, which is the field Anthropic's own streams update).

**S3. Terminal states: one table, raw value always kept** (`upstream_finish_reason` in the journal; OpenRouter keeps
`native_finish_reason` for the same reason).

| IR terminal | Responses | Chat `finish_reason` | Messages `stop_reason` |
|---|---|---|---|
| completed, natural end | `response.completed`, `completed` | `stop` | `end_turn` |
| completed, stop sequence | `completed` | `stop` | `stop_sequence` (+ `stop_sequence`) |
| completed, tool calls | `completed` | `tool_calls` | `tool_use` |
| incomplete, output limit | `response.incomplete`, `max_output_tokens` | `length` | `max_tokens` |
| incomplete, context window | `incomplete`, `max_output_tokens` (no closer reason exists) | `length` | `model_context_window_exceeded` (Claude 4.5+) |
| incomplete, content filter | `incomplete`, `content_filter` | `content_filter` | `refusal` |
| completed with a refusal (the model declined: content, not truncation) | `completed`, a `refusal` content part | `stop`, the message's `refusal` field | `refusal`, with its text |
| paused (a server-tool loop to resume) | — | — | `pause_turn`: native only, since hosted tools are refused on translation |
| failed | `response.failed` | `data: {"error": …}`, then `[DONE]` (today, `api-openai:546`; chat has no standard error frame) | `event: error` |
| uncertain (llama.cpp's hard-coded `completed`) | as received | as received | as received; journal `finish: uncertain` |

The IR keeps three things apart: the terminal **status** (completed / incomplete / failed), the normalised **finish
reason**, and the upstream's **raw** value. On a native path the client gets the raw value verbatim. On a translated path
the client gets only values its own protocol defines: an upstream value with no row above is journaled raw and sent as
the protocol's documented fallback (`stop` / `completed` / `end_turn`) unless it signals failure or truncation
(native-first §8.1 rule 4), never as an invented enum value.

**S4. Usage: one normalised record, the upstream's numbers to the client.** The journal and budgets use
`{input_total, cached_read, cache_write, output, reasoning}`. OpenAI's `input_tokens` includes cached tokens; Anthropic's
`input_tokens` excludes `cache_read_input_tokens` and `cache_creation_input_tokens`, so the total is their sum (today's
adapter already adds them, `anthropic.ts:219`, but loses the breakdown). Native clients get the upstream's usage
verbatim; translated clients get it in their protocol's shape.

**S5. Keepalives are protocol events where the protocol defines one.** Messages has `event: ping` (the translated
encoder emits it; native forwards the upstream's); Responses has none, and Codex's idle timer counts parsed events only,
so the translated path keeps #131's `response.in_progress`, and native gets the upstream's own progress
(native-first §9.4); chat uses SSE comments.

**S6. Errors mid-stream** use the client protocol's own shape (S3's failed row). The router adds a terminal event only
when the upstream sent none (native-first §7.3 rule 3).

**S7. The failover boundary is the first flush** (the hold rule, native-first §7.3). Messages streams hold
`message_start` like Responses holds `response.created`.

**S8. Non-streaming:** native forwards the upstream's JSON (bounded, native-first §7.3); translated builds the object
from the IR.

---

## 9. Test strategy

The owner's standing rules shape it: tests run in docker; **short (~15 min) tests come before long ones**; no request
goes to a cloud model while the probe and the release run (and in CI never: cloud upstreams are recorded fakes).

### 9.1 Tier 0: deterministic, in docker, every PR (minutes)

1. **Golden conformance per protocol pair.** For each (client protocol × target protocol) pair the router offers, a
   corpus of real, redacted requests and recorded upstream streams:
   - Codex 0.166 Responses turns (native-first Step 1's corpus);
   - Claude Code 2.1.x Messages turns (#128's capture, step 1): tool loops, images, thinking blocks with signatures,
     `/compact`, deferred tools, sub-agents, `count_tokens`;
   - Open WebUI and dsh chat turns, with and without `reasoning_content`;
   - Assistant IR requests;
   - recorded upstream streams: llama.cpp b12020 and vLLM 0.28 (`/v1/responses`, `/chat/completions`, `/v1/messages`),
     the codex backend (the existing `codex-responses-stream.sse` fixture, plus new ones with encrypted reasoning),
     Anthropic (from its documented examples; no live calls), DeepSeek (documented examples).

   For each item it asserts:
   - **native:** `upstreamBody == applyDeclared(clientBody, mutations)` and client stream == upstream stream modulo
     comments and declared response mutations (native-first §8.1 rule 6, §7.3);
   - **translated:** a snapshot of the upstream body and the client stream, **plus the round-trip property for every
     lossless contract line**: `decode(encode(ir)) == ir` and, across a two-turn exchange, the client's replayed items
     decode to the same IR the router produced. Opaque payloads are compared byte for byte; envelopes must round-trip
     through a recorded Codex replay;
   - **declared losses** appear by name in `request.wire.mutations` and `x-router-mutations`, and nowhere else;
   - **upstream prefix equality across turns:** for Anthropic targets, the Messages body the router builds for turn N+1
     must contain, value for value, the `system`, `tools` and earlier messages it sent at turn N plus the reply it
     received, so Claude 5.1+'s prefix binding holds; the same check runs across translator versions (a fixture recorded
     with version K replayed through K+1).
2. **State and envelope tests.** A conversation with OpenAI, Anthropic or plaintext state: placement pins it;
   failover skips incompatible targets (`reasoning_foreign:<domain>`); `strip-foreign-state` acts only where declared;
   unknown-origin state is refused on agent routes; vLLM's random-hex signatures are not taken as Anthropic's. Fixtures
   for: a router **restart** mid-conversation (the ledger survives); **key rotation** (old key still verifies within the
   lifetime; a retired key gives `envelope_key_retired`); **tampering** (`envelope_tampered`, never stripped);
   **cross-principal and cross-conversation replay** of a valid envelope (foreign), and a **route change** to a
   compatible target (restored) and to an incompatible one (refused or stripped); **mixed state** (OpenAI and plaintext items
   in one history); **rollback** (envelope emission off, reading on: in-flight sessions continue); a **resumed session** after placement
   expired (re-bound from the ledger to its seat) and after ledger retention (agent route: a refusal naming the
   remedy; chat route: one strip and an epoch increment); an envelope arriving
   on a native path (restored by `restore-envelope`, never forwarded); **interrupted delivery:** cut the stream (a client
   disconnect, and a killed router process) right after each state-completing event (`output_item.done`,
   `signature_delta` / `content_block_stop`, the chat finish chunk), then continue the conversation, and show that the
   ledger already holds the item; force a ledger write failure and show that the completing event is never delivered
   and the stream ends with the protocol's error event; and show that a plaintext fragment kept from a cut stream is
   attributed through its open-item row.
3. **Terminal and usage tables** (§8 S3, S4), each row a test, per protocol, native and translated.
4. **Streaming transport** (native-first §7.3): fragmented UTF-8, multiline frames, slow readers, disconnects, drain,
   plus Anthropic `ping`/`error` events and chat error frames.
5. **Cache-stability checks (§4.7), one per contract.** For every recorded multi-turn conversation and every (client,
   target) pair, native and translated, at complete message boundaries: (1) **content:** the upstream body of turn N+1
   contains turn N's by value (system, tools, items; `cache_control` markers excluded); (2) **rendered tokens:** for
   local targets, the tokenised render of turn N (images included) is a prefix of turn N+1's; (3) **generated reply:**
   turn N+1's prompt equals turn N's prompt plus the generated tokens, except at the boundaries the qualification lists.
   Every declared reset appears in the journal at its turn, with the epoch incremented. A test that the validator
   refuses a numeric `lastTurns`.
6. **Render checks.** Every pair that reaches a local model renders the upstream body through the real template
   (llama.cpp's `/apply-template`, vLLM's renderer, in a docker container with the served template file) and snapshots
   the prompt text. This is what catches empty `<think>` blocks, split assistant turns, moved system text and the
   llama.cpp Messages tool-result order (§4.6).

### 9.2 Tier 1: the Open Responses compliance suite and llm-comply (minutes)

Run the Open Responses suite (§2.1), and llm-comply's Responses, Chat Completions and Anthropic Messages suites (MIT,
`--base-url`, `--format messages`), against:
- **the router's own `/v1/responses`**, once per target kind, with the upstream a recorded fake (native llama.cpp,
  native vLLM, translated Anthropic, translated chat). This tests the router as a Responses server, translated paths
  included, which no other check does;
- **the local upstreams directly** (`/v1/responses` on llama.cpp and vLLM in docker on a small model, and read-only on
  cloud2/cloud3 when the models are free, as native-first Step 5 schedules it).

Each failure is classified before anything changes: a **router defect** (a translated path or the router's own stream
does it wrong: fix the router), an **upstream limitation** (the server does it wrong on a native path: a `losses` entry
in the qualification manifest, native-first §7.1, and an upstream bug report), or an **intentional exclusion** (a feature
the router refuses by design, such as `previous_response_id`). Suite and client versions are pinned in the manifest
entry, so a result is reproducible.
Both suites are thin (schema checks, a few semantic ones, no translation round-trips), so they are a first filter;
the golden corpus (§9.1) is the conformance suite that qualifies a wire.

### 9.3 Tier 2: real clients, short (≤ 15 min each), docker first

Each client runs against an in-process router with fake upstreams that replay recorded streams, then against the real
local upstreams. Each check is a short task with a checkable artifact (a file written, a test passed), not "exit 0":

| Client | Check |
|---|---|
| Codex CLI 0.166 (`codex exec`) | **the prompt-cache check: the 12-file task (~10-15 min) on each local wire**, from the journal's `usage.inputTokens` and `cachedInputTokens`. It passes when (a) **on append-only wires** (append-only replay, and native wires, where the client replays everything), **no request's uncached prompt tokens exceed the growth of its prompt since the previous request plus 3k**, except a request that follows a declared reset, and (b) **on every wire, the task's total uncached prompt tokens stay within 1.5× its append-only baseline** (about 75-85k: `lastTurns: "all"` gave 75k over 51 requests, only 2 over 3k; P1 gave 67k over 36, 7 over 3k because of large tool outputs, which (a) allows). **On replay-off wires to recurrent models** (flash-next, qwen3.8; cloud2 runs this way at rev 2228), criterion (a) does not apply. By design, a reply re-rendered without its reasoning can force a restore from an earlier checkpoint (§4.7, contract 3), so per-request outliers are expected there. They are recorded and reported, and the wire is judged on (b) alone. A replay-off wire that fails (b) fails the check; that is the measurement §9.7 uses to choose qwen3.8-27b's replay setting. The failing sliding-window arm was 777,850 over 43; a text turn; a tool loop; parallel calls; an image tool output; a resumed session across a native ↔ translated flip in both directions (native-first §10.4); **an envelope round-trip** (translated Anthropic fake → Codex replays → the fake receives the exact signature); a deliberately short `stream_idle_timeout_ms` against a long prefill |
| Claude Code 2.1.x (`claude -p`, `ANTHROPIC_BASE_URL` = router) | a `Bash` + `Read` + `Edit` task; an image; a sub-agent; `/compact`; thinking blocks replayed; every model-selection override (#128 finding 20); unknown betas |
| Open WebUI (docker) | a chat with reasoning on each provider setting (*Default* and *llama.cpp*), proving what it replays; a tool call |
| dsh, the Assistant | their existing suites, on IR v2 |

Recorded fakes prove the router's behaviour, not a vendor's acceptance. Each new wire or encoder against a cloud API
therefore also gets a **live acceptance check** once (a handful of requests, scheduled by the owner, never while a probe
or a release runs), before it serves traffic: an Anthropic thinking tool loop across two turns, an OpenAI reasoning
replay, a DeepSeek tool loop.

### 9.4 Tier 3: long runs, only after tiers 0-2 pass

P1-length Codex runs, two concurrent sessions through compaction, Claude Code sessions of an hour, then the 48 h watch,
as native-first §11 Step 6 sets out.

### 9.5 Always: what the model actually saw

For every local target, one test per release renders a captured real request through the served template and diffs it
against the previous release's render. A change in the rendered prompt that no mutation explains is a failure.

### 9.6 Router transparency A/B, for every provider (owner requirement, 2026-11-05)

The owner, verbatim: *"For all providers (e.g. codex, anthropic, qwen) implement and run short tests directly, without
llm-router and with llm-router, then compare the results. We must validate that the router does not negatively affect
the quality of responses."*

**Design.** The same short tasks run **DIRECT** (client to the provider or upstream) and **VIA** the router, with the same
client and version, the same settings and, where possible, the same seat or account. The arms are paired per task and
interleaved in time (A, B, B, A, …), so load and time of day hit both. Local models run one arm at a time, as the
benchmark does, because both arms share the two-slot pool.

**Layer (a): deterministic transparency (cheap, decisive, one run per case).**
- **Record both arms:** what the upstream receives (a recording proxy in front of the upstream, or the router's exact-body
  diagnostic capture, native-first §9.5) and what the client receives (the stream as the client sees it).
- **Normalise only volatile fields** (ids, timestamps, session and `client_metadata` values, a re-keyed
  `prompt_cache_key`), then diff:
  - native paths: only the declared mutations of that wire may differ (`model`, `return_progress`, `stream-usage`, …);
  - translated paths: only the declared, qualified mapping of that pair;
  - the responses: events, fields, terminal status and usage, by the same rule.
- **Handling:** recordings are conversation content. They are encrypted at rest and short-lived, like the diagnostic
  capture. Credentials and `Authorization`/`x-api-key` headers are never recorded.

**Layer (b): quality A/B (short tasks first, about 15 minutes each, the standing rule).**

| Metric | How | Non-inferiority margin (predeclared: the router arm is not worse than direct by more than this) |
|---|---|---|
| Task success | the 12-file non-regression task, short benchmark implementation tasks, a structured-output task; tests pass, artefact delivered | at most 1 paired failure more than direct in 6 pairs |
| Tool-call validity | invalid or rejected tool calls per run | ≤ direct + 2 percentage points |
| Reasoning continuity | the decision-turn probe (native-first §12.1: commit at the decision turn), local models | commit rate within 1 of 6 |
| Tokens and cache | input, output, uncached prompt tokens (journal and upstream usage) | uncached total ≤ 1.2× direct; on append-only wires also §9.3's per-request bound (criterion (a)) |
| Latency | time to first token, time to the artefact, wall time | TTFT median ≤ direct + 2 s; wall time median ≤ 1.25× |
| Errors and retries | router-originated errors, client retries, `stream closed` loops | no router-originated error; retries ≤ direct |

- **Sample size.** Start with 3 pairs per task. A metric outside its margin, or within half a margin of it, takes 3 more
  pairs, and the decision uses all 6. The margins are written into the run's plan file before the first run and not
  changed after it.
- **When the router arm fails:** layer (a) says where. Either the diff shows an undeclared difference (a router defect,
  §9.2), or there is no difference, which points at load, a queue or a cold cache (re-run, then look at the journal).

**Direct-path setup, per provider:**

| Provider | Direct arm | Via-router arm | Comparable? |
|---|---|---|---|
| codex (ChatGPT seat) | Codex CLI logged in with the seat itself | Codex CLI on the seat's `dw-*` / pool route (native) | yes, same seat and model. Each run costs that seat's quota |
| Claude seat (`claude` provider) | Claude Code (`claude -p`) logged in with the seat | the router's `claude` transport, which runs Claude Code itself, reached through a chat or canonical caller | **layer (a) yes**: a recording proxy on `ANTHROPIC_BASE_URL` in both arms shows what Anthropic receives, and the router's synthetic transcript and MCP bridge are its declared translation. **Layer (b) only for text and function-tool tasks.** Agentic coding tasks are not comparable: no client can drive Claude Code's own tool loop through the router, because Messages to the Claude seat is out of scope (owner, §13) |
| qwen3.8-27b (vLLM, cloud2) | Codex → vLLM's own `/v1/responses`; chat clients → its `/v1/chat/completions` | Codex and chat clients via the router (translated today; native after native-first Step 7) | yes |
| flash-next (llama.cpp, cloud3) | Codex → llama.cpp's own `/v1/responses` (through llama-swap); chat clients → `/v1/chat/completions`; Claude Code → `/v1/messages` (for R6c) | the same clients via the router | yes |
| image models | `POST /v1/images/generations` to the local image server | the same via the router | yes. With a fixed seed, the same image bytes are expected: a byte or hash comparison is layer (a) |
| Anthropic API, OpenAI API, DeepSeek | the vendor SDK direct | via the router | **not applicable today:** no such account exists or is planned (§13). Required before R5 or R7 is scheduled |

### 9.7 Planning re-validation on qwen3.8-27b and flash-next (owner requirement, 2026-11-05)

The owner, verbatim: *"Run the planning test on qwen3.8-27 and flash-next again, after you fix the models configuration
issues, directly without llm-router and validate the results."*

**Preconditions: the model configuration fixes.**
- **flash-next:** append-only replay (done, rev 2228), plus a Codex `model_auto_compact_token_limit` (and advertised
  window) sized for the shared 262,144-token pool: at most half of it per session, minus output headroom (native-first's
  context gate).
- **qwen3.8-27b on cloud2:** its replay setting (append-only or off), decided on measurements. Run the decision-turn probe
  and the §9.3 cache check on both settings. Criterion (a) applies to the append-only setting only; the replay-off
  setting is judged on the task total (criterion (b)), with its per-request outliers reported. Choose by quality first,
  then by cache.
- **Templates:** any D5 template fix the render checks require.

**Runs.**
- **Tasks:** the planning tasks P1-P3 of the local-model benchmark (ailab
  `plans/2026-11-03-local-model-agent-benchmark-plan.md`), at their base commits, with their frozen task texts,
  checklists, settings and caps.
- **Arms:** each run both **DIRECT** (Codex 0.166 to the model's own `/v1/responses`) and **VIA** the router (translated
  with append-only replay; native once its canary passes).
- **Order:** the short checks come first, about 15 minutes each: the 12-file task and the decision-turn probe on each
  model.
- **Judge:** the benchmark's blind judge, unchanged: `codex exec --profile review`, gpt-6-astra, xhigh, read-only. It
  scores the rubric 1-5 (correctness, completeness, feasibility, verification, grounding) in two passes with different
  letter orders. Direct and routed plans are blinded together, so the judge does not know the arm.
- **Scoring:** the benchmark's day-2 delivery-aware rule.

**Pass, per model:**
- the routed plan mean is within 2 points (of 25) of direct;
- a plan is delivered within the cap via the router whenever it was delivered direct;
- every run passes §9.3's cache check, as scoped there for the model's replay setting.

**Evidence, corrected 2026-11-05.** The "1.3× gap" (direct 15.4 min against the router's ~20 min) was a measurement
error. 15.4 min was when the direct run ATTEMPTED its first write, an `apply_patch` that failed ("the last line of the
patch must be '*** End Patch'"). The router run made the same failed attempt at 16.6 min. Timed by successful writes
(the tool's exit code or "Success."):
- **first successful write:** direct 19.3 min, router 20.1 min (1.04×);
- **wall time:** direct 23.5 min, router 21.2 min (0.90×).

So there is no router gap. §9.7 times delivery by the first SUCCESSFUL write of the plan file, and records each failed
write separately.

---

## 10. Migration plan

The native-first critical path (native-first §11: Step 1, Step 3 d2, the context gate, Step 4, Step 5, Step 6) stays
first, **gated by the §9.6 A/B on the local models for each canary (native-first Steps 6-8) and by the §9.7 re-run,
and amended by R0b:** wherever native-first falls back or rolls back to a translated path "with `lastTurns`", that
now means append-only replay (`"all"`) or off, with the client's compaction as the bound. The steps below are for the rest of the router. Each is behind configuration, off by default,
reversible by a config write or an image revert, has its tests, and gets a codex review of its plan and implementation.

| Step | What | Behaviour change | Rollback | Gate (§9.6 A/B, §9.7) |
|---|---|---|---|---|
| **R0** | Record this standard as an ADR beside D1-D5 (the IR, provenance and state domains, the three front doors, the cache-stability invariant), with D6 amended (§4.7) | none | — | — |
| **R0b** | The config validator refuses a numeric `replayReasoning.lastTurns` (keeps `true`/`"all"` and off); the `reasoning-window` registry entry is withdrawn from the native-first plan; client compaction thresholds are set against concurrent capacity for translated local routes too | refuses a config that would defeat the cache (none is live: rev 2228) | revert | layer (a) on the local accounts: no difference |
| **R1** | IR v2 types beside v1, **only the decoders active callers need** (Responses and chat first; Messages with R6), and an `IRv2 → v1` shim, so every adapter keeps running unchanged. The golden suite pins that v1 behaviour is identical through the shim | none | revert | layer (a) on every provider: no difference (the shim reproduces v1) |
| **R2** | The provenance ledger (digests only) and state facts in the journal (`request.state {formats, provenance, wouldRefuse}`) | none (journal and store only) | revert | layer (a): no difference |
| **R3a** | Declare `stateScope` and model families from today's evidence (the Pro `codex-*` seats share one scope). Scopes decide validity, and spills on chat routes only; agent routes pin to the seat. R2's `wouldRefuse` shows which spills the pin would stop | none | config | layer (a): no difference |
| **R3b** | **Enforce** the pin in placement and failover (§6 R4-R5), per route: agent routes pin to the seat, chat routes may move and strip (owner, §13). A route is enforced when §4.3's **measured readiness** holds: its replayed opaque items hit the ledger ≥ 99 % over a day of its traffic, or its legacy sessions have been drained or compacted. Until then the route may run in the per-route legacy mode (`legacyState: 'allow'`, journaled as weaker assurance). `strip-foreign-state` is a declared, per-route mutation (chat routes only) | **yes**: agent-route conversations holding reasoning state no longer spill (they queue, then fail); chat-route moves outside the scope strip, declared | per-route flag | A/B on the codex seats (`pool-codex`) and both local accounts |
| **R4** | **codex encoder on IR v2** (translated path): `include: ["reasoning.encrypted_content"]`, reasoning items replayed in place, mid-conversation system/developer as `developer` items in place (not folded into `instructions`; the backend refuses `system` items, §2.3), `phase` kept, tool-output images as `input_image` parts. **No envelope needed:** the state is already in the format of the Responses slot, and the ledger gives its provenance; chat clients get `reasoning_details` if the route opts in. **Depends on R3b being enforced on the same routes** (encrypted state must stay on its seat) | **narrower than it sounds:** Responses clients on a translated route keep their GPT reasoning across turns (they replay `encrypted_content`); chat clients only where the route opts into `reasoning_details` *and* the client replays message objects, which Open WebUI (*Default*) and dsh do not (§5.2, §11); the Assistant once it stores reasoning items in its conversations (IR v2) | per account flag | A/B on the codex seat: Codex CLI direct as the reference; the translated callers (chat clients, the Assistant) via the router |
| **R5** | **anthropic encoder on IR v2**, only once an API account exists. **All envelope machinery is deferred to here** (R4 and R6c need none): R5a the envelope **verifier**; R5b thinking per model (§6 R9), signatures and `redacted_thinking` in their positions, inline `system` in place, the full `stop_reason` table, and the envelope **writer** for Responses clients | an Anthropic API account gets thinking | per account; writer off = rollback, verifier stays | A/B against the Anthropic API direct (needs an account first) |
| **R6a** | `/v1/messages` kernel prerequisites (#128 findings 14-18, 26): the `x-api-key` authenticator, the Messages body cap and admission budget, the Anthropic error serializer, the telemetry protocol union | none until a route opts in | revert | layer (a): no difference on existing endpoints |
| **R6b** | Configuration: `protocols` on RoutePolicy, with migration, pointer and fallback validation, and subscription providers forbidden on Messages routes (#128 findings 19, 22, 23) | none until a route opts in | config | layer (a): no difference on existing routes |
| **R6c** | Native Messages wires to llama.cpp and vLLM only (owner, §13), each after its quirks are qualified (§4.6), route opt-in | new endpoint for the opted-in routes | per route | A/B: Claude Code direct to llama.cpp and vLLM `/v1/messages` vs via the router |
| **R6d** | Translated Messages pairs, only if a qualified need appears | per route | per route | A/B per pair, when scheduled |
| **R7** | `openai` provider: a `responses` wire (native for Responses clients; chat clients translated to Responses, required for tools on GPT-6 Astra / 6.1 Sol) | only if an OpenAI API account is configured | config | A/B against the OpenAI API direct (needs an account first) |
| **R8** | Encoders for chat targets on IR v2 (local, DeepSeek): the stopgap's mapping becomes the IR's normal case, append-only; reasoning after visible output is forwarded on translated streams, so clients can replay it (§4.7, both directions) | **yes**: clients now receive (and will replay) reasoning that was suppressed, which changes their history and context use; golden, render and cache-stability checks gate it | per account; rollback stops forwarding but keeps decoding what clients already hold | A/B on both local accounts with chat clients, plus §9.7 re-run on both models |
| **R9** | Retire | — | — | the A/B of the step that replaced the code |

**Kept, rewritten, retired.**

| Today's code | Fate |
|---|---|
| `runNative` / `parseNativeResponses` (codex native) | **kept**, as the first instance of the generic native transport (native-first §7.2-7.3); its `JSON.stringify` transport is rewritten to byte patching |
| `chatcompletions.ts` | **kept** as the chat encoder + chat tap; `toChatMessages` rewritten on IR v2 (R8) |
| `codex.ts` translated `run()` | **rewritten** (R4): `deriveInstructions` folding and `toResponsesInput`'s `JSON.stringify` of tool parts **retired** |
| `anthropic.ts` | **rewritten** (R5): `toAnthropicInput`'s system folding replaced by §6 R10; thinking added |
| `adaptCodexResponses` | **retired** once native carries Codex traffic (native-first Step 9); until then its drops become refusals (native-first Step 3 f). Its invented tool-image sentence ("The tool returned an image, shown after the tool results.") goes with it |
| `parseRequest` subset | **rewritten** as the Responses and chat decoders (R1); the allowlist stays as the translated path's refusal list |
| `Message.reasoning` + `replayReasoning` (#135/#136) | **kept** until R8, then subsumed: plaintext reasoning items are the IR's normal case, replayed append-only. **The numeric `lastTurns` is retired now** (R0b): it violates the cache-stability invariant |
| `systemMessages: merge`, neutral-value drops, `developer→system` for local | **retired** per D5 and native-first §8.3 |
| `claude` / `claude-seat` transports | **kept**, qualified as narrow translated targets (§5.5) |
| `images.ts` | **kept** |
| #128's `parseMessages` translator plan | **replaced** by R6 (native-first); its transport requirements kept |

**Rollback contracts.** Once R4, R5b or R8 has emitted state-bearing output, an image revert alone is not a rollback:
clients hold items only the new code decodes. Each of those steps therefore rolls back by turning emission off while the
decoder (and the envelope verifier) stays deployed, until the affected conversations have expired or been compacted.

**Order and gates.** R1, R2 and R3a change no behaviour and can start now. R3b is the first behaviour change (a
routing restriction), switched on per route after R2's journal has shown what it would refuse. R4 is the first
remote-side change and the cheapest large win (chat and Assistant callers on GPT seats lose their reasoning every turn
today); it is tested against recorded streams, then given one live acceptance check (§9.3). R6a-R6c wait for the native
Responses canary (native-first Step 6), so that the transport is proven once before a second protocol uses it. **Not
scheduled:** R5 and R7 (no OpenAI or Anthropic API-key account is planned, owner §13), R6d, and anything Gemini. Each envelope writer
ships after its verifier, and its rollback is to stop writing, never to stop reading.

---

## 11. Risks and open questions

- **The IR is new code on the hottest path.** Mitigation: R1 ships it behind a shim that reproduces today's behaviour,
  pinned by the golden suite, before any encoder changes behaviour; the native paths (where most traffic goes) do not
  use it at all.
- **Upstream converters are moving targets.** llama.cpp's and vLLM's Responses and Messages converters (§4.6,
  native-first §5) change every release. Qualification is per backend version and template digest (native-first §7.1);
  an upgrade unqualifies a wire until re-run.
- **Anthropic does not support Claude Code on non-Claude models through a gateway** (§2.3). The local `/v1/messages`
  wires are therefore at the estate's own risk, qualified by the real-CLI matrix (§9.3), and documented as such. Claude
  Code's betas change fast; a native front door forwards them (Anthropic's rule), and a local upstream ignores what it
  does not know, so a beta whose semantics a local server cannot honour is a qualification finding, not a router rule.
- **Envelope keys.** The MAC key is a router secret (OpenBao, like the others). As §4.5 rule 5 says, a key stays
  verifiable until every conversation holding its envelopes has been idle past the replay lifetime, **measured from last
  use**, not from the key's age, so routine rotation strands nothing. Only retiring a compromised key early loses state:
  that is one declared reset per affected conversation (a refusal that says to compact or restart, or one strip on a
  chat route), not a loss on every turn. (Envelopes are deferred to R5, so none exist until then.)
- **Account-bound state and pools.** Sonnet/Haiku 5.5 thinking and OpenAI encrypted reasoning are account- or org-bound
  (§2.3). Smart pools that spread one conversation over several seats lose that state on every move. **Decided (§13):**
  agent routes stay on the seat that produced the state, whatever the scope (queue, then fail); chat routes may spill
  within the scope, or move and strip. The cost is that dev-worker Codex
  conversations on `pool-codex` stop spilling to a warm seat when theirs is busy (spills were 2.5 % of turns after the
  2026-10-12 concurrency change); R3a's `wouldRefuse` journal shows how often they would queue or fail before R3b is
  switched on.
- **Chat clients cannot carry opaque state.** Open WebUI (*Default*), dsh and most chat UIs drop unknown message fields.
  For them, GPT and Claude reasoning is lost between turns whatever the router does; `reasoning_details` helps only
  clients that replay message objects. Stateful storage in the router (keyed by tool-call id) would close the gap but
  makes the router hold conversation content; not proposed.
- **DeepSeek's rule is the strictest** (every `reasoning_content` back whenever tools are present). Append-only replay
  satisfies it; a client that drops reasoning does not, whatever the router does.
- **Client compaction is now load-bearing.** With no router-side bound, a client that does not compact (or whose
  threshold exceeds the server's concurrent capacity) fills the shared KV pool. The context gate must hold on every
  local route, translated or native, and the dev-worker profiles must set the threshold explicitly.
- **Open questions:** none left for the owner; the four were decided on 2026-11-04 (§13).

---

## 12. Codex cross-validation

Run on the workstation, not through the dispatcher:
`codex exec --profile plan-review --skip-git-repo-check -C <this directory> --output-last-message <file> - < <prompt>`.
Codex CLI 0.163.0, provider `llm-router`, `model: gpt-6-astra`, reasoning effort `xhigh`. It read the design, the
llm-router export, the llama.cpp and vLLM sources and #128, and checked vendor documentation on the web. It modified
nothing and called no LLM API.

### 12.1 Round 1 (session `01a1a7db-cbdd-78d3-ae72-cece147019ab`, 181,799 tokens)

Raw output: `codex-r1/review.md`; log: `codex-r1/run.log`; prompt: `codex-r1/prompt.md`; the reviewed text:
`design.r1-reviewed.md`.

**Verdict: adopt with changes.** "Keep three native-first front doors and a Responses-shaped IR for qualified
translations. State-domain pinning is necessary, but its provenance and compatibility rules need tightening. The envelope
is a reasonable router extension; its current specification is insufficient for safe replay. D1–D5 are broadly
respected; correct the D6 contradiction and strengthen qualification and rollback gates."

| # | Severity | Finding (short) | Response |
|---|---|---|---|
| 1 | blocker | Provenance cannot be established by acceptance (upstreams ignore state they cannot use); `stateScope` omits model family; DeepSeek listed as an Anthropic-state producer | **Accepted.** §4.3 rewritten: a digest-only provenance ledger, surviving restarts; a compatibility key (producer, model family, credential scope, format); unknown-origin state refused on agent routes; DeepSeek's thinking blocks reclassified as DeepSeek plaintext state. §6 R4-R5 follow |
| 2 | blocker | Envelope restoration conflicts with native dispatch; the envelope must carry type, format and ids; it is a router extension needing client qualification | **Accepted.** §4.5: envelopes verified before routing on every path; restored by a declared `restore-envelope` mutation on native paths; never reach an upstream; the payload schema carries state type, format, ids and a text digest; qualified per client (rule 7) |
| 3 | blocker | "Thinking blocks first" breaks interleaving; the flat IR loses message/block boundaries; IR round-trip cannot prove Anthropic prefix preservation | **Accepted.** §4.2: items carry `at {msg, block}`; decoders may decompose but never reorder or merge. §6 R2: blocks in their original positions. §9.1: upstream prefix equality across turns and across translator versions |
| 4 | important | Envelope security: invalid vs foreign vs retired; origin is not permission; rotation is not a one-time loss | **Accepted.** §4.5 rules 1-6 (bounded decoding, MAC over version + key id + payload, distinct reasons, authorisation independent, conversation binding, key retention for the envelope lifetime, recovery path); §11 corrected |
| 5 | important | `reasoning-window` on the native llama.cpp row contradicts D6 | **Accepted** (a contradiction, not an argued extension). Then superseded by §12.2: no router-side window on any path |
| 6 | important | Anthropic thinking controls are per model (Opus 5.5 cannot disable; Sonnet 5.5's lowest is `between_tools`); omitting `thinking` does not mean none | **Accepted, verified** in the thinking docs. §1.2, §2.3, §5.2 and §6 R9 now say so; `thinking-off-in-tool-loop` only where the model accepts it, otherwise refusal |
| 7 | important | The local Messages loss inventory is incomplete | **Accepted, verified in source:** llama.cpp ignores `tool_choice: none`, `disable_parallel_tool_use`, `strict`, `is_error`, `thinking: disabled`; vLLM moves tool-result images into a user message and ignores `is_error`. §2.8, §4.6 and §5.3; the request-sensitive ones make a request ineligible |
| 8 | important | DeepSeek documents a native Responses API | **Accepted, verified** ([docs](https://api-docs.deepseek.com/guides/responses_api/)). §2.3, §2.8, §4.4, §5.1: a native `responses` wire, with its silently ignored parameters as qualification losses |
| 9 | important | `<namespace>__<name>` is not injective; derived ids must not pass as provider ids | **Accepted.** §7: collisions and length violations refused, never renamed; client and provider ids kept apart |
| 10 | important | Refusal and truncation conflated | **Accepted.** §4.2: typed `refusal` parts, status separate from finish reason; §8 S3: a refusal row, and raw values never emitted into another protocol's enum |
| 11 | important | Test failures need classifying; recorded fakes are not vendor acceptance; more fixtures; pinned versions | **Accepted.** §9.1 fixtures (restart, rotation, tampering, cross-route replay, mixed state, prefix stability, rollback); §9.2 classification and pinned versions; §9.3 a one-off live acceptance check per cloud wire, scheduled by the owner |
| 12 | important | The migration is not uniformly small or reversible: R3 changes behaviour; R6 bundles too much; envelope writers before readers strand sessions | **Accepted.** §10: R3a (declare, observe) vs R3b (enforce, per route); R6a-R6d along #128's prerequisite boundaries; verifiers before writers, rollback = stop writing; R4 needs no envelope at all; R5, R7, R6d and Gemini unscheduled until needed |
| 13 | nit | The Claude seat's limits are adapter and policy choices (the CLI has `--json-schema`); nesting is not a routing cycle | **Accepted.** §1.2, §4.6, §5.3, §5.5 reworded |

Codex's "verified in source" list included the IR limits (`contracts/index.ts:245`), the codex adapter's missing
`include` and discarded reasoning (`codex.ts:351`, `:575`), the system hoist and tool-output stringify (`common.ts:267`,
`:354`), the discarded thinking (`anthropic.ts:254`), the llama.cpp ordering and control losses (`server-chat-b12020.cpp`
L421, L536, L560, L582, L614) and empty signatures, vLLM's template detection, image relocation and random signatures
(`anth-serving.py` L144, L407, L532, L646), D6's native/translated distinction (native-first, PR copy line 1633), and the
vendor rules. It did not verify live backend acceptance, envelope replay through the deployed clients, or whether the
Codex backend returns ciphertext without `include` (the adapter's failure to keep or replay it is verified).

### 12.2 Between the rounds: the cache-stability requirement

After round 1 the coordinator relayed a production measurement as a hard requirement (§4.7): a sliding reasoning window
(`lastTurns: 1`) made the hybrid local models re-prefill 50-80k tokens per turn (777,850 uncached prompt tokens over 43
requests, against about 85k append-only) and a 15-minute task fail its cap. The design now states the cache-stability
invariant (§4.7), audits every mutation against it, amends D6 (append-only or off, client compaction, no router-side
window on any path), withdraws native-first's `reasoning-window`, adds R0b (the validator refuses a numeric
`lastTurns`), and adds cache-stability checks to tiers 0 and 2 (§9). Round 2 reviewed this text.

### 12.3 Round 2 (session `01a1a7ef-36ac-7813-8112-b128b202658a`, 158,208 tokens)

Raw output: `codex-r2/review.md`; log: `codex-r2/run.log`; prompt: `codex-r2/prompt.md`; the reviewed text:
`design.r2-reviewed.md`. (A first round-2 run was stopped after a few minutes, before it wrote anything, so that round 2
could review the cache-stability requirement too: `codex-r2/run-aborted.log`.) Codex opened by listing §4.5's seven
envelope rules, the §4.7 audit rows and the §10 step ids, which shows it read the current text.

**Verdict: adopt with changes.** "The architecture and evidence-backed D6 amendment are sound; the cache contract needs
correction. BLOCKERS remain: the provenance fallback and envelope principal binding." Of round 1's findings, 3, 5, 6, 7,
8, 10, 11 and 13 were resolved, and 1, 2, 4, 9 and 12 partly, each partial one pointing at a new finding below. On D6:
"§§0, 4.7 and 12.2 explicitly connect the production evidence to append-only/off replay and client compaction. That is
sufficient justification."

| # | Severity | New finding (short) | Response (this version) |
|---|---|---|---|
| N1 | blocker | Placement history does not prove provenance (a client can append imported ciphertext); waiting one placement lifetime does not drain sessions | **Accepted.** §4.3: provenance only from observed output (native tap, non-streaming JSON and native chat included) or a verified envelope; the ledger entry commits before the terminal event is forwarded; legacy state is never promoted, only allowed per route as labelled weaker assurance during a transition; R3b readiness is measured coverage, not time |
| N2 | blocker | The envelope does not bind the principal (`conversationHash` hashes only the client id) | **Accepted, verified** (`api-openai:46` vs `inference:318`). §4.3 and §4.5: the ledger key and the envelope's MAC'd `binding` are `(namespace, principalId, conversation, epoch)`; callers without a conversation id key on the principal; forks inherit through verified parent provenance; the route is deliberately not bound, and §9.1 tests both kinds of route change |
| N3 | important | Per-item expiry recreates a sliding window; key retention by creation age is wrong | **Accepted.** §4.5 rules 3 and 5: no per-item expiry; keys retained until every conversation holding their envelopes is idle past the replay lifetime, measured from last use; recovery is one conversation-level reset (refusal, or one strip plus an epoch increment) |
| N4 | important | The formal invariant is too strong in one way and too weak in another; three contracts; "one reply" is not a bound for recurrent models | **Accepted.** §4.7: `f(h) ≼ f(h ⊕ δ)` at complete message boundaries under a fixed policy; contracts for content, rendered tokens and the generated reply, tested separately (§9.1 item 5); the one-reply claim withdrawn |
| N5 | important | Native `restore-envelope` must be narrow; R1's "never edits" contradicts it | **Accepted.** §4.5 rule 2: byte replacement only when format and item shape match, otherwise a qualified rebuild (mandatory text-digest check) or refusal; §6 R1 names both native exceptions |
| N6 | important | "Pure per-item" needs a frozen policy (foreignness, replay settings and translator versions can change) | **Accepted.** §4.7: the policy epoch; a change is a declared reset; namespace flattening resolves each historical call from its own recorded namespace |
| N7 | important | The audit is incomplete (`additional_tools` hoist, suffix-dependent reasoning attachment, response-side suppression and cuts, memory-only cache salt) | **Accepted, verified** (`protocol:101`, `:193`; `inference:180`, `:228`). Rows added; the tool-image row split into its two cases |
| N8 | important | Stable content is necessary, not a cache-hit guarantee; `cache_control` markers excluded; breakpoint limits; `prompt_cache_key` is model-dependent; the hold rule is compatible; R6 should say first flush | **Accepted.** §4.7 paragraph and rows; §6 R6 reworded |
| N9 | important | Migration still imports native-first's numeric-`lastTurns` fallback; R8 is behavioural; rollback after new state-bearing output | **Accepted.** §10: the native-first path is "amended by R0b"; R8 marked as a behaviour change; a rollback-contract paragraph (stop emitting, keep decoding) |
| N10 | important | DeepSeek's Responses API treats `developer` as `user` | **Accepted, verified** ("`developer` is treated as `user`"). §2.3 and §5.1: a known loss that an agent route must allow explicitly or refuse |
| N11 | nit | Reduce scope: defer envelopes until needed; only the decoders active callers need; the gateway survey is not normative | **Accepted.** §0 and §10: envelope machinery deferred to R5; R1 builds the Responses and chat decoders first. §2 stays as evidence; nothing in it is a requirement |
| (9, partly) | — | "Lossless function tools" needs the schema subset stated | **Accepted.** §7: lossless for the subset both ends document; a keyword outside it is refused, never relaxed |

**No third round was run** (the brief allows two). Both round-2 blockers had concrete fixes, which are applied above and
not yet re-reviewed. A codex review of the ADR (R0), as the owner's standing rule requires for each plan and
implementation, will cover them.

Codex's "claims verified" in round 2 included: the sliding-window selection stopping at the last user message
(`compat/index.ts:213`); the system merge (`:191`); reasoning attachment and grouping (`protocol/index.ts:101`); the
`additional_tools` hoist (`:193`) and image relocation (`:194`); the system hoists (`codex.ts:252`, `common.ts:267`);
suppressed later reasoning (`api-openai/index.ts:490`); the conversation hash without the principal (`api-openai:46`)
against placement keys with principal and namespace (`inference/index.ts:318`); idle refresh with asynchronous
persistence (`:345`); the memory-only salt (`:180`, `:228`); llama.cpp's token-prefix comparison and checkpoint and
full-prefill fallbacks (`server-context.cpp` L4105, L4252, L4292); the local Messages losses; and the vendor caching and
thinking rules (OpenAI and Anthropic prompt caching, preserved thinking, thinking controls, DeepSeek Responses). It did
not verify live acceptance or envelope interoperability with the deployed clients.

---

## 13. Owner decisions (2026-11-04)

1. **D6 amendment: confirmed.** The cache-stability invariant (§4.7) is part of the standard: no router-side reasoning
   window anywhere; replay is append-only or off; context is bounded by client compaction. R0b implements it.
2. **Pinning.** Agent routes pin a conversation that holds provider state to its seat: it waits in the queue, then fails
   loudly. Chat routes may move it to another seat and strip the state as a declared, journaled mutation (§6 R5, §10
   R3b).
3. **No OpenAI or Anthropic API-key accounts are planned.** R5 (the anthropic encoder and with it all envelope
   machinery) and R7 (the OpenAI `responses` wire) stay unscheduled.
4. **`/v1/messages` serves local routes only** (#128's scope), each after its quirks are qualified (§4.6). Not DeepSeek.

### 13.1 The reviewers' findings on PR #1602, and what changed

Findings 1-8 are reviewer-claude's on `ff2bbe36`; finding 9 is reviewer-codex's on `ff2bbe3` and `dbb318f4`.

| # | Finding | Change |
|---|---|---|
| 1 | R3b still gated on time | §10 R3b: measured readiness (≥ 99 % ledger hits over a day, or legacy sessions drained or compacted), the per-route legacy mode |
| 2 | `tool-images-to-user` relocation could violate contract 1 | §7: one user message after the **complete** run, never between tool messages; partial runs with images refused; why that is prefix-stable |
| 3 | The envelope-key risk still described retention by key age | §11 rewritten to match §4.5 rule 5 (retention from last use) |
| 4 | The prompt-cache check used the withdrawn "one reply per turn" bound | §9.3: a per-request bound (uncached ≤ prompt growth + 3k) and a task total within 1.5× the measured append-only baseline (75-85k) |
| 5 | R4 overstated who keeps reasoning, and did not depend on R3b | §10 R4: narrowed to Responses clients, opted-in chat clients that replay message objects, and the Assistant once it stores reasoning items; depends on R3b on the same routes |
| 6 | Ledger expiry at 24 h would make a resumed Codex session fail every turn | §4.3: retention follows the conversation's replay lifetime (`ledgerRetentionDays`, default 30, refreshed on use); re-binding to the producing seat; beyond retention, a refusal naming the remedy (agent routes) or one declared strip (chat routes) |
| 7 | The thinking mode could still switch before the first tool turn | §4.7, §5.2, §6 R9: fixed on the conversation's first request, from whether it carries `tools` |
| 8 | "§6 R11" should be R10 | §5.1 and §10 fixed |
| 9 | Provenance was committed only before the terminal event, but earlier events (`output_item.done`, a signed `content_block_stop`) already make state replayable; a disconnect or crash in between left unrecorded state, and agent routes would then refuse with `reasoning_unknown_origin` | §4.3: a durable commit per item before the event that completes it; buffering rules for signatures and plaintext; open-item rows; a write failure ends the stream before the state is replayable; crash consistency; the interaction with the hold rule and the failover boundary; held events are never rewritten. §9.1: an interrupted-delivery test |

### 13.2 The owner's validation requirements (2026-11-05)

| Requirement (verbatim in §9.6, §9.7) | Where |
|---|---|
| "For all providers … run short tests directly, without llm-router and with llm-router, then compare the results … the router does not negatively affect the quality of responses." | §9.6: a deterministic layer (a), a quality A/B (b) with predeclared non-inferiority margins, a direct-path setup per provider, and the comparisons that are impossible today (agentic tasks on the Claude seat; the API-key providers, which have no account); §10: a gate column on every step |
| "Run the planning test on qwen3.8-27 and flash-next again, after you fix the models configuration issues, directly without llm-router and validate the results." | §9.7: the configuration fixes as preconditions, P1-P3 direct and via the router, the benchmark's blind judge and rubric, short checks first, a pass rule per model; §10: the native-first canaries are gated on it |

---

## Appendix: sources cited inline

Accessed 2026-11-04 unless a date is given in the text. Source files read here: llm-router `origin/main` `247b19f` (exported to `llmr-main/`); llama.cpp `tools/server/server-chat.cpp` and `server-task.cpp` at b12020 (`../server-chat-b12020.cpp`, `../r/new/`); vLLM `vllm/entrypoints/anthropic/{serving,protocol,api_router}.py` at v0.28.0 (`dl-vllm/`); llm-router issue #128.

- [messages](https://agentgateway.dev/docs/standalone/main/documentation/llm/api-types/messages.md)
- [Anthropic API](https://api-docs.deepseek.com/guides/anthropic_api)
- [Responses API](https://api-docs.deepseek.com/guides/responses_api/)
- [thinking mode](https://api-docs.deepseek.com/guides/thinking_mode)
- [llm-gateway](https://code.claude.com/docs/en/llm-gateway)
- [LLM gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol.md)
- [latest-model guide](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6.1-sol)
- [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
- [Chat create](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Responses create](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-body-based-routing)
- [Vertex thought signatures](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures)
- [LiteLLM](https://docs.litellm.ai/blog/responses-api-encrypted-content-incident)
- [docs](https://docs.ollama.com/api/anthropic-compatibility)
- [docs](https://docs.vllm.ai/en/latest/serving/integrations/claude_code/)
- [NVIDIA forum](https://forums.developer.nvidia.com/t/claude-code-2-1-154-breaks-with-vllm-anthropic-compatible-endpoint-on-dgx-spark/371797)
- [internal/translator](https://github.com/envoyproxy/ai-gateway/tree/main/internal/translator)
- [v1.6.0](https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/tag/v1.6.0)
- [openai-python types](https://github.com/openai/openai-python/tree/main/src/openai/types/responses)
- [model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- [Hugging Face](https://huggingface.co/blog/open-responses)
- [changelog](https://lmstudio.ai/changelog/lmstudio-v0.3.29)
- [stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
- [preserved thinking](https://platform.claude.com/docs/en/build-with-claude/preserved-thinking)
- [streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)
- [thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)
- [OpenAI SDK compatibility](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk)
- [README](https://raw.githubusercontent.com/Oaklight/llm-comply/master/README_en.md)
- [anthropic_anthropic.go](https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/internal/translator/anthropic_anthropic.go)
- [openai_helper.go](https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/internal/translator/openai_helper.go)
- [types.go @v1.5.0](https://raw.githubusercontent.com/kubernetes-sigs/gateway-api-inference-extension/v1.5.0/pkg/epp/framework/interface/scheduling/types.go)
- [CHANGELOG](https://raw.githubusercontent.com/openresponses/openresponses/main/CHANGELOG.md)
- [SD Times](https://sdtimes.com/aaif/61588/)
- [Simon Willison](https://simonwillison.net/2026/jan/15/open-responses)
- [data plane](https://theagentrouter.ai/docs/1.0/concepts/architecture/data-plane)
- [supported endpoints](https://theagentrouter.ai/docs/capabilities/llm-integrations/supported-endpoints)
- [InfoQ, 2026-02-02](https://www.infoq.com/news/2026/02/openai-open-responses/)
- [openresponses.org](https://www.openresponses.org/)
