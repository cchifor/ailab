<!-- status: proposed (design only; no llm-router code or config changed) -->
# llm-router request handling: route, don't rewrite

**Date:** 2026-11-04 · **Scope:** `cchifor/llm-router` request path (Responses and Chat Completions front-ends, engine,
adapters, compat), as of `main` `f9eea0d` (#132, #133, #134 merged). **Audience:** the owner, and whoever implements
the migration.

The owner's brief, verbatim: *"Also analyze what's the best solution to implement the routing in llm router. Check how
other llm routers are implemented. We must avoid introducing side effects at the routing level. the router should
behave like a router, without introducing bugs, hidden issues or side effects. Analyze the web, and provide the best
solution."*

---

## 0. Summary

**Decision: a native-protocol-first router.** Recommended over the alternatives in §6.

- Each account declares, per model, the wire protocols its upstream speaks natively (`responses`, `chat`).
- When the client's protocol is native to the selected target, the router forwards the client's body **byte-for-byte
  except for declared, journaled mutations**. Routing needs only one of these, the model name. Auth is a header, not
  the body.
- It streams the upstream's response back **verbatim**, observing it through a read-only tap for usage, finish,
  first output and errors.
- It translates only when no native path exists, and only through a translator with a written **fidelity contract**
  and **golden round-trip tests**. A field the translator cannot carry is refused by name; it is never silently
  dropped.

**Why.** Every bug this week was a side effect of the translator that turns Responses into chat-completions and back.
Some were things it dropped (reasoning, replayed reasoning, `reasoning_effort`); one was a shape it invented (split
assistant history). The router's legitimate jobs do not need that translator when the upstream already speaks the
client's protocol:
- auth;
- mapping a model alias to an account and model;
- the pool, failover and the admission queue;
- budgets and limits;
- the journal;
- the vision, Assistant and image gates;
- drain.

The rest of the industry draws the same line (§4 has the evidence):
- **The infrastructure-grade gateways avoid mutating at all where they can.**
  - Envoy AI Gateway patches only `model` in the original bytes on a same-schema route and otherwise sends the body
    untouched.
  - Kong added a native `llm_format` pass-through that keeps analytics.
  - LiteLLM forwards Responses "untranslated" to deployments that declare it.
  - Cloudflare's provider endpoints and Helicone's proxy are base-URL swaps.
- **The normalisers carry exactly this week's bug class.**
  - LiteLLM's Responses → chat bridge shipped "each message item in its own choice" (our bug #4) and a dropped
    Codex `additional_tools` item, and it silently drops unsupported params *for Codex's user agent*.
  - Portkey drops unmapped params.
  - OpenRouter ignores unsupported params unless `require_parameters`.
- **The model vendors agree on reasoning.** OpenAI, Anthropic and Gemini all say to send reasoning back unmodified
  and in order. So does OpenRouter, for every model it serves.

A further finding changes the picture: **llama.cpp b12020's own `/v1/responses` and vLLM 0.28's already do what our
translator gets wrong.** They map replayed reasoning items to `reasoning_content`, `reasoning.effort` to the
template, and stream reasoning as Responses events (§5).

**It is measured.** On P1, Codex 0.166 straight to llama.cpp's `/v1/responses` delivered the plan at 15.4 min. Through
the router it delivered nothing in 90 min. Native works. Because llama.cpp replays every reasoning block, the
context still grows, so native on cloud3 needs a context bound (§12.1).

**The owner decided D1-D6 on 2026-11-04** (§13): adopt this design; forward unknown fields; return the served model
name; translate only through qualified translators on agent routes; fix the template instead of merging system
messages; bound reasoning replay at the router.

**Codex cross-validation: adopt with changes** (§14). The rollout is gated on three things:
- context capacity: the advertised window is today the whole two-slot pool;
- enforced, not descriptive, qualification;
- a rollback that resumed sessions survive.

A thin native Responses path comes first (§11, critical path).

**What changes.** The changes are small and reversible (§11, which also gives the shorter critical path):
1. a golden round-trip suite that pins today's behaviour;
2. a mutation registry and journal event;
3. stopping today's silent drops;
4. a per-account, per-model `wire` declaration with late binding of the body;
5. a Codex → llama.cpp `/v1/responses` canary on cloud3 (after a cloudlab template fix), then cloud2 (vLLM) once it
   is qualified;
6. chat-client pass-through.

**The stopgap** (llm-router #135/#136, merged, not yet released) fits this direction. It adds
`replayReasoning: boolean | {lastTurns: N | 'all'}` and `reasoningEffort`. Keep it as the translator's fix (§12):
- **Replay bounded by `lastTurns`.** The template cannot bound replay in an agent session, and replaying every block
  overflows cloud3's pool.
- **Effort is not used for this.** `medium` did not change the outcome.

It is not the end state for Codex on llama.cpp: the native path replaces it there.

---

## 1. Why now: this week's bugs were all translation side effects

The router serves the Codex CLI (Responses), Open WebUI, dsh and the Assistant (Chat Completions, or the canonical
internal request). For the Codex/OpenAI subscription accounts it is already a pass-through (`native: ['codex-responses']`
routes, `adapters/providers/codex.ts:448-529`). For the local accounts it **translates**:
- cloud3, llama-swap at `http://192.168.0.26:8080`, which fronts llama.cpp b12020 for `qwen3.8-flash-next` and vLLM
  0.28 for `qwen3.8-27b-fp8`;
- cloud2, vLLM 0.28 at `http://192.168.0.28:18020`.

| # | Bug | Mutation that caused it | Status |
|---|---|---|---|
| 1 | Long thinking looked like a stall; Codex's 300 s idle timer killed turns | Response side: the chat adapter read only `content`/`refusal`/`tool_calls`, so reasoning deltas were dropped | Fixed: #131 (progress events), #132 (`forwardReasoning`) |
| 2 | flash-next re-derives its decisions each turn and never delivers a plan | Request side: echoed reasoning items / `reasoning_content` are **dropped** (`protocol/index.ts:33-53`, `:88`, `:161`) | Open; stopgap in flight |
| 3 | Models run at their template default (`xhigh`) whatever the client asks | `compat/index.ts:54`, `local: { reasoningEffort: false }`, drops `reasoning_effort` (disclosed in a header no client reads) | Open; stopgap in flight |
| 4 | qwen-27b ended turns with a narration and no tool call (8/48 vs 0/48) | Request side: one reply split into two assistant messages | Fixed: #130 |
| 5 | GGUF template raises on a mid-conversation system message | Not a router bug, but the router's fix (`systemMessages: merge`) **moves** text to the head of the prompt | Lossy mitigation, live |

The flash-next evidence for #2 is in `scratchpad/flashnext-plan-investigation/`:
- **Mechanism.** The memory note `flash-next-agent-failures-2026-11-02.md` has the root-cause section. The GGUF
  template (`chat_template.jinja:113-120`) renders every past assistant turn as
  `<think>\n` + `reasoning_content` + `\n</think>` whenever `preserve_thinking` is undefined or true. llama.cpp
  reports `supports_preserve_reasoning: true` for this template (`props.json`). When the router drops the reasoning,
  the model therefore does not merely lose its earlier reasoning. It sees **an empty `<think></think>` on every past
  turn**, which is a strong in-context signal in its own right. That rendering is a side effect no one chose.
- **Probe.** These are the replay A/B probe's results at the decision point K=46:

  | Arm | Committed the plan | Reasoning before acting |
  |---|---|---|
  | `asis` (reasoning dropped, as the router sends it) | 1/6 | 48-233 s |
  | `last1` (last block restored) | **6/6** | 1.2-3.2 s |

  The arms' thinking times do not overlap at all. Details, caveats and the direct-path run are in §12.1.

---

## 2. What the router legitimately does

These are the router's own jobs. None of them requires changing what the model sees:

| Job | What it needs from the payload | Must it change the body? |
|---|---|---|
| Auth (principal, keys, namespaces) | headers | No. The upstream credential is a header the adapter sets. |
| Alias → account/model, pointers, model mode | `model` (read) | **Yes, `model` only.** Some upstreams reject unknown names: vLLM 404s, and llama-swap's `useModelName` exists for that reason (`llama-swap-main.yaml:320-325`). |
| Pool, strategy, conversation placement | `model`, `prompt_cache_key`/`thread-id` (read) | No. One exception: the Codex re-key salts `prompt_cache_key` after overloads (`inference/index.ts:114-116`), which is a routing-motivated, journaled mutation. |
| Failover before first output | upstream events (read) | No. It holds back lifecycle events (`codex.ts:439-446`), a timing change, not a content change. |
| Admission queue, concurrency, busy 429 | nothing | No |
| Budgets, quota windows, usage | `usage` in the response (read) | Chat streams need `stream_options.include_usage` upstream (§9.3). |
| Output-limit enforcement (only for the `codex` **provider**, whose API has no output limit) | visible output (read) | Truncates the stream; declared (`x-router-output-limit`; §8.2 `output-limit-cut`) |
| Journal, telemetry, `explain_request` | everything (read) | No |
| Vision / tools / Assistant / image-model gates | facts about the request: images, tools, agent id (read) | No |
| Progress keepalives (#131) | nothing | Adds events to the stream (§9.4) |
| Drain | nothing | Cuts the stream without a terminal event (deliberate: Codex reconnects; §8.2 `drain-cut`) |

The only body change routing **requires** is the model name. Everything else the router does to a body today is a
translation choice, a compatibility workaround, or a bug.

---

## 3. Every place the router changes the payload today

**Classes.**
- **N**: necessary, kept in every mode.
- **P**: avoidable with native pass-through.
- **L**: avoidable by a lossless translation.
- **R**: a bug risk: silent, lossy, or not relative to the upstream.

Most rows carry two classes. Line numbers are `main` `f9eea0d`.

### 3.1 Request direction

**Codex adapter.** `adaptCodexResponses`, `protocol/index.ts:133-190`. It runs when `originator` starts with `codex_`
and the route is not Codex-native.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| Drop `prompt_cache_key`, `client_metadata` | `:137` | journal `changes` + `x-router-adapted` | P (harmless hints; placement reads them first) |
| Drop `reasoning.summary`, `reasoning.context`, `text.verbosity` | `:138-142` | journal | P |
| Drop `include: ["reasoning.encrypted_content"]` | `:143` | journal | P / N (the router emits no encrypted reasoning) |
| Drop every non-function tool: `web_search`, `namespace` groups (Codex's multi-agent tools), `custom` freeform tools | `:145-156` | journal, by tool name | **R**: the model silently loses tools. L for namespaces (flatten, then re-wrap). N for hosted tools a local model cannot run. |
| Drop **reasoning input items** | `:161` | journal `input:reasoning` | **R: bug #2.** L (map to `reasoning_content`) or P. |
| `additional_tools` items merged into `tools` | `:162` | journal | L |
| `function_call_output` content parts → joined text; images **moved** into a synthetic user message after the run of tool results | `:165-179` | journal | **R** (role/order change; it also shifts Qwen's `last_query_index`). P if the upstream takes parts. |
| Refuse `tool_choice` that names a dropped tool | `:184-188` | 400 | fine (loud) |

**Subset parser.** `parseRequest`, `protocol/index.ts:54-112`.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| `null` controls deleted (null = absent) | `:56` | no | N (semantics-preserving) |
| **Allowlist of top-level keys.** Anything else is a 400. This blocks `top_k`, `min_p`, `chat_template_kwargs`, `extra_params`, `reasoning_format`, `return_progress` and so on. | `:57-58` | 400 | Loud (good), but **P**: blocks upstream features |
| `user`, `metadata` validated, never forwarded | `:20-25`, `:61-62` | documented | N (declare it) |
| `n`, `logprobs`, `store`, `service_tier` accepted only as no-op values | `:63-65` | 400 otherwise | N |
| Chat assistant `reasoning_content` / `reasoning` **dropped** on replay | `:33-38`, `:79` | no | **R: bug #2 for chat clients.** P/L. |
| Message fields other than role/content/tool_calls/tool_call_id refused (`name`, legacy `function_call`); `type`/`id`/`status` silently stripped | `:6-13` | 400 / silent | mostly fine; P |
| Content parts other than text and user `image_url` refused (`input_audio`, files, video) | `:10` | 400 | P (llama.cpp takes audio and video, per the README) |
| Responses `instructions` → leading system message; string `input` → user message; `input_text`/`output_text` → text; `input_image` → `image_url` (**`detail` dropped**; `file_id` refused) | `:81-88` | no | L (detail: R, minor) |
| Consecutive `function_call`s grouped into the preceding assistant message (#130) | `:88` | no | L (the fix for bug #4; a translation choice) |
| Function tools flattened from the Responses shape to the chat shape; non-function tools refused | `:93` | 400 | L |
| `text.format` → `response_format` (`json_schema` keeps name/schema/strict; **`description` dropped**) | `:106` | no | L (R, minor) |
| `reasoning`: only `effort`; `reasoning.summary` refused unless the Codex adapter dropped it | `:108` | 400 | P |
| Output limit: `max_tokens` / `max_completion_tokens` / `max_output_tokens` unified, then sent as `max_tokens` | `:74-76`, `chatcompletions.ts:142` | no | L |
| `stream_options`: only `include_usage` accepted | `:71` | 400 | P |

**Per-target adaptation.** `adaptRequest` / `planControls`, `compat/index.ts:134-203`.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| **"Neutral" values dropped, never disclosed:** `top_p: 1`, penalties `0`, `parallel_tool_calls: true`, `tool_choice: "auto"` | `:109-118`, `:148` | **no** | **R.** "Neutral" means OpenAI's default, not the upstream's: llama.cpp's default `top_p` is 0.95 (README; cloud3's flash-next runs `--top-p 0.95`), so a client's explicit `top_p: 1` silently becomes 0.95. `parallel_tool_calls` has no documented llama.cpp default (it is verified against the template), so dropping `true` is not provably neutral. P. |
| `reasoning_effort` dropped for `local` | `:54`, `:153` | `x-router-ignored-params` + journal | **R: bug #3.** The table is stale: llama.cpp documents `reasoning_effort` ("the value is made available to the jinja template"), and the template lists `reasoning_effort_levels: [low, medium, xhigh]`. P. |
| Account `params.drop` / `params.reject` | `:144-149` | journal + header | **N: the right model** (explicit, per account, opt-in, disclosed) |
| `systemMessages: merge` folds every system/developer message, in order, into one leading system message, moving mid-conversation text to the head | `:182-189`, `:200` | `x-router-adapted: system-messages-merged` | Needed only for the current GGUF template, and lossy (position changes meaning). **D5: retired** once the cloudlab template fix lands (§10.3). |
| `tool_choice` / `parallel_tool_calls` removed without tools | `:122-126` | no | N (they mean nothing without tools) |

**Chat adapter.** `chatcompletions.ts`, with `common.ts` for message conversion.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| `model` → deployment model | `:74` | journal | **N** |
| `developer` → `system` for local and deepseek | `:64-65`, `common.ts:204` | no | L/R (the Qwen template accepts `developer`; other templates may not, so make it per account) |
| `content: null` → `''` | `common.ts:206` | no | R, minor (template-visible on some templates) |
| Always `stream_options: {include_usage: true}` upstream | `:155` | no | N for budgets (declare it; §9.3) |
| `store` never sent | `:153` | — | N |

### 3.2 Response direction

**Chat adapter → events.** `chatcompletions.ts`.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| Only reasoning (opt-in), content, refusal, tool_calls, finish and usage are read; everything else is dropped (llama.cpp `timings`, `prompt_progress`, annotations, logprobs, vendor fields) | `:251-268`, `:296-312` | no | P |
| **Unknown `finish_reason` → `"stop"`** | `:99-113` | no | **R** (silent, and the raw value is lost; an `error`/`abort` would read as a normal stop) |
| Upstream error bodies hidden; three template sentences recognised | `:181-204` | router sentence | N for the journal; P for the client (it is the client's own conversation) |

**Responses synthesis.** `api-openai/index.ts`.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| The router builds the event stream itself: ids `resp_<trajectory>`, `msg_…`, `rs_…`; tool item `id` = `call_id` (OpenAI uses `fc_…` vs `call_…`); `call_<index>` when the upstream gave none | `:241`, `:255-261`, `:509-538` | no | L (P for native) |
| One leading reasoning item; **reasoning after visible output is not forwarded on Responses** (only counted) | `:490-497`, `:518-523` | no | R (interleaved thinking); P |
| Response `model` = the name the client asked for, not the upstream model | `:257-260`, `:488` | the upstream model is in the journal and `x-router-route` | N (the alias contract; declare it) |
| Usage reshaped; `output_tokens_details` left out for Codex when not reported; extra `router_usage_confidence` / `router_usage_scope` fields | `:250-253` | — | L (additive fields are N) |
| `response.in_progress` events injected while the upstream thinks (#131); `: ok` / `: keepalive` SSE comments | `:462-507` | documented | N for translated streams. Comments are invisible to parsers. §9.4 covers native. |
| Chat chunks rebuilt: `id: chatcmpl_<trajectory>`, `created` = now, `model` = requested; usage chunk only if the client asked; `[DONE]` | `:488`, `:516` | — | L (P for native) |
| In-stream errors: router codes and messages (`response.failed`, chat `{error}`) | `:540-556` | — | N (router-originated failures); P for upstream ones |

**Native Codex path.** Already pass-through: `protocol/index.ts:204-219`, `codex.ts:448-529`.

| Mutation | Where | Disclosed? | Class |
|---|---|---|---|
| `model` replaced; `store: false`, `stream: true` forced (both already validated, so in practice only `model` changes) | `codex.ts:471` | — | N |
| Headers: allowlist; `originator: codex_cli_rs` set if the client sent none; credentials replaced | `codex.ts:463-470`, `protocol:197-218` | — | N |
| `prompt_cache_key` salted after `REKEY_AFTER` overloads | `inference:114-116` | journal `conversation.rekeyed` | N (routing-motivated, declared) |
| Lifecycle events held until the first other event (enables failover) | `codex.ts:488-508` | — | N (timing only) |
| `server_is_overloaded` → `server_error` after output (upstream code kept as `upstream_code`) | `codex.ts:424-431` | `upstream_code` field | N (deliberate client-behaviour fix; declare it) |

### 3.3 What the inventory says

- **About 35 distinct mutations.** About 25 exist only because the router rebuilds the body from its own subset type
  (`InferenceRequest`), and disappear on a native path.
- **The bug-risk rows share one shape.** Each is something the translator could not represent, so it was dropped
  (reasoning, tools, effort), or something it represented differently (split messages, neutral values, finish
  reasons).
- **The native Codex path has five mutations,** each routing-motivated and each documented. It has not produced a
  translation bug.

---

## 4. How other routers and gateways do it

The research ran 2026-11-04. Most vendor docs pages carry no date; "acc." means accessed 2026-11-04. Release tags and
page dates are given where they exist. Full URLs are in the appendix.

### 4.1 LiteLLM proxy: a normaliser with escape hatches, and this week's bugs in its changelog

- **Design.** OpenAI-format in, a per-provider transform out. A deployment can opt out:
  `model_info.supported_endpoints: ["/v1/responses"]` "forward[s] the request untranslated". Deployments without a
  native Responses config (`openai_like/`, `custom_openai/`) are "bridge[d] … through `/v1/chat/completions`"
  ([Responses API docs](https://docs.litellm.ai/docs/response_api), acc.).
- **Unknown or unsupported params.**
  - The default raises `UnsupportedParamsError`. `drop_params` drops instead; `additional_drop_params` (JSONPath)
    drops more; `allowed_openai_params` passes a param "as is".
  - **However, "Requests from Claude Code and Codex, found by their user agent, drop unsupported params by default … even
    when `drop_params` is unset"**, and a bug from v1.103.0 to v1.108.x ignored the override (fixed v1.109.0).
  - Nothing reports what was dropped ([drop_params](https://docs.litellm.ai/docs/completion/drop_params), acc.).
  - That is the same silent-drop-for-Codex behaviour as our `adaptCodexResponses`, without even our journal entry.
- **Reasoning.**
  - It normalises to `reasoning_content`, plus Anthropic `thinking_blocks`, which clients must send back on tool
    turns or the provider 400s.
  - `modify_params=True` "fixes" that by removing `thinking` for the turn: the model silently stops reasoning.
  - An effort level a deployment does not declare "is dropped rather than rounded"
    ([reasoning_content](https://docs.litellm.ai/docs/reasoning_content), acc.).
  - `merge_reasoning_content_in_choices` folds reasoning into `<think>` content; it crashed streaming with vLLM QwQ
    ([#9578](https://github.com/BerriAI/litellm/issues/9578)).
- **Failover.** Fallback drops encrypted reasoning: items "keep their summary text and lose only their encrypted payload
  and id", and the turn "is served in degraded form rather than failed". That includes a stream that broke mid-stream
  ([reliability](https://docs.litellm.ai/docs/proxy/reliability), acc.).
- **The Responses → chat bridge has had our exact bugs:**
  - **v1.105.2:** the bridge "put each Responses message item in its own choice", so tool calls landed on
    `choices[1]`. The fix folds "message text, annotations, reasoning and tool calls" into one `choices[0]`. That is
    the class of our split-history bug #4
    ([release notes](https://docs.litellm.ai/release_notes/v1.105.2/v1-105-2)).
  - **v1.101.6:** the bridge dropped Codex's `additional_tools` input item, so bridged models never saw the tools
    ([release notes](https://docs.litellm.ai/release_notes/v1.101.6/v1-101-6)).
  - **[#27276](https://github.com/BerriAI/litellm/issues/27276)** (opened 2026-05-06, open; a 2026-09-02 comment says
    it is still unfixed on main): the bridge "drops tool names and forwards unsupported tool types (custom, shell)".
  - **[#16810](https://github.com/BerriAI/litellm/issues/16810):** an automatic switch to the bridge broke structured
    outputs; the user asked for a switch to turn bridging off.
- **Pass-through endpoints.** "No translation is done", though the same page also says "Request is reformatted", so
  treat "verbatim" as unverified. Keys, permissions and logging still apply. Cost is tracked where the provider table
  allows, else traffic is "logged without cost". Errors are forwarded "with original error codes"
  ([pass-through](https://docs.litellm.ai/docs/pass_through/intro), acc.).

### 4.2 OpenRouter: a normaliser that preserves the original next to the normalised value

- "OpenRouter normalizes the schema across models and providers"; an unsupported parameter is ignored.
  `finish_reason` is normalised, and **the raw value is kept as `native_finish_reason`**
  ([API overview](https://openrouter.ai/docs/api-reference/overview), acc.). That is a cheap way to keep a mapping
  honest; our `?? 'stop'` keeps nothing.
- **`provider.require_parameters`** (default `false`): with it, "the request won't even be routed to that provider"
  if the provider lacks a parameter ([provider routing](https://openrouter.ai/docs/features/provider-routing), acc.).
  This is our `compat: strict`.
- **Unified `reasoning`:** `effort` / `max_tokens` / `exclude` / `enabled`. **`reasoning_details`** must be sent back
  unmodified: "you cannot rearrange or modify the sequence of these blocks"; for Responses, "Keep each `reasoning`
  item's `content`, `summary`, `signature`, and `encrypted_content` unchanged". The reason given is tool calling:
  they "capture the model's step-by-step reasoning that led to tool requests"
  ([reasoning tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens), acc.).
- **Responses API:** a beta, stateless `/api/v1/responses` that resends the full history (OpenResponses format).
- **Streaming failover:** "If an attempt fails before any tokens reach you, OpenRouter automatically tries a backup
  provider"; "Failover also stops once part of the answer has reached you"
  ([errors](https://openrouter.ai/docs/api/reference/errors-and-debugging), acc.). That is our rule.

### 4.3 Portkey AI Gateway: map-based transforms drop silently; the proxy path does not

- Verified in source (`main`, acc. 2026-11-04): each provider has a `ProviderConfig` param map, and
  `transformToProviderRequest` iterates only the map, so **unmapped params are silently dropped**. The `proxy` path
  returns params unchanged (`if (fn === 'proxy') { return params; }`)
  ([transformToProviderRequest.ts](https://raw.githubusercontent.com/Portkey-AI/gateway/main/src/services/transformToProviderRequest.ts)).
- **`strict_open_ai_compliance` defaults to true** and removes Anthropic thinking `content_blocks` from responses.
  Thinking needs the flag off, and multi-turn needs "the previous thinking content" sent back with its signature
  ([thinking mode](https://portkey.ai/docs/product/ai-gateway/multimodal-capabilities/thinking-mode), acc.; beta).
- Fallback and retry decide on HTTP status; no streaming branch exists, so fallback can only happen before the body
  streams (inferred from
  [handlerUtils.ts](https://raw.githubusercontent.com/Portkey-AI/gateway/main/src/handlers/handlerUtils.ts)).

### 4.4 Kong AI Gateway: translation by default, native pass-through as a first-class mode

- `ai-proxy` "transforms requests and responses according to the configured `config.model.provider` and
  `config.route_type`" (route types include `llm/v1/responses` since 3.11)
  ([ai-proxy](https://developer.konghq.com/plugins/ai-proxy/), acc.).
- **`config.llm_format`** (3.10+): with a non-OpenAI format, the plugin "passes requests upstream without
  transformation", and "will still provide analytics, logging, and cost calculation". The older `preserve` mode was
  fixed twice, then deprecated in favour of `llm_format`
  ([changelog](https://developer.konghq.com/plugins/ai-proxy/changelog/)).
- The same changelog lists translation bugs:
  - Anthropic `stop_reason` mis-mapped;
  - Gemini and Bedrock streams arriving as one chunk;
  - Azure streams missing tokens;
  - Gemini reasoning tokens left out of `completion_tokens`.

  Each is the kind of bug a pass-through cannot have.

### 4.5 Envoy AI Gateway (now "Agent Router"), Gateway API Inference Extension / llm-d, vLLM Semantic Router

- **Envoy AI Gateway.** The project was renamed Agent Router; docs are at theagentrouter.ai
  ([repo](https://github.com/envoyproxy/ai-gateway), acc.).
  - **Routing.** ext_proc reads the body's `model` into the `x-ai-eg-model` header, and routes match on the
    header.
  - **Per attempt.** Translation and auth run in an upstream-level filter, per attempt, because a retry may go to
    a different provider
    ([data plane](https://theagentrouter.ai/docs/concepts/architecture/data-plane), v1.2 docs).
  - **No mutation on same-schema routes.** In OpenAI → OpenAI the body is changed only when a model override is
    set: one `sjson.SetBytesOptions(original, "model", …)` patch on the original bytes. Otherwise `newBody` is
    `nil`, meaning no mutation
    ([openai_openai.go](https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/internal/translator/openai_openai.go)).
    That is exactly §8.1 rule 1.
  - **Responses translators** exist only to OpenAI, AWS-OpenAI and Azure-OpenAI; no Responses → chat bridge was
    found.
- **Gateway API Inference Extension / llm-d.** Body-based routing "sets the X-Gateway-Model-Name header based on the
  body but does not mutate the body". The endpoint picker may override the body's model name when a model-rewrite
  rule matches. This is partly verified only, from mirrors and a deploy preview. GKE notes that bodies over ~250 KB
  may not get the header injected
  ([GKE body-based routing](https://cloud.google.com/kubernetes-engine/docs/how-to/configure-body-based-routing)).
- **vLLM Semantic Router** (ext_proc).
  - **What it mutates.** It "rewrites the `model` field in the body"
    ([agentgateway blog, 2026-07-28](https://agentgateway.dev/blog/2026-07-28-agentgateway-semantic-router-integration/)).
    It can inject system prompts and `reasoning_effort` / `chat_template_kwargs`; that part is from deploy-preview
    docs and was not verified live.
  - **Visibility.** `x-vsr-selected-model`, `x-vsr-client-protocol` / `x-vsr-upstream-protocol` and
    `x-vsr-protocol-warnings` are on by default.
  - **But the prompt injection itself is visible only with `x-vsr-debug: true`**
    ([vsr headers](https://vllm-sr.ai/docs/troubleshooting/vsr-headers), acc.). That is the anti-pattern: the most
    consequential mutation is the least visible.

### 4.6 Cloudflare AI Gateway

- **Provider-native endpoints** (`…/{gateway}/openai/responses` etc.) are a base-URL swap; the docs describe no
  transformation ([OpenAI provider](https://developers.cloudflare.com/ai-gateway/usage/providers/openai/), updated
  2026-04-20).
- **The unified `/compat/chat/completions`** normaliser "is deprecated" for single-model chat
  ([chat completion](https://developers.cloudflare.com/ai-gateway/usage/chat-completion/), updated 2026-10-02).
- **Fallbacks** go through the Universal endpoint, and `cf-aig-step` says which step served
  ([fallbacks](https://developers.cloudflare.com/ai-gateway/configuration/fallbacks/), updated 2026-04-20).
- **Logs** hold prompt, response, tokens, cost and duration; `cf-aig-collect-log-payload: false` keeps metadata only
  ([logging](https://developers.cloudflare.com/ai-gateway/observability/logging/), updated 2026-11-04).

### 4.7 Helicone

- **The original proxy** is a base-URL swap with `Helicone-Auth` (and `Helicone-Target-Url` for any upstream). It
  observes and forwards; it is now "maintained but no longer actively developed"
  ([proxy vs async](https://docs.helicone.ai/references/proxy-vs-async.md), acc.).
- **The new Rust AI Gateway** (public beta) **does** translate ("converts your request to the correct provider
  format") and fails over; no Responses support was found
  ([gateway overview](https://docs.helicone.ai/gateway/overview), acc.).

### 4.8 TensorZero (archived 2026-06-12)

- **Every provider-specific change is a declared, scoped patch:** `extra_body` entries are JSON-Pointer `pointer` +
  `value` or `delete = true`, scoped to a variant, a model or a model+provider. "The values provided at
  inference-time take priority over the values in the configuration file"
  ([inference API](https://raw.githubusercontent.com/tensorzero/tensorzero/main/docs/gateway/api-reference/inference.mdx)).
  That is the closest prior art for §8.2's registry.
- **Reasoning is a first-class `thought` block** (text, signature, summary). Clients "Pass through the `signature` …
  to continue a reasoning conversation". An `unknown` block is forwarded only to the providers it names.
- **The repo is read-only now** ([repo](https://github.com/tensorzero/tensorzero)), so it is prior art, not a
  dependency.

### 4.9 Failover after the first byte

- **OpenRouter:** no ("stops once part of the answer has reached you").
- **Portkey:** status-based only, so effectively no.
- **LiteLLM:** yes, and it drops encrypted reasoning when it does.
- **Cloudflare and TensorZero:** not documented.

Our rule (no failover once anything was emitted, `inference/index.ts:851`) matches the conservative end and should
stay.

### 4.10 What the providers themselves say about replaying reasoning

- **OpenAI** ([reasoning guide](https://developers.openai.com/api/docs/guides/reasoning), acc.):
  - "we highly recommend you pass back any reasoning items returned with the last function call", and "you should
    pass back all reasoning items, function call items, and function call output items, since the last user
    message";
  - a newer `reasoning.context` (`auto | current_turn | all_turns`) defaults to `all_turns` on GPT-5.6, which means
    "preserve every output item … and replay the complete history".
- **Anthropic** ([extended thinking](https://platform.claude.com/docs/en/build-with-claude/thinking), acc.):
  - "Pass every `thinking` block back to the API complete and unmodified"; "Modified thinking blocks are rejected with
    a 400";
  - the advice is "Make your integration append-only";
  - a beta adds **an `input_transformations` array on every response listing what was dropped**. That is the
    provider itself surfacing mutations, which is what `x-router-mutations` would be for the router.
- **Gemini** ([thought signatures](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures),
  acc.): "Don't merge a `Part` containing a signature with one that does not"; a missing signature on a function call
  is a 400.
- **Qwen3.8:** the template preserves past thinking by default, and the card reportedly calls this "especially
  beneficial for agent scenarios". That is from the 11-04 investigation (memory note); the card was not re-read here.

**Every model vendor says the same thing: send reasoning back, unmodified, in order.** A router that drops it, as ours
does today for local accounts, works against all of them.

### 4.11 Principles the research supports

1. **Silent drops are the industry norm, and the documented source of bugs.**
   - OpenRouter ignores unsupported params unless `require_parameters`.
   - Portkey drops unmapped keys.
   - LiteLLM drops for Codex and Claude Code user agents.
   - None of them says what it dropped. The exceptions are vSR's `x-vsr-protocol-warnings`, OpenRouter's
     `native_finish_reason` and Anthropic's `input_transformations`.
2. **Minimal mutation is achievable and is what the infrastructure-grade gateways do.** Envoy patches one field in the
   original bytes; Kong replaced `preserve` with native `llm_format` and kept analytics; LiteLLM and Cloudflare offer
   untranslated paths beside the normaliser.
3. **The mutation that matters most must be the most visible, not the least.** vSR's debug-only prompt-injection header
   is the counter-example.
4. **Contract tests exist for upstreams, not for translators.** The Open Responses spec (Jan 2026; launch partners
   include vLLM, Ollama, OpenRouter, LM Studio) ships a server compliance suite. There is "no client-side equivalent"
   ([Simon Willison, 2026-01-15](https://simonwillison.net/2026/Jan/15/open-responses/)). No gateway found publishes
   golden round-trip tests for its translations, so §11 Step 1 is new work, not something to adopt.

---

## 5. The upstreams: what their Responses APIs actually do

**Evidence labels.**
- **[src]:** read in source at the stated tag.
- **[doc]:** README or docs.
- **[issue]:** issue or PR text.
- **[infer]:** reasoning from source, not run.

Nothing here was sent to the cloud models.

### 5.1 llama.cpp `/v1/responses` (we run b12020; latest b12110 = v0.9.0)

**Versions.**
- b12020 is `0ec4027a`, 2026-10-31.
- b12110 and v0.9.0 are both `cd959bae`, 2026-11-04
  ([v0.9.0](https://github.com/ggml-org/llama.cpp/releases/tag/v0.9.0)).
- The only Responses change between them is `extra_params`, #30959. `server-task.cpp`, `server-common.cpp` and
  `server.cpp` are byte-identical [src].

**What it is.** A shim, like ours, but upstream and next to the template machinery [src]:
- `server_chat_convert_responses_to_chatcmpl()`
  ([server-chat.cpp L32-326 @b12110](https://github.com/ggml-org/llama.cpp/blob/b12110/tools/server/server-chat.cpp#L32-L326))
  rewrites the body to chat-completions, then runs the normal chat path. Output goes back out through
  `to_json_oaicompat_resp[_stream]()`.
- The README says it "works by converting Responses request into Chat Completions request" [doc].
- It was added in #18486 "(partial)", merged 2026-01-21. The fixes since then are #19773 (merges contiguous
  assistant items, which is our #130 fix), #23041 (skips non-function tools "for Codex CLI"), #25348 (progress in the
  stream), #22575 (`input_image` in `function_call_output`) and #30863 (`output_index`).

**Input items [src].**

| Item | Behaviour |
|---|---|
| `message` (user, system, developer) | Accepted: `input_text`, `input_image`. `input_file` throws. |
| `message` (assistant) | Accepted: `output_text`, `refusal` |
| `function_call`, `function_call_output` (string, or `input_text`/`input_image` parts) | Accepted |
| **`reasoning`** | Accepted when `summary` is an array **and `content[0].text` is a string** ([server-chat.cpp L258-282 @b12020](https://github.com/ggml-org/llama.cpp/blob/b12020/tools/server/server-chat.cpp#L258-L282), read here). The text becomes **`reasoning_content`**, which the template renders. **This is the stopgap's mapping, done upstream.** (1) **Placement:** after a user or tool message, the item opens a new assistant message, and the following `message`/`function_call` items merge into it (`merge_prev`, L70). When the previous message is already an assistant message, the text goes onto that one and *overwrites* any earlier `reasoning_content`, so interleaved reasoning within one reply keeps only the last block. Only `content[0]` is read. (2) **Refusals:** an item without content is a 400 (`item['content'] is not an array`), and so is `summary: null` (#29159, open). |
| `custom_tool_call(_output)`, `web_search_call`, `item_reference`, `local_shell_call`, `tool_search_*`, **`additional_tools`** | **400 "Cannot determine type of 'item'"**: loud, nothing skipped. #30859 is open for `additional_tools`. |

- `developer` becomes `system` for every template except GPT-OSS [src, `common/chat.cpp`].
- Qwen3.x templates **raise on a mid-conversation system message**. Codex hits this; the workaround is a patched
  template ([#27367](https://github.com/ggml-org/llama.cpp/issues/27367), closed not_planned) [issue].

**Tools [src].**
- Only `function` is converted, with `strict: true` forced when absent.
- **`custom`, `web_search`, `namespace` and `tool_search` are dropped with only a server-log warning.** That is a
  silent drop, upstream. Codex's MCP and multi-agent tools arrive in `namespace` groups and are therefore unreachable
  ([#24295](https://github.com/ggml-org/llama.cpp/issues/24295), open). PR #31568 (open, 2026-11-03) flattens
  namespaces.
- `tool_choice`: string values only; an object value falls back to `auto` [infer].
- `parallel_tool_calls` is honoured.

**Reasoning controls [src].**
- **`reasoning.effort` → `reasoning_effort` → `chat_template_kwargs.reasoning_effort`.** `none` sets
  `enable_thinking=false`. This fixes bug #3 with no router code.
- `reasoning.summary` is dropped.
- Per request it also takes `reasoning_budget_tokens`, `chat_template_kwargs` and (b12020: any top-level key)
  `return_progress`.

**Streaming [src].**
- **Emitted:**
  - `response.created`, `response.in_progress`;
  - `output_item.added` and `output_item.done` for reasoning, message and function_call items;
  - **`response.reasoning_text.delta`**;
  - `content_part.*`, `output_text.delta` / `output_text.done`;
  - `function_call_arguments.delta`;
  - `response.completed` with `usage{input_tokens, output_tokens, total_tokens, input_tokens_details.cached_tokens}`.
- **Item shapes:** reasoning items carry `summary: []`, `content: [reasoning_text]`, `encrypted_content: ""`. Ids
  are `fc_…`, call ids `call_…`.
- **Never emitted:**
  - `function_call_arguments.done`;
  - `response.incomplete` / `response.failed`: **status is hardcoded `completed` even on truncation**
    ([#30773](https://github.com/ggml-org/llama.cpp/issues/30773), open);
  - `sequence_number`.
- **Mid-stream errors** are an **untyped** `data: {"error": …}`.
- **Pings:** an SSE comment `:` every `--sse-ping-interval`, default 30 s.
- **Prompt progress:** **`return_progress: true` adds prompt-processing `response.in_progress` events.** These are
  real events, which reset Codex's idle timer during a long prefill.

**Other fields [src].**
- **Rejected:** `previous_response_id` (400).
- **Mapped:** `instructions` → a leading system message; `max_output_tokens` → `max_tokens`.
- **Copied into llama params and never read:** `store`, `include`, `prompt_cache_key`, `truncation`,
  `service_tier`, `client_metadata` and `text`. **`text.format` is not mapped to `response_format`, so Responses
  structured output is silently ignored.**

**llama-swap** (v269, 2026-11-01) [src]:
- **Routing:** `/v1/responses` is a model-routed POST route.
- **Edits only through configured filters,** applied as in-place `sjson` edits, in order: `useModelName`,
  `stripParams`, `setParams`, `setParamsByID`. A key ending in `?` is set only when absent.
- **Every other byte passes unchanged.**

That gives the operator a server-side, declared place for per-model request defaults (§9.4).

### 5.2 vLLM Responses API (we run 0.28.0, 2026-08-26; latest stable 0.32.0, 2026-10-18)

**Path.**
- `use_harmony` is for `gpt_oss` only. Qwen goes through `SimpleContext`, with the server's `--tool-call-parser`
  (cloud3 fp8: `qwen3_coder`) and `--reasoning-parser qwen3` [src @v0.28.0].
- **Function and namespace tools:** flattened for the template, and the namespace is restored on output [src].
- **`custom` tools:** silently dropped in 0.28. 0.32 supports them through a shim, but **rejects `format.type:
  "grammar"` with a 400**, and that is the format of Codex's apply_patch freeform tool
  (`codex core/src/tools/handlers/apply_patch_spec.rs`) [src].
- **Built-in tools** (`web_search_preview`, `code_interpreter`): only on the harmony or experimental contexts [src].

**Reasoning.**
- **Output:** a reasoning item `content: [reasoning_text]`, `summary: []`. In 0.28 the stream also sends
  non-standard `response.reasoning_part.*` events; a 2026-10-01 PR standardised them, after 0.28.
- **Input:** `content[0].text` (else `summary[0].text`) becomes the assistant `reasoning`, then `reasoning_content`
  for the template. **A non-empty `encrypted_content` raises "Encrypted content is not supported."**
- **Effort:** `reasoning.effort` → `chat_template_kwargs.reasoning_effort` (+ `enable_thinking`).
  `reasoning.summary` is ignored [src].

**Stream and state [src].**
- `response.created`, `in_progress`, `output_item.*`, `content_part.*`, `output_text.*`,
  `function_call_arguments.delta/done` and `response.completed` with usage and `sequence_number`.
- **The terminal event is always `response.completed`, even when the status is `incomplete`.** On length exhaustion
  the enclosed `response.status` is `incomplete`. A tap that reads only the event type, as today's Codex tap does,
  records a `stop`; the tap must read the payload's status and `incomplete_details` (codex review, §14).
- **A reply's function calls merge into the preceding assistant message:** confirmed in source by the codex review.
  The real-template render check stays.
- **The final response re-parses the output, so its item ids differ from the streamed ones.** PR #56285 (open)
  reports the Codex conformance suite failing 11/26 cases on another model's path [issue].
- `store` is forced to false unless `VLLM_ENABLE_RESPONSES_API_STORE=1` (an in-memory store that is never
  evicted).
- `previous_response_id` → 404.

**Validation [src].**
- **Top level:** `extra="allow"`; unknown top-level fields are ignored, with a debug log.
- **Nested:** strictly validated. `include` is a literal list, `service_tier` a literal, and `tools` and `input`
  use the openai-SDK unions.
- **Unknown item shapes:** in 0.28 an unmatched dict goes into the chat messages as-is, which is likely to break
  the renderer [infer]. In 0.32 it is a 400.

**Roles.** `developer` reaches the template unchanged in 0.28. Qwen3.6/3.8 templates reject a second system message;
PR #57522 is documenting this for Codex [issue].

**Codex.** vLLM's own Codex doc recommends `wire_api="responses"` with `--reasoning-parser qwen3
--enable-auto-tool-choice --tool-call-parser qwen3_coder`, and lists no caveats [doc].

### 5.3 The Codex CLI (rust-v0.166.0, 2026-10-29)

- **Request** (`ResponsesApiRequest`, `codex-api/src/common.rs`) [src]:
  - **Fields:** `model`, `stream: true`, `input`, `tools`, `tool_choice: "auto"`, `parallel_tool_calls`,
    `reasoning {effort?, summary?, context?}`, `store: false`, `include: ["reasoning.encrypted_content"]` (always),
    `prompt_cache_key`, `text {verbosity?, format?}`, `client_metadata`.
  - **No `instructions`.** The base instructions are a **leading `developer` message**.
  - **No `max_output_tokens`, no `previous_response_id`** over HTTP.
  - **Tools:** `function`, `namespace` (MCP), `tool_search`, `web_search`, `custom` (freeform with a Lark
    grammar). Unknown models get `apply_patch_tool_type: None`. With `use_responses_lite`, tools ride in an
    **`additional_tools` input item**.
- **Replayed reasoning items** [src]:
  - `summary` is always an array;
  - **`content` is `null` when absent**;
  - `encrypted_content` is kept even for non-OpenAI providers.

  So reasoning produced by llama.cpp or vLLM round-trips cleanly. Reasoning from an OpenAI turn (encrypted, no
  content) gets a 400 on either local server [infer].
- **What it needs back** (`codex-api/src/sse/responses.rs`) [src]:
  - `response.output_item.done` items;
  - `response.completed` with a `response.id` (usage optional);
  - `response.incomplete` is an error unless the reason is `interrupted`;
  - a stream without `completed` gives "stream closed before response.completed" and a retry;
  - events without a `type` are skipped, so **llama.cpp's untyped mid-stream error becomes a silent retry** [infer].
- **Timeouts and retries:**
  - `stream_idle_timeout_ms` defaults to 300 000, wrapped around `stream.next()` on parsed events; SSE comments do
    not reset it [infer];
  - `stream_max_retries` 5, `request_max_retries` 4.
- **`wire_api = "chat"`** fails config parsing; it was removed in rust-v0.95.0 (2026-02-04). Only Responses exists,
  which is why the router has to serve Codex on local models over Responses at all.

### 5.4 What this means for "native"

- **For a plain Codex session on a local model, both upstreams already do what the router does, or better.** That
  means function tools, messages, the leading developer message, and the model's own reasoning replayed. In
  particular:
  - reasoning replay into `reasoning_content` (bug #2);
  - effort → template kwargs (bug #3);
  - merging contiguous assistant items (bug #4): llama.cpp (#19773, `merge_prev`); vLLM 0.28 merges function calls
    into the preceding assistant message too (confirmed in source by the codex review). Step 5 still render-checks it
    with the real template;
  - reasoning streamed as Responses events (bug #1).
- **What neither does well:**
  1. `namespace` / `custom` / `web_search` tools: silently dropped (llama.cpp; vLLM 0.28 custom), a 400 (vLLM 0.32
     grammar custom), or supported (vLLM namespaces).
  2. Foreign item types, `additional_tools` and encrypted or content-less reasoning: 400s.
  3. Mid-conversation system/developer messages under Qwen templates: a template raise.
  4. No `response.incomplete`. llama.cpp reports truncation as `completed`. vLLM sends `response.completed` with
     `status: incomplete`, so its payload says so.
  5. llama.cpp: `text.format` ignored; untyped mid-stream errors.
- **None of these needs the router to rewrite the body for the common case.** They need the router to **know**
  them:
  - per wire, from the qualification run (§11, Step 5);
  - so the facts extractor can refuse a request the upstream would silently degrade (`text.format` to llama.cpp,
    `namespace` tools to llama.cpp b12020) or would 400 (foreign items, encrypted reasoning);
  - so the tap can turn an untyped error into a proper terminal event (§7.3 rule 3).

  The one gap that does need a payload change, mid-conversation system messages, belongs in the server's template
  (§10.3).

## 6. Options considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| **A. Normaliser (today)** | One canonical request type (`InferenceRequest`). Every client protocol is parsed into it, and every adapter rebuilds its wire body from it (LiteLLM's unified API, Portkey, OpenRouter). | One engine; any client can reach any target; heterogeneous failover is trivial. | Every field the canonical type lacks is dropped or refused. Every client feature needs code. The bug class of §1 and §3 is structural: the router changes the prompt the model sees. The research (§4) shows the same failures in mature normalisers. | Reject as the primary path; keep as the fallback |
| **B. Native-first, translate as fallback** | Forward the client's body verbatim when the target speaks its protocol; translate only otherwise, under a fidelity contract | Zero translation where it matters most (Codex → llama.cpp/vLLM Responses; chat clients → chat servers). Routing jobs unaffected. New upstream features reach clients with no router change. Same shape as the existing Codex-native path. | Two paths to maintain. Gates must read native bodies. Failover is limited to targets of the same protocol, or to a qualified translator. Upstream converters (llama.cpp's Responses → chat) have their own gaps. | **Recommended; adopted (D1)** |
| **C. Pure pass-through, never translate** | Helicone- or Cloudflare-style: a route is pinned to one protocol; a mismatch is a 400 | No mutation at all; simplest | The Assistant and internal callers speak the canonical type; there is no Codex on an upstream without a usable Responses API (vLLM's non-harmony path may be one, §5.2); no heterogeneous fallback | Reject; but B must be able to run in C's mode per route (`translate: 'never'`) |
| **D. Put a translating proxy (LiteLLM) in front of the local servers** | The router stays a pure router; LiteLLM does Responses → chat | Moves code out of the router | The translation bugs move with it (§4.1: LiteLLM's bridge split choices and dropped `additional_tools` in earlier releases, and #27276 on tool names and tool types is open); an extra hop, extra config and an extra failure domain; the router can no longer see or gate the content | Reject |
| **E. Let the upstream translate** (a variant of B) | Forward Responses to llama.cpp's own `/v1/responses`, which converts to chat internally, next to its template, reasoning parser and tool parser | The converter is maintained by the people who own the template machinery, and maps reasoning input items to the template's `reasoning_content` (§5.1) | Its fidelity is version-dependent (b12020 vs latest), and its gaps are now the router's problem to detect | **Adopt within B** for llama.cpp, gated by the same golden suite as any translator |

**Why B over A.** A cannot be made side-effect-free: a normaliser changes the model's prompt by construction. The best
it can do is make the changes visible, which is what `x-router-adapted` and `parameters_adjusted` do today, and bugs
#2-#4 shipped anyway. B removes the changes rather than disclosing them, wherever a native path exists, and that is
where the traffic is: Codex agents on local models (Responses) and chat clients on local models (chat). A remains for
what has no native path. Today that is the Assistant and internal callers (canonical → chat), and Codex on any
upstream whose Responses API fails the golden suite.

**Why B over C.** C would break the Assistant (canonical request type), and Codex on vLLM if vLLM's Responses path
does not qualify for Qwen tool calling. B with `translate: 'never'` on a route gives C's guarantee where the owner
wants it. `translate: 'qualified'` (D4, for the dev-worker agent routes) allows only qualified translators.

---

## 7. The recommended architecture

### 7.1 Wire declarations (configuration)

Each account declares, **per model** (cloud3's llama-swap serves llama.cpp and vLLM behind one URL), what its upstream
speaks natively:

```ts
// Account (new, optional). Absent = today's behaviour (translate to chat).
wire?: Array<{
  models: string[];                  // which of the account's models this applies to ('*' allowed)
  protocol: 'responses' | 'chat';    // a protocol the upstream speaks natively
  path?: string;                     // default '/responses' or '/chat/completions' under baseUrl
  mutations?: DeclaredMutation[];    // OPT-IN mutations, beyond the wire kind's defaults (§8.1 rule 2, §8.2)
  holdMs?: number;                   // the hold rule's window (§7.3 rule 2); absent = hold until the first other event
  allowReserved?: string[];          // operator-reserved fields this wire lets through (§7.2)
  allowLoss?: string[];              // known losses this wire may serve anyway, each named (e.g. 'tool:namespace')
  qualification: QualificationRef;   // REQUIRED: the manifest entry below; a wire without a matching one is inert
}>;
// Qualification manifest (versioned with the golden suite; one entry per path a target can take)
interface Qualification {
  path: 'native' | 'translated'; protocol: 'responses' | 'chat';
  backend: string;                   // e.g. 'llama.cpp b12020' / 'vllm 0.28.0' (read from /props, /version)
  model: string; templateDigest: string;     // sha256 of the template the server reports
  translator?: string;               // translated path: contract version + account settings (replayReasoning, ...)
  supports: string[];                // features that passed: item and tool kinds, text.format, reasoning shapes, non-streaming
  losses: string[];                  // features known to be dropped or degraded (e.g. 'tool:namespace', 'text.format')
  run: string;                       // the qualifying run (journal id, date)
}
// RoutePolicy (new, optional)
translate?: 'any' | 'qualified' | 'never';
  // any (today's behaviour, the default during migration): any translator;
  // qualified (D4: the dev-worker agent routes): only a translator with a matching manifest entry; a target needing an
  //   unqualified one is skipped, and with none left the request is refused loudly (400, naming the pair);
  // never: a target whose wire lacks the client's protocol is skipped (C's guarantee)
nativeWires?: boolean;               // route-scoped activation (canary isolation): an account's `wire` entries take
                                     // effect only on routes that set this; absent = translated, as today
```

**Qualification is enforced, not descriptive.** A target is eligible for a request only when all three hold:
- a manifest entry matches the target's live backend, model and template digest (and, translated, the translator
  configuration);
- every feature the request's facts name (§9.6) is in `supports`;
- no feature it names is in `losses`, unless the wire's `allowLoss` names that exact loss.

Otherwise the target is skipped with a named reason: `unqualified`, `feature_unsupported:<kind>`,
`protocol_unavailable` or `translation_unsupported:<field>`. Each new reason joins `PERMANENT` and `REQUESTED` in
`compat/index.ts:95` (`origin/main` `247b19f`), so that when no target remains, the engine returns the loud 400 and not
a capacity error. A backend or template change (a new digest) unqualifies the wire until Step 5 re-runs.

The existing Codex mechanism (`RoutePolicy.native: ['codex-responses']`, `ProviderSupport.native`) becomes one case of
this: the codex provider declares `wire: [{models:['*'], protocol:'responses'}]` implicitly, with its own manifest
entry. The route-level `native` setting stays as a compatibility alias during migration.

### 7.2 Request flow

```
client body ──► 1. envelope parse   (model, stream, principal, conversation id: read-only)
            ──► 2. facts            (protocol-aware, READ-ONLY inspection: tools?/kinds, images?, reasoning items?
                                     encrypted?, output limit, structured output, agent id) ── never builds a body
            ──► 3. route            (alias → chain → candidates; gates use FACTS, not a translated message list)
            ──► 4. per attempt: wire plan for THIS target
                    native?    → upstream body = applyDeclared(clientBody, account.wire.mutations) ; model := deployment
                    translate? → translator(clientBody) under its fidelity contract; any unmappable field ⇒ this target
                                 is ineligible (reason `translation_unsupported:<field>`), never a silent drop
                    neither    → target skipped (`protocol_unavailable`)
            ──► 5. journal      `request.wire` {path: native|translated, protocol, mutations:[names], digests}
            ──► 6. send
```

Three properties matter:
- **The body is bound late, per attempt.** Today `api-openai` decides native vs subset before routing
  (`index.ts:398-418`) and the subset parse fails the whole request. In B the decision is per target, so a chain of
  "llama.cpp (native responses) → vLLM (translated)" is possible, and a parse failure for translation only removes the
  targets that would need it.
- **Facts are not a body.** The vision gate today reads `request.messages`, which is empty for a native request
  (`routing/index.ts:305`), so a native body with `input_image` would pass a non-vision target unseen. The facts
  extractor must read the native shapes (`input_image` in message content and in `function_call_output` output
  parts, chat `image_url` parts) and is tested against the same golden corpus.
- **Validation is validate-or-reject, never rewrite.** The native path checks only the fields that affect router
  invariants:
  - `stream` (boolean);
  - `n` (1; budgets);
  - `store` and `previous_response_id` / `conversation` / `background` (no server-side state, as
    `parseNativeResponses` does today);
  - body size;
  - **operator-reserved fields** (below).

  Anything else goes through untouched, and the upstream validates it (D2).

  **Operator-reserved fields.** Forwarding unknown fields (D2) would also let a caller set fields that steer the
  server's own state, its scheduling or the response shape the tap reads. Each wire kind therefore has a default
  denylist. A request carrying any of these fields is refused by name (400). They are not stripped; an operator may
  allow one per wire.
  - **llama.cpp** (server README, `/completion` options, which the chat and Responses paths share):
    - slot and cache state: `id_slot`, `cache_prompt`, `n_cache_reuse`, `n_keep`;
    - adapters: `lora`;
    - extra or unbounded work: `n_cmpl`, `ignore_eos`, `t_max_predict_ms`;
    - response shape and diagnostics: `n_probs`, `post_sampling_probs`, `return_tokens`, `response_fields`,
      `timings_per_token`, `sse_ping_interval`;
    - out-of-band control: `reasoning_control`, which arms `/v1/chat/completions/control`.

    The same keys are denied inside `extra_params` (b12110) as well. On b12020 any top-level key reaches llama
    params (§5.1).
  - **vLLM** (its documented extra request parameters; verify the list against 0.28 in Step 5):
    - scheduling and caching: `priority`, `request_id`, `cache_salt`, `kv_transfer_params`, `vllm_xargs`;
    - **template replacement and render controls**, which D5 makes server policy: `chat_template`,
      `add_generation_prompt`, `continue_final_message`, `add_special_tokens`, `echo`;
    - response shape: `return_token_ids`, `return_tokens_as_token_ids`, `prompt_logprobs`;
    - custom processors: `logits_processors`.
  - **Not reserved by default:** sampling controls (`top_k`, `min_p`, `repeat_penalty`, …), `grammar` /
    `json_schema`, `chat_template_kwargs` and `reasoning_effort`. These are the client features pass-through exists
    for. An operator who wants a fixed thinking policy reserves `chat_template_kwargs` on that wire.

  The router itself sets `return_progress` (§9.4), so it is not reserved: a client value is the same request.
- **The upstream body is the client's bytes, patched.** Today the Codex adapter parses the body and re-serialises it
  with `JSON.stringify` (`codex.ts:471`), and that does not preserve the bytes. The native adapter instead:
  - keeps the bounded original request buffer;
  - applies each declared request mutation as a byte-level patch of that one field (`model`, `return_progress`),
    as Envoy does with `sjson` (§4.5);
  - leaves every other byte as received.

  The parsed object serves facts and routing only. `RouterHttp.preserveRawBody` today refuses registrations above
  2 MiB (`kernel/http.ts:99-101`), so it must take native-sized bodies, bounded by §9.2's reservations.
- **Body size and admission are specified, not inherited** (§9.2).

### 7.3 Response flow: verbatim stream, observing tap

**Streaming.** The upstream's bytes go to the client as they come.
- **One read loop, governed by downstream backpressure.** The loop reads an upstream chunk, passes it through the
  tap, writes it, and awaits drain before it reads again. There is no parallel tap and no unbounded buffer.
- **Frames are forwarded as received.** The tap delimits SSE frames over the raw bytes, with bounded per-frame
  buffers, and parses a copy. Unlike today's `sendRaw` (`api-openai:485`), which rebuilds each frame from the parsed
  event and loses the original framing and fields such as SSE `id:`, the frame goes out as received.
- **The tap extracts what the router needs:**
  - usage: `response.completed.usage`, or the chat usage chunk;
  - the finish reason, **read from the terminal payload's `status` / `incomplete_details`, not only its event type**
    (vLLM sends `response.completed` with `status: incomplete`, §5.2). A finish that cannot be trusted
    (llama.cpp's hardcoded `completed`) is journaled as uncertain;
  - first output: the first output item or delta, which is the failover boundary;
  - error codes;
  - reasoning/text/tool character counts for the journal.

It never writes. The only permitted differences between upstream bytes and client bytes are:
1. SSE comments (`: ok`, `: keepalive`), which every SSE parser ignores;
2. holding the upstream's lifecycle events at the start of the stream, so a failure before anything reached the
   client can fail over (a timing change). This is the **hold rule**, defined once here and used by §9.1 and §9.4:
   - **What is held:** the stream's leading `response.created`, `response.queued` and `response.in_progress`
     events, in order, while nothing has been flushed.
   - **When the hold ends:**
     - on the first other event, which is today's Codex rule and the only rule on the `codex` wire;
     - or, on a wire that declares `holdMs`, when the hold has lasted `holdMs`.

     The llama.cpp responses wire declares `holdMs: 30000`. That is below Codex's 300 s idle timer with ample room,
     and it is how `return_progress`'s prefill events reach Codex during a long prefill.
   - **When the hold ends,** the held events are flushed in order, and every later event, `in_progress` included,
     is forwarded as it comes.
   - **The flush is the failover boundary:** once anything reached the client, the attempt is committed and is no
     longer failed over (§9.1).

   A failure before output on llama.cpp is almost always an HTTP error before any event (a refused connection, a
   loading 503, a template 400/500), so the 30 s window still covers the failovers that matter.
3. a router-originated terminal event when the router itself ends the stream (timeout, budget, client gone), or
   after an upstream error that is not a protocol event (llama.cpp's untyped `data: {"error":…}`, §5.1), and only
   when the upstream sent no terminal event;
4. the declared response mutations of §8.2 that apply to the wire, e.g. `overload-code` (today's
   `server_is_overloaded` rewrite), `output-limit-cut` and `drain-cut`.

A test asserts this per protocol: strip comments and the declared mutations from the client stream, and the result
must equal the upstream stream byte for byte. The same suite covers:
- fragmented UTF-8 across chunks;
- multiline `data:` frames and frames with `id:` / `retry:`;
- a slow reader (backpressure holds the upstream read);
- a client disconnect;
- shutdown and drain cleanup.

**Non-streaming** (`stream: false`, both protocols). Today the router always synthesises a new response object, with
the requested alias as `model` (`api-openai:445`), which conflicts with D3. On a native wire instead:
- the upstream's status code, `content-type` and JSON body go to the client as received;
- the body is bounded by today's 8 MiB non-streaming cap, beyond which the router returns 502;
- the tap reads `usage` and the finish (`choices[].finish_reason`, or Responses `status` / `incomplete_details`) from
  a parsed copy.

The chat and Responses non-streaming cases are in the golden suite, and must pass before a wire serves
`stream: false`. Until then, a wire refuses `stream: false` (`feature_unsupported:non_streaming`).

### 7.4 Translation, only as a fallback, with a fidelity contract

The translator (today's `parseRequest` + `toChatMessages` + the Responses synthesis in `api-openai`) remains for:
- canonical/internal callers (the Assistant, the admin bridge, `test_route`);
- any (client protocol, target) pair without a native path.

Its contract is a document plus tests, per pair (Responses → chat, chat → Responses, canonical → chat). It names:
1. **Mapped losslessly:** every field and item type that round-trips. Examples: function tools; `function_call` items
   attached to the preceding assistant text (#130); reasoning items to and from `reasoning_content` (the stopgap);
   `reasoning.effort` to and from `reasoning_effort`; `instructions` to a leading system message.
2. **Mapped lossily, declared:** each with the mutation name it journals. Examples: `input_image.detail` lost;
   `json_schema.description` lost; tool-output images moved after the tool results.
3. **Refused:** everything else, by name, with a 400 that says which field and suggests the native route. This is
   today's behaviour for unknown keys, and must also become the behaviour for the things the Codex adapter drops
   today, unless declared per account (§8.3).

Golden tests (§11, Step 1) pin each line of the contract.

---

## 8. The mutation policy

### 8.1 Rules

1. **Nothing changes that routing does not require.** On a native path, by default, the upstream body is the client's
   body with `model` replaced. Auth is a header.
2. **Every other change is a declared mutation.** It is named, from a fixed registry, and is one of two kinds:
   - a **wire default**, part of a wire kind's definition, applied without configuration. Examples: `store-false`
     and `overload-code` on the codex wire; `stream-usage` on chat wires. The registry says which wires;
   - **opt-in**, configured per account (or per route).

   Either way, every mutation an attempt applies is:
   - **listed by name** in that attempt's `request.wire.mutations` (names only, never values; wire defaults are not
     implied);
   - disclosed to the client in one header (`x-router-mutations`; it replaces `x-router-adapted` and
     `x-router-ignored-params`, which stay during migration).

   A wire default that did not act on a request (for example `cache-key-salt` before any overload) is not listed.
3. **No silent drops.** An unknown field is forwarded (native, D2) or refused by name (translated). On native wires,
   operator-reserved fields (§7.2) are refused by name, never stripped. A known field the target
   cannot take is refused by name, unless the account declares the drop. "Neutral" drops are allowed only for a value
   that equals **the target's** documented default; a target that supports the control always gets the client's value.
4. **No silent rewrites of meaning.** Mapping a value (finish reasons, roles, effort levels) is a translation and lives
   in the contract. **The raw value is always kept.** Following OpenRouter's `native_finish_reason`, the journal
   records `upstream_finish_reason`, and a translated response may carry it as an extra field.
   - **An unknown `finish_reason`:** mapped to `stop`, with the raw value kept. It is an error only when the value
     says the output is incomplete or failed (`error`, `abort`, `cancelled`, or anything with `error` in it), and
     then the stream ends with the router's failure event in place of the finish.
   - **Native paths:** the client gets the value verbatim; the journal keeps it too.
5. **Policy about what the model sees belongs at the server, not the router.** Examples: where system text goes (D5)
   and sampling defaults. These are template and server settings (`--chat-template-file`, `--chat-template-kwargs`,
   llama-swap presets), owned in cloudlab IaC. The router forwards.
   - **One measured exception: how much past reasoning is replayed.** In an agent session every assistant turn comes
     after the last user message, so the template's `preserve_thinking=false` strips nothing. Replaying every block
     overflows cloud3's pool (§12.1). The bound therefore lives in the router, as the declared, per-account,
     journaled `reasoning-window` mutation.
6. **The invariant is testable.** `upstreamBody == applyDeclared(clientBody, mutations)`, where `mutations` is the
   attempt's journaled list (rule 2), is checked in the golden suite. In production the journal can store both digests
   (`sha256` of the client and upstream bodies). They verify that the invariant held for a request, but they cannot
   reconstruct it (§9.5).

### 8.2 The mutation registry (initial)

| Name | Direction | What | Kind (rule 2) | Why it exists |
|---|---|---|---|---|
| `model` | req | replace `model` with the deployment model | wire default: every wire | routing |
| `store-false` | req | force `store: false` (Codex backend) | wire default: codex | no server-side state |
| `cache-key-salt` | req | salt `prompt_cache_key` after overloads | wire default: codex (listed only when it salts) | Codex re-key |
| `stream-usage` | req+resp | add `stream_options.include_usage: true`; remove the usage-only chunk from the client stream if the client did not ask | wire default: native chat | budgets (§9.3) |
| `set:return_progress` | req | add `return_progress: true` (llama.cpp prompt-progress events) | wire default: llama.cpp responses | Codex idle timer during a long prefill, from the upstream's own events (§9.4) |
| `reasoning-window` | req | replay only the last N turns' reasoning: the stopgap's `replayReasoning: {lastTurns: N}` on translated wires; on a native Responses wire, the reasoning items of older turns are left out of `input` | opt-in, per account | context bound (§8.1 rule 5 exception, §12) |
| `drop-replayed-reasoning` | req | today's behaviour (`replayReasoning` unset or false) | opt-in, per account | a template that breaks on replayed reasoning |
| `developer-as-system` | req | `developer` → `system` | opt-in, per account (translated) | template compatibility |
| `drop:<param>` | req | today's `params.drop` | opt-in, per account | a server that chokes on a control |
| `effort:<level>` | req | fixed reasoning effort (the stopgap's level form) | opt-in, per account | operator policy. Not a fix for plan delivery: `medium` did not help (§12.1). |
| `system-merge` | req | today's `systemMessages: merge` | opt-in; **retired by D5** once the template fix is live | template workaround |
| `overload-code` | resp | `server_is_overloaded` → `server_error` after output | wire default: codex | Codex retry behaviour |
| `progress-events` | resp | `response.in_progress` while the upstream thinks (#131) | wire default: translated Responses | Codex idle timer |
| `output-limit-cut` | resp | the router's `OutputLimiter` ends the stream at the client's output limit, with finish `length` and estimated usage | wire default: codex (the provider, `routerOutputLimit`), for any client; never on local wires | the codex API has no output limit |
| `drain-cut` | resp | during drain, a stream with no terminal event yet is cut without one (Codex reconnects to the next process) | every wire | drain (§9.2) |

### 8.3 What happens to each of today's mutations

| Today | In B |
|---|---|
| Codex adapter drops (hints, `include`, non-function tools, reasoning items) | **Native:** none happen; the body goes as sent. **Translated:** hints (`prompt_cache_key`, `client_metadata`, `text.verbosity`, `reasoning.summary`, encrypted-only `include`) become contract line items, "mapped: ignored by chat servers", because they carry no instruction to the model. Reasoning items: mapped (the stopgap). `namespace` tools: flattened, then re-wrapped, or refused. `web_search` / `custom` tools: refused by name unless the account declares `drop:tool:<type>`. |
| Subset allowlist (unknown keys → 400) | Native: forwarded. Translated: stays a 400 (loud is right). |
| Chat `reasoning_content` dropped on replay | Native chat: forwarded (llama.cpp and vLLM take it). Translated: mapped (`replayReasoning`, merged in #135/#136), bounded by `reasoning-window` where the account sets one. |
| Neutral-value drops | Removed for targets that support the control. Kept only where the target lacks the control and the value is its default. |
| `reasoning_effort` dropped for local | Native: forwarded. Translated: forwarded (the provider table says `true` for local). An account may declare `effort:<level>` or `drop:reasoning_effort`. |
| `systemMessages: merge` | **D5:** retired. The router stops merging once cloudlab's patched template is live on each account that sets it (§10.3); until then, `system-merge` on the translated path only. |
| `developer` → `system` | Native: forwarded. Translated: `developer-as-system`, per account |
| `content: null` → `''` | Translated: send `null` (the OpenAI shape); change per account only if a template needs it |
| Unknown `finish_reason` → `stop` | Native: verbatim. Translated: mapped, with the raw value in the journal (`upstream_finish_reason`); an error only for a value that means incomplete or failed (§8.1 rule 4) |
| Upstream error text hidden | Native: the upstream's error JSON goes to the caller as sent (it is the caller's own conversation). The journal still keeps only the classification and code. |
| Response ids, `model` alias, usage shape | Native: verbatim, **including the served `model` name (D3)**. There is no alias masking. |
| Reasoning after visible output not forwarded | Native: verbatim. Translated: contract line (multiple reasoning items in order) |
| Progress events, keepalive comments | §9.4 |
| Codex-native mutations (`model`, `store`, headers, salt, overload code) | Unchanged, now registry entries |

---

## 9. The router's legitimate features in each mode

### 9.1 Failover

- **Before anything reached the client: both modes, as today.** The boundary is the **first flush to the client**
  under the hold rule (§7.3, rule 2):
  - on the codex wire, the first output item or delta (lifecycle events are held until then);
  - on a wire with `holdMs`, that, or the end of the hold window, whichever comes first.

  Once a prefill progress event has been forwarded, the attempt is committed exactly as if output had begun.
  Reasoning counts as output once forwarded, as #132 decided.
- **After first output: never,** in both modes. That is today's rule (`inference/index.ts:851`, "emitted"), and the
  research (§4.9) shows it is the industry norm. A mid-stream failover would splice two models' outputs into one
  response.
- **Across heterogeneous targets.** A failover target must be able to serve the *same client body*: it speaks the
  client's protocol natively, or a qualified translator exists for the pair. Otherwise it is skipped
  (`protocol_unavailable`). Encrypted reasoning (`encrypted_content` from an OpenAI account) cannot go to a local
  model. The translator refuses it (today's `replayedReasoning` already returns false for non-null
  `encrypted_content`); native local targets must refuse it too, and the facts extractor flags it
  (`reasoning.encrypted`), so routing skips those targets instead of letting the upstream fail. Conversation
  placement keeps a conversation on one account most of the time. It still spills busy turns and moves unavailable
  conversations (`placement/index.ts:6`), so cross-path continuation is a tested case (§10.4), not a rare one.

### 9.2 Admission queue, concurrency, drain

The queue (#133) and concurrency act before the body is sent, and on the connection, not the payload. They are
unchanged.

Drain cuts a stream with no terminal event (deliberate, so Codex reconnects). It is the registry's `drain-cut`,
journaled on the attempts it cuts. On a native stream that already carries the upstream's terminal event, it ends
normally, as today (`api-openai:557-562`).

**Body admission is not unchanged.** Today a large (> 2 MiB, up to `codexNativeMaxBodyMiB`) `/v1/responses` body is
eligible only when a route the principal may use is Codex-native, decided from route-level configuration **before the
body is read** (`api-openai:650`, `nativeCaller`). Local `wire` entries would not inherit that, and chat has no such
reservation at all. B specifies:
- **Pre-read eligibility.** A principal is large-body eligible on an endpoint when any route it may use has a target
  that can take that protocol natively. That means a codex route, or an active local `wire` (`nativeWires` routes
  only, §7.1). It is computed from configuration, before reading, as today.
- **Per-principal reservations.** Today's pools hold: a small-body floor, plus one principal may take at most half
  of the large pool. Native chat gets the same reservations on `/v1/chat/completions` before any chat wire is enabled.
- **Translated candidates and size.** A body over the 2 MiB subset limit can go only to native targets. A translated
  candidate is skipped (`body_too_large_for_translation`, permanent and request-caused). With none left, the
  answer is 413, not a capacity error.
- **Reservation lifetime.** The reservation is held from admission until the handler settles: through the queue
  wait, every failover attempt and drain, as `RouterHttp.hold` does today. Its size accounts for what is actually
  retained: the raw bytes (kept for patching, §7.2), the parsed object (facts), and at most one patched upstream
  copy per attempt. A translated attempt's serialised body counts too.
- **Tests:** chunked bodies (no `content-length`, provisioned at the cap and shrunk on end); queue saturation with
  large bodies waiting; failover holding one reservation; drain releasing it.

### 9.3 Budgets, usage, limits

- **Native Responses.** `response.completed.usage` (llama.cpp and vLLM both emit it; §5). Read by the tap.
- **Native chat.** OpenAI-compatible servers put usage in a stream only when asked
  (`stream_options.include_usage: true`); the router sets it today (`chatcompletions.ts:155`). Pin llama.cpp's and
  vLLM's behaviour without the flag in Step 5. The router needs usage, so `stream-usage` is a declared mutation on
  chat wires:
  - add the flag when the client did not set it;
  - remove the usage-only chunk (`choices: []`) from the client stream when the client did not ask for it, because
    some clients index `choices[0]` unconditionally.

  It is the one response mutation a native chat path needs. Alternative: estimate usage from the tokenizer and mark
  it `estimated` (no mutation, worse numbers); the owner can choose per account.
- **Output limit.** Native: the client's own `max_output_tokens` / `max_tokens` is forwarded and the upstream enforces
  it. Router enforcement (`OutputLimiter`, the registry's `output-limit-cut`) stays as today. It applies to targets
  of the **`codex` provider** only, because its API has no output limit (`routerOutputLimit` in the provider table),
  whichever client sent the request. It never applies on local wires.

### 9.4 Progress events: still needed when the upstream streams reasoning natively?

- **During thinking: no.** On a native Responses path, llama.cpp and vLLM stream `response.reasoning_text.delta`
  events (§5.1, §5.2), and Codex's idle timer counts parsed events. #131's progress events exist only because the
  translated path had nothing to send.
- **During prefill: the upstream can do it.** llama.cpp emits prompt-processing `response.in_progress` events when
  the request carries `return_progress: true` (§5.1; in b12020, #25348). That is a real upstream event, not a router
  invention. Two places can set it, both declared:
  - (a) cloud3's llama-swap entry, `filters.setParams` with `return_progress?: true` (server-side, cloudlab-owned).
    But llama-swap filters are per model, not per endpoint, so chat requests would get `prompt_progress` fields too.
  - (b) A router wire mutation `set:return_progress` on the `responses` wire only, journaled. **Recommend (b),**
    validated with the real Codex CLI on a ≥ 100k-token prompt (the K=46 probe's prefill was 94 s).

  These progress events reach Codex only through the hold rule's `holdMs` (§7.3, rule 2). The hold covers the
  first 30 s. After that, the held events and every later `in_progress` event are forwarded, and the attempt is
  committed (§9.1).
- **During a cold model load or a queue wait: nobody sends anything.**
  - SSE comments do not reset Codex's timer [infer, §5.3]. That covers the router's 5 s `: keepalive` and
    llama.cpp's 30 s `:` pings alike.
  - The router cannot synthesise `response.in_progress` without the upstream's envelope: before the upstream's
    `response.created` it has none, and after it the router would be duplicating an upstream event.
  - **The fixes are outside the router.** Codex's `stream_idle_timeout_ms` in the local-model profiles (the runbook
    already names it as the fix for cold loads). cloud3's resident default model (cloudlab #53, live). The admission
    queue's `maxWaitMs` (80 s on cloud2) is already below 300 s.
- **On translated streams: unchanged.** The router owns the envelope, and #131 stays.

### 9.5 Journal and observability

- **New event, per attempt:** `request.wire` with:
  - `path` (`native` or `translated`) and `protocol`;
  - `mutations` (names);
  - `translation` (the contract version, and its lossy line items, by name);
  - optional body digests.
- **Kept:** `request.parameters_adjusted`, for one release, then folded in.
- **The tap feeds** `attempt.completed` as today: finish, `textChars`, `toolCalls`, `tools`, `reasoningChars`.
- **Content capture: today's contract stays, bounded and redacted.** The recorder redacts the captured request, and
  bounds it: strings are sliced at 16,384 characters, collections at 200 entries, and an oversized capture becomes a
  `{truncated, bytes}` marker (`trajectory/index.ts:32`, `observability/index.ts:3`).
  - Native reasoning histories hit those limits at once, so a capture is **not** a replayable copy of the request.
  - Each captured request gets an explicit completeness label (`replayable: true` only when nothing was redacted or
    truncated). Digests (§8.1 rule 6) verify, but they cannot reconstruct.
  - **Exact-body capture is a separate diagnostic mode:** encrypted at rest, short-lived, opt-in per account and
    time-boxed like `capture.expiresAt`. It exists so that a request can be replayed straight to the upstream, which
    the #130 and flash-next investigations had to reconstruct by hand.

### 9.6 Capability gates

They run on **facts** (§7.2), extracted read-only from either protocol:

| Gate | Fact | Native-body source |
|---|---|---|
| vision (`visionModels`) | `hasImages` | Responses `input_image` parts in `message.content` and `function_call_output.output`; chat `image_url` parts |
| tools | `hasTools`, `toolKinds` | `tools[]` types, `additional_tools` items, `function_call(_output)` history |
| Assistant (`assistantModels`) | `agentId === 'router-agent'` | header / body, as today |
| image models | endpoint | unchanged (images are a separate endpoint) |
| structured output | `responseFormat` | `text.format` / `response_format` |
| encrypted reasoning | `reasoning.encrypted` | `reasoning` items with `encrypted_content` |

Some targets cannot handle a kind the request uses. llama.cpp silently drops `custom` and `namespace` tools (§5.1),
and vLLM 0.32 refuses grammar tools (§5.2). The facts are checked against the target's qualification manifest
(§7.1):
- **A known loss skips the target** before the network, with the reason named (`feature_unsupported:tool:namespace`).
- **The exception is a wire whose `allowLoss` names that exact loss.** Then the request is forwarded and the loss
  is journaled.
- **D2 forwards unknown *fields*. It does not extend to known *losses*:** journaling a dropped `namespace` group
  does not make an agent route work.
- **The skip reasons are permanent and request-caused** (§7.1), so with no target left the client gets a 400 naming
  the feature, not a capacity error.

For the canary this is a real choice (§11, Step 6):
- Codex sends its multi-agent tools as a `namespace` group, and both today's translator and llama.cpp b12020 drop
  them.
- So either the dev-worker wire declares `allowLoss: ['tool:namespace', 'tool:web_search']`, which makes today's
  behaviour explicit,
- or the canary waits for llama.cpp's namespace flattening (#31568).

---

## 10. Risks and edge cases

### 10.1 Upstream Responses-API gaps

| Gap | llama.cpp b12020 | vLLM 0.28 | Router response in B |
|---|---|---|---|
| Truncation | reported as `completed` (#30773) | `response.completed` event, but `response.status: incomplete` | Tap reads the terminal **payload's** status and `incomplete_details`, not only the event type. vLLM: `length` when the payload says incomplete. llama.cpp: the status cannot be trusted, so the journal records `finish: uncertain` when the output may have hit a limit. Token-count equality is not proof. No rewrite. |
| Mid-stream error | untyped `data: {"error":…}`; Codex skips it, then retries on "stream closed" | — | Tap recognises it: forward the bytes, then end with the router's own `response.failed` (rule 3, §7.3), so Codex sees an error instead of a mystery retry |
| `namespace` / `web_search` / `custom` tools | silently dropped (server log only) | namespace OK; `custom` dropped (0.28) or 400 (0.32, grammar) | Facts: `toolKinds`, checked against the qualification manifest (§7.1). A known loss **skips the target**, unless the wire explicitly allows that specific loss (`allowLoss: ['tool:namespace']`). An allowed loss is journaled. D2 does not cover known losses. |
| `text.format` (structured output) | silently ignored | mapped | Facts: `responseFormat`; a llama.cpp native wire refuses it (or the request is translated for that target, where `response_format` works) |
| Foreign items (`additional_tools`, `custom_tool_call`, `web_search_call`, `item_reference`) | 400 | 400 in 0.32; passed through raw in 0.28 (may break the renderer) | Facts: refuse before the network with the item named (saves a prefill), or skip the target |
| Reasoning items: encrypted, content-less, `summary: null` | 400 | 400 if encrypted | Facts: `reasoning.encrypted` / `reasoning.contentless`; skip local targets; placement keeps GPT conversations on GPT accounts |
| Mid-conversation system/developer | template raise (Qwen) | template raise (Qwen) | Template fix in cloudlab (D5, §10.3); the router stops merging once it is live |
| `previous_response_id` / `store: true` | 400 | 404 / store off | The router already refuses (`parseNativeResponses`) |
| Stream item ids differ from the final response | no | **yes** (re-parse) | Qualify with the real Codex CLI (it builds items from `output_item.done`) |
| Text + calls of one reply rendered as one assistant turn (bug #4) | yes (`merge_prev`) | yes (confirmed in source, codex review) | Step 5 render check with the four 11-01 stop turns, against the real template |
| Non-standard events | — | `response.reasoning_part.*` (0.28) | Verbatim; Codex ignores unknown types [infer]; qualify |

**Version drift is the standing risk.** Each upstream upgrade re-runs the qualification (Step 5) before the `wire`
entry's `qualified` field moves. llama.cpp's open PRs (#31568 namespaces, #29412 content-less reasoning) and vLLM 0.32
change several rows.

### 10.2 What Codex sends, and what each path does with it

Sources: §5.1-5.3. Codex 0.166 for a model outside its catalog, which is what the dev workers' local profiles are.

| Codex sends | Native → llama.cpp b12020 | Native → vLLM 0.28 | Translated (today + stopgap) |
|---|---|---|---|
| Leading `developer` message (base instructions) | → system; fine | → template; fine as the first message | → system |
| Mid-conversation `developer` | **template raise** (until the D5 template fix) | **template raise** (until the D5 template fix) | `system-merge` (lossy; retired by D5) |
| `function` tools, `function_call(_output)` | mapped | mapped | mapped |
| `namespace` (MCP / multi-agent) | **silently dropped** | flattened and restored | dropped (journaled) |
| `web_search` | silently dropped | dropped (no tool server) | dropped (journaled) |
| `custom` freeform (apply_patch; not sent for unknown models unless configured) | silently dropped | silently dropped | dropped (journaled) |
| `additional_tools` item (only with `use_responses_lite`) | **400** | (raw dict) | merged into `tools` |
| `reasoning` items with content (from a local turn) | → `reasoning_content` | → `reasoning_content` | dropped today; mapped by the stopgap |
| `reasoning` items, encrypted or content-less (from a GPT turn) | 400 | 400 (encrypted) | refused |
| `reasoning.effort` | → template kwarg | → template kwarg | dropped today; passed by the stopgap |
| `include`, `prompt_cache_key`, `client_metadata`, `text.verbosity`, `reasoning.summary` | ignored | ignored / validated | dropped (journaled) |
| `text.format` (only with `--output-schema`) | **silently ignored** | mapped | mapped |
| Needs back: `output_item.done`, `response.completed` with `id` | yes | yes (ids re-parsed) | synthesised |

For the dev workers' plain local sessions, three rows decide the canary: mid-conversation `developer`, `namespace`
tools, and replayed reasoning.
- **The first needs the template fix.**
- **The second is today's behaviour as well** (the router already drops them), so it is not a regression. But under
  §9.6 it skips the target unless the wire explicitly allows that loss (`allowLoss`).
- **The third is the point of the change.**

The facts extractor knows these kinds. A kind the target's manifest lists as a loss skips the target, unless the wire
explicitly allows that loss (§9.6). A request is never sent just to fail, and a loss is never left implicit.

### 10.3 Mid-conversation system messages

- **The cause.** Codex and Claude Code inject `developer`/`system` messages mid-conversation. Qwen's GGUF template
  raises "System message must be at the beginning" (`chat_template.jinja:109-110`). It merges only the *leading*
  run (`:45-56`).
- **The router's options are all lossy.** Merge everything into the head (today, `system-merge`), or turn the message
  into a user message. Each changes what the model sees, in the router, where nobody looks.
- **Decided (D5): fix it where the template lives.** A cloudlab-owned `--chat-template-file` (llama.cpp) /
  `--chat-template` (vLLM) that renders a mid-conversation system/developer message as its own `<|im_start|>system`
  block is a one-line template change, versioned with the server, and testable against the golden corpus.
  - **Order.** The template ships and passes the render checks first. Then the router stops merging: `system-merge`
    is removed from each account in the same change that confirms its template.
  - **Until then,** `system-merge` stays on the translated path only, journaled. A native wire is not enabled on a
    model whose template still raises (a Step 6 prerequisite).
- **On the native path,** llama.cpp's Responses converter decides what `developer` items become. That is one more
  reason the golden suite runs against the real upstream.

### 10.4 Failover across heterogeneous accounts

A route chain mixing native and translated targets serves the same request two different ways. The journal says
which (`request.wire.path`); the golden suite qualifies both. Rules:
- **Same conversation: placement usually keeps it on one account, but not always.** A busy bound account spills one
  turn elsewhere, and an unavailable one moves the conversation (`placement/index.ts:6`, `SPILL_REASONS`). So a
  session *can* cross between native and translated targets mid-conversation, which is the resumed-session case
  below.
- A fallback that cannot take the body is skipped, not "adapted harder".
- **D4:** the dev-worker agent routes use `translate: 'qualified'`. A chain may mix native and translated targets
  only through translators that passed the golden suite for that pair. Otherwise the target is skipped, and with
  none left the request is refused loudly. `translate: 'never'` forbids the mix outright where that is wanted.
- **A session resumed across the flip.** One Codex session goes through both paths, in either direction, when:
  - a native wire is turned on or off mid-conversation;
  - placement spills or moves it (above).

  Each direction must accept the other path's items.
  - **Translated → native.** A session started translated replays the router's synthesised reasoning items:
    `rs_<id>`, `summary: []`, `content: [{type: reasoning_text, text}]`, no `encrypted_content`
    (`api-openai/index.ts:255`). llama.cpp's converter needs exactly an array `summary` and a string
    `content[0].text` (§5.1), so by source reading the shape is accepted. vLLM reads `content[0].text` too.
  - **Native → translated** (a rollback, a spill or a move). llama.cpp's items carry `encrypted_content: ""`
    (`server-task.cpp:572` @b12020). The translator's `replayedReasoning` accepts only an absent or `null`
    `encrypted_content`, and refuses any key outside `type/id/status/summary/content/encrypted_content`. That is
    `protocol/index.ts:65` at `origin/main` `247b19f`, where the merged #136 still rejects the empty string (the
    codex review confirms it). **Today a rollback would refuse every resumed native session.** Before the canary:
    - accept an empty `encrypted_content` alongside the supported plaintext shapes, and keep rejecting non-empty
      ciphertext;
    - pin llama.cpp's exact item keys.
  - **Tests, both directions:**
    - continuation after one turn, and **rollback after several native turns** (reasoning, tool calls, an image
      output);
    - each in Step 1's corpus, and again live in Step 6.

    Until they pass, **removing the `wire` entry is not a validated rollback.**

### 10.5 Chat clients: dsh, Open WebUI, the Assistant

- **Open WebUI and dsh** speak Chat Completions to local models. On a native chat wire they get exactly what vLLM or
  llama.cpp would give them directly. That is the goal, but it is also a behaviour change in four ways:
  - the response `model` field becomes the served name, verbatim (D3);
  - `reasoning_content` arrives as the server sends it (the same as `forwardReasoning: true`, live on both local
    accounts since 11-02/11-03);
  - parameters the router refused (`top_k`, `min_p`, `chat_template_kwargs`) now take effect;
  - upstream error JSON replaces the router's sentences.

  Each client's history replay must be checked.
  - **Open WebUI's docs** (accessed 2026-11-04, undated; [reasoning models](https://docs.openwebui.com/features/chat-conversations/chat-features/reasoning-models)):
    replay depends on the connection's *Provider* setting.
    - "llama.cpp: Sent back as `reasoning_content`".
    - "Default (Chat Completions): Left out", because "Most Chat Completions APIs reject unknown fields on assistant
      messages".
    - The OpenAI Responses API gets "Stored reasoning items sent back".

    So an Open WebUI connection to the router set to *Default* never replays reasoning, whatever the router does.
    Set to *llama.cpp*, it does, and native chat then delivers it to the template.
  - **dsh:** not checked here.

  Verify both clients against the canary before flipping their accounts.
- **The Assistant** keeps the canonical → chat translator (it constructs `InferenceRequest` directly). The fidelity
  contract and the golden suite cover it; nothing native is needed for it.
- **Guarded fields on native chat:** `n > 1` (budgets and usage), `stream_options` (the router's usage needs),
  `store` (no state). Refuse or declare. `logprobs`: forward (no router invariant depends on it).

### 10.6 Other edge cases

- **Upstream `model` in responses.** Per D3, the client sees the served name: llama-swap's `useModelName` or vLLM's
  `--served-model-name`, not the alias it asked for. Chat UIs that label by the response's `model` show the served
  name.
- **Body size.** Native local wires do not inherit the Codex large-body budget automatically. §9.2 specifies the
  eligibility, the reservations, and the 413 for a large body that only translated targets could serve.
- **Security of forwarding upstream errors.** A llama.cpp template exception embeds an excerpt of the prompt
  (`templateRefusal` matches it). Returning it to the same principal leaks nothing new. Journaling it would, so the
  journal stays code-only.
- **Extra fields from clients reach the server (D2).** Server-state and render-control fields are reserved by default
  (§7.2). `chat_template_kwargs` is not reserved, so a caller can override `enable_thinking`, `preserve_thinking` or
  `reasoning_effort`. That is the point of pass-through; an operator who wants a fixed policy reserves it on that wire.
- **The 8 MiB / 256 MiB stream budgets** apply per path, as today.

---

## 11. Migration plan: small, reversible steps

Every step ships behind configuration, is off by default, and is rolled back by a config write or an image revert.
Each has its tests. Codex review of the plan and of each implementation follows the owner's standing rule.

**Settled:**
- D1-D6 (§13);
- the stopgap (#135/#136, merged; release pending);
- the probe and direct-path results (§12.1).

**Critical path: a thin native Responses path first.**
1. Step 1, limited to the Responses-native, resumed-session and non-streaming cases.
2. Step 3 (d2), the rollback-safety fix.
3. The context gate (Step 6 prerequisites).
4. Step 4, limited to:
   - native Responses;
   - the manifest and the route-scoped `nativeWires` switch;
   - byte-patching transport;
   - admission (§9.2).
5. Step 5 for llama.cpp.
6. Step 6.

Off the critical path: Step 2's full registry, Step 3's broad translator cleanup (a-c, e, f), Steps 7-8, and the
unused translation directions.

**The short-test gate, before any long run or the 48 h watch:**
1. **Deterministic protocol and render tests** (Step 1) pass in docker.
2. **Idle survival is proven with a deliberately shorter client timeout and a longer prefill.** A 90 s prefill cannot
   show survival past a 300 s idle timeout. Example: Codex `stream_idle_timeout_ms` 20 000 against a prefill of 60 s
   or more, the pattern #131/#132 used.
3. **~15-minute real-CLI task checks** pass on the canary route.

Only then come P1-length runs and the watch.

**Step 1: golden round-trip suite (no behaviour change).**
- **Corpus.** Real captured requests, redacted:
  - Codex 0.166 Responses turns: text, tool loop, parallel calls, images in tool output, reasoning echo, the
    `namespace` and `web_search` tools, mid-conversation developer messages;
  - Open WebUI and dsh chat turns, with and without `reasoning_content`;
  - Assistant canonical requests;
  - **resumed sessions** (§10.4): a translated-path history carrying the router's `rs_…` items sent native, and a
    native history carrying llama.cpp's items (`encrypted_content: ""`) sent translated.
- **What it records, per (corpus item, target kind):**
  1. the exact upstream body the router produces today: a snapshot, **diffed against the client body**;
  2. the client-visible stream produced from a recorded upstream stream: a snapshot, plus the verbatim-modulo-declared
     check of §7.3;
  3. for translated pairs, a **render check**: run the body through the target's real chat template (the jinja
     template file, rendered offline with the same engine llama.cpp/vLLM use, or the server's `/apply-template`
     endpoint in a docker test container) and snapshot the **prompt text the model would see**.

  Item 3 is what would have caught bug #4 (two `<|im_end|>` turns) and bug #2 (empty `<think>` blocks) before release.
- **Coverage.** Every row of §3 gets a test that names it. Where a row has no test, write one that pins today's
  behaviour first.
- **Runs in docker** (standing rule), in CI.

**Step 2: mutation registry and `request.wire` journal event (no behaviour change).** Every §3 mutation is emitted by
name. `x-router-mutations` is added beside the old headers. The test is the §8.1 rule 6 invariant, over the corpus.

**Step 3: stop the silent drops (small behaviour changes, each its own PR).**
- (a) The stopgap (merged as #135/#136; release pending): `replayReasoning: {lastTurns: N}` on the local accounts
  (§12). `reasoningEffort: 'pass'` too, so the client's value is not silently dropped, but not as a fix for plan
  delivery.
- (b) `local: { reasoningEffort: true }` in the provider table, once (a) has shown each local template's accepted
  levels. For a template that does not take a level, an upstream 400 is the honest answer; the account can declare
  `effort:<level>`.
- (c) Neutral-drop only for unsupported controls.
- (d) Unknown `finish_reason`: mapped, with the raw value journaled (`upstream_finish_reason`). An error only for
  values that mean incomplete or failed (§8.1 rule 4).
- (d2) **On the critical path.** The translator accepts llama.cpp's reasoning-item shape (`encrypted_content: ""`;
  merged #136 still rejects it, `protocol/index.ts:65`) and keeps rejecting non-empty ciphertext. Then neither a
  rollback nor a placement spill or move breaks resumed sessions (§10.4). The bidirectional continuation tests ship
  with the fix.
- (e) `content: null` sent as `null`.
- (f) The Codex adapter's tool drops become refusals, unless the account declares `drop:tool:web_search` and
  similar. Declare them on today's local accounts in the same config write, so nothing breaks.

**Step 4: per-account `wire` declaration and late body binding (no behaviour change until configured).**
- Implement §7.1-7.3 as a protocol-generic native adapter for `local`. Native Responses comes first (the critical
  path); chat follows.
  - `parseNativeResponses` / `runNative` (codex) are a starting point for the flow, not for the transport. They
    re-serialise the request and rebuild SSE frames, so they are **not** byte-preserving (§7.2, §7.3).
  - The new adapter patches the original bytes and forwards frames as received, in one backpressured read loop.
- Implement the facts extractor and gate parity tests: every gate gives the same answer on the translated and native
  forms of each corpus item.
- Engine:
  - the per-attempt wire plan;
  - the manifest check (§7.1);
  - the route-scoped `nativeWires` switch;
  - the new skip reasons, registered as permanent and request-caused;
  - admission per §9.2.
- Tests:
  - the golden suite in native mode against **recorded** upstream streams from llama.cpp b12020 and vLLM 0.28;
  - the transport cases of §7.3: fragmented UTF-8, multiline SSE, a slow reader, a disconnect, shutdown;
  - non-streaming JSON on both protocols;
  - admission with chunked bodies and a saturated queue;
  - the real-CLI harness in docker: Codex 0.166 → in-process router → a fake upstream that replays recorded
    `/v1/responses` streams. The pattern is the one #131/#132 used, and includes the gate's short-idle-timeout case.

**Step 5: qualify each upstream's Responses API (read-only probes, scheduled when the models are free).** For
llama.cpp b12020 (flash-next) and vLLM 0.28 (fp8, cloud2's 27b), replay the golden corpus straight at the upstream's
`/v1/responses`, then classify and record each item:
- accepted and correct;
- accepted and lossy (e.g. a reasoning item ignored);
- refused.

A wire entry's `qualified` field points at that run.

Also run the Open Responses server compliance suite (§4.11) against each upstream. It is an existing, independent
check of the same surface, and saves writing those cases ourselves.

Tonight's direct-path run (§12.1) is the first real-CLI evidence for llama.cpp. Codex 0.166 straight to `/v1/responses`
completed P1 end to end. It does not replace the corpus run, which covers the shapes that P1 did not exercise.

The expected outcome, from §5: llama.cpp qualifies for plain sessions, with these known drops and refusals:
- `namespace`, `web_search` and `custom` tools are dropped;
- `text.format` is ignored;
- foreign items are refused;
- truncation is reported as `completed`.

vLLM 0.28 needs:
- the real CLI, for its re-parsed ids and non-standard reasoning events;
- the terminal-status check (a `length` run must journal `length`, §7.3);
- the render check of the four 11-01 early-stop turns. Source confirms vLLM merges the calls into the preceding
  message (§5.2); the check confirms it against the deployed template.

Each passing run writes the manifest entry (§7.1). A wire is eligible only when the live backend, model and
template digest match it.

**Step 6: canary on cloud3 (flash-next only).**
- **Prerequisites:**
  - **cloudlab:** a patched Qwen template that renders mid-conversation system/developer messages in place, loaded
    with `--chat-template-file` and passing the golden render checks (D5, §10.3);
  - **the context gate (blocker; D6, §12).**
    - The dev-worker configuration advertises **262,144** tokens for flash-next
      (`ansible/roles/dev_worker/defaults/main.yml:150`). That is the whole two-slot shared pool, and two sessions
      at the observed ~135k already overflow it.
    - Set the advertised window **and** `model_auto_compact_token_limit` against **concurrent** capacity: at most
      half the pool per session, minus output headroom.
    - Qualify automatic compaction and continuation **with both slots occupied**.
    - Until that passes, translated replay stays bounded (`lastTurns`). Native waits, or runs with
      `reasoning-window`;
  - **the rollback-safety fix** (Step 3 d2) and its bidirectional tests (§10.4);
  - **an explicit decision on Codex's `namespace` tools** (§9.6): `allowLoss: ['tool:namespace', 'tool:web_search']`
    on this wire (today's behaviour, made explicit), or wait for llama.cpp #31568;
  - **the short-test gate** above.
- **Config.**
  - `wire: [{models:['qwen3.8-flash-next'], protocol:'responses', holdMs: 30000, allowLoss: […], qualification: …}]` on
    `lan-192.168.0.26-8080`. Its wire defaults are `model` and `set:return_progress`.
  - **Isolation: `nativeWires: true` only on the canary route.** Every other route using this account stays
    translated. Verify that in the journal (`request.wire.path: translated` on the other workers' routes) before
    starting.
  - fp8 (vLLM behind the same llama-swap) stays translated.
- **Who.** One dev worker's key first, on its own canary route; then the four.
- **Validation with the real Codex CLI** on a dev worker (`codex exec -p flash-next -c show_raw_agent_reasoning=true
  … </dev/null`):
  1. a text turn;
  2. a tool loop (exec_command → output → answer);
  3. parallel calls;
  4. an image tool output;
  5. a long prefill under a **shorter** client idle timeout (the gate's idle test, live), then a ≥ 100k-token turn
     with the default timeout;
  6. after the ~15-min checks: the plan-writing task that never delivered (P1-d2-fn), run to completion. The direct
     run delivered the plan at 15.4 min and finished at 23.5 min;
  7. two P1-length sessions **at once** (both slots), through compaction;
  8. a session resumed across the flip, in each direction, including rollback after several native turns (§10.4).
- **Pass, beyond exit 0:**
  - the plan artifact exists and is complete;
  - compaction and continuation succeed with both slots busy;
  - retries are clean: no "stream closed" retry loops and no resent identical requests;
  - the journal shows `request.wire.path: native` and `mutations: [model, set:return_progress]`;
  - the upstream body digest equals `applyDeclared(clientBody)`;
  - Codex displays the thinking, with `show_raw_agent_reasoning`.
- **Watch for 48 h,** only after all of the above: early-stop candidates, `CLIENT_CLOSED` at 300 s, template 500s
  and context-size errors.
- **Rollback:** turn `nativeWires` off on the canary route. It counts as validated only once item 8 has passed.

**Step 7: cloud2 (vLLM) and fp8.** Only if Step 5 qualifies vLLM's Responses path for Qwen tool calling and reasoning
items (§5.2). Otherwise these stay translated, on the now-lossless translator.

**Step 8: native chat for chat clients.** `wire: [{models:['*'], protocol:'chat'}]`. Its wire defaults are `model` and
`stream-usage`.
- **Order:** the platform eval key first, then dsh, then Open WebUI. Each one is checked for reasoning replay and the
  `model` field.
- **Rollback:** per account.

**Code map for Steps 2-4.** A guide, not a spec.

| Change | Where |
|---|---|
| Mutation registry (names, directions, defaults); `applyDeclared(body, mutations)`; the invariant test helper | new `packages/modules/wire/` (pure) |
| `Account.wire`, the qualification manifest, `RoutePolicy.translate` / `nativeWires`; config schema; validator: a `wire` entry's models ⊆ `allowedModels`, and its manifest entry exists; `native` routes keep their Codex check | `contracts/index.ts`, `runtime/config.ts`, `api-openai` validator (`index.ts:292-315`) |
| New skip reasons in `PERMANENT` / `REQUESTED` | `compat/index.ts:95` (`247b19f`) |
| Large-body eligibility for local wires and chat; reservation lifetime; translated-candidate size skip | `api-openai/index.ts:650` (`nativeCaller`, body budget), `kernel/http.ts:99-101` (`preserveRawBody` refuses `maxBytes` above 2 MiB) |
| Envelope + facts extraction for both protocols (read-only) | `modules/protocol/index.ts` (new `factsOf(body, protocol)`, beside `conversationOf`) |
| Gates on facts | `modules/routing/index.ts:288-305`, `compat/index.ts:206-215` (`requiresTools`, `implementationReasons`) |
| Late binding: carry `{protocol, rawBody, facts}` on `InferenceRequest`; parse to the subset lazily per target; a per-attempt wire plan | `api-openai/index.ts:398-428`, `inference/index.ts` around `:715` (where `adaptRequest` runs today) |
| Protocol-generic native adapter: byte-patched request, the hold rule, the terminal-status / usage tap in the single read loop, the failover boundary. The flow comes from `codex.ts:448-529`, but not its `JSON.stringify` transport (`:471`). A chat variant with the `stream-usage` tap; non-streaming JSON forwarding | `adapters/providers/native.ts` (new); `chatcompletions.ts` picks it per wire |
| Client side of native: write the upstream's frames as received, under backpressure. This replaces `sendRaw`'s frame rebuild (`api-openai:485`); no router `response.created` | `api-openai/index.ts:509-539` |
| `request.wire` journal event; `x-router-mutations` header | `inference/index.ts:715-724`, `api-openai:442` |

**Step 9: retire.** Once native carries the Codex and chat traffic for a release, delete:
- the Codex adapter's drop paths (they are refusals already, since Step 3);
- `withoutReplayedReasoning`;
- the neutral-drop table for local.

Keep the translator for canonical callers and unqualified pairs, under its contract.

---

## 12. The stopgap: keep it, bounded

**What it is.** Merged as llm-router #135/#136 (2026-11-04), not yet released. It was first read here as uncommitted
work in `wt-llmr-replay` at ~18:50; the merged form adds the bound.
- `Account.replayReasoning?: boolean | {lastTurns: N | 'all'}`: the parser keeps replayed reasoning, from a Responses
  `reasoning` item of the router's own shape or a chat `reasoning_content`/`reasoning`, as `Message.reasoning` on
  the assistant message it belongs to. `lastTurns` keeps it only on the last N assistant turns.
  - `adaptRequest` keeps it only for an attempt on an account that sets the flag, and journals
    `input:reasoning_replayed`. Every other attempt gets it removed (`input:reasoning`).
  - The chat adapter sends it as `reasoning_content`.
- `Account.reasoningEffort?: 'pass' | 'none' | 'low' | 'medium' | 'high' | 'xhigh'`:
  - `pass` forwards the client's value despite the provider table;
  - a level overrides it, disclosed in `x-router-ignored-params`, journaled as `reasoning_effort:overridden`.

**Verdict: keep, and merge it as the translator's fix.** It is exactly what §7.4 asks for:
- a lossless mapping (reasoning item ↔ `reasoning_content`; `reasoning.effort` ↔ `reasoning_effort`);
- decided per attempt, so a failover sends each target what it takes;
- journaled by name;
- reversible per account.

The translator remains in B: the Assistant, unqualified pairs, cloud2 until vLLM qualifies, and fallbacks. Its
reasoning mapping must be lossless anyway, so this work is not thrown away when Codex moves to the native path. For
Codex on flash-next, the native path (Step 6) supersedes it, with the same mapping done by llama.cpp. Until then it is
the bridge; after that it is the translated path's permanent behaviour.

**Adaptations.**
1. **Bound the replay at the router: `lastTurns`, not `'all'`.** This is D6, decided by the evidence in §12.1.
   - **`'all'` does not fit.** At K=46 it makes a 253,468-token prompt. That leaves under 9k tokens of cloud3's
     262,144-token pool for output, and nothing for the second slot that shares the pool.
   - **The template cannot bound it.** `preserve_thinking=false` keeps the reasoning of every assistant turn after
     the last user message (`chat_template.jinja:119`, `loop.index0 > ns.last_query_index`). In a Codex agent
     session, every assistant turn comes after that message, so it strips nothing.
   - **So the router owns the bound,** as the declared `reasoning-window` mutation (§8.1 rule 5's one exception).
     Start at `lastTurns: 1`, the measured arm. Larger N is unmeasured; raise it only with a probe arm.
   - **Native wires need their own bound.** llama.cpp's `/v1/responses` replays every block, which is the same
     growth. There, Codex auto-compaction against concurrent capacity comes first; `reasoning-window` on the native
     wire is the fallback (Step 6).
   - **The context gate is a blocker** (codex review, §14).
     - The dev workers advertise 262,144 tokens for flash-next (`ansible/roles/dev_worker/defaults/main.yml:150`).
       That is the whole two-slot pool, so two sessions at ~135k overflow it.
     - The advertised window and the compaction threshold must match concurrent capacity, and compaction must
       qualify with both slots occupied.
     - Until then, the bounded translated replay stays as it is.
2. **Effort is not the lever.** `medium` did not improve plan delivery (§12.1), so `reasoningEffort` is set to
   `'pass'` only, to end the silent drop. It is not set to a level to make the model act.
3. **Per account, not a blanket default.** Under §8.1 the lossless behaviour is the target state, and the drop is the
   declared mutation (`drop-replayed-reasoning`, `drop:reasoning_effort`).
   - **Do not invert the replay default across all local accounts** (codex review). Replay is turned on per account:
     only on an account whose context gate and qualification have passed, with that account's own `lastTurns`.
   - The provider-table default for `local` stays "drop" until every local account has done this.
   - Track it as a follow-up issue so the opt-in does not become permanent.

**Smaller points for its review.**
- **Same mapping as llama.cpp's.** The stopgap attaches an item's text to the assistant message its output becomes.
  llama.cpp b12020's converter does the same (§5.1). Two details differ:
  - llama.cpp keeps only the last block of interleaved reasoning;
  - the stopgap joins a turn's blocks with a blank line.

  Both are defensible. Pin each in its golden test so the translated and native paths are known to agree.
- **Chat replay on vLLM.** vLLM 0.28 copies an assistant message's `reasoning` into `reasoning_content` for the
  template on its Responses path (§5.2). That a chat-completions `reasoning_content` field is taken the same way on
  cloud2 is likely but not verified here. Check before setting `replayReasoning` there. An upstream 400 is loud, so
  the risk is availability, not silence.
- **`reasoningEffort: 'high'` on cloud2.** The ailab memory notes that vLLM 400s on `high` with cloud2's template
  (flash-next's template maps `high` → `xhigh`). With `pass`, a client sending `high` now gets that 400 where it
  used to be silently ignored. That is correct under the policy, but announce it to the dev-worker profiles.
- **The orphan rule.** Reasoning that no assistant output follows is dropped. Name it in the contract as
  `input:reasoning_orphan` instead of folding it silently into `input:reasoning`.

### 12.1 Tonight's evidence: the fast path and the replay probe

**The replay A/B probe at K=46** (`flashnext-plan-investigation/out/`):
- **Setup.** Straight to cloud3's llama-swap chat endpoint, bypassing the router; flash-next's own sampling.

| Arm | Samples | Committed the plan | Notes |
|---|---|---|---|
| `asis` (reasoning dropped, as the router sends it today) | 6 | **1** | 48-233 s of thinking before the first call, 5,357-33,315 reasoning chars |
| `last1` (the last turn's reasoning restored as `reasoning_content`) | 6 | **6** | 1.2-3.2 s of thinking. 3 wrote to `plans/` directly; 3 drafted the plan to `/tmp` first, counted as commits. |
| `asis-medium` | 2 | 1 | `medium` effort does not rescue the drop |
| `last1-medium` | 2 | 2 | 9 reasoning tokens each: the replay does the work, not the effort |
| `all` (every block restored) | — | — | **253,468 prompt tokens**: does not fit the shared pool |

- **Commit rate.** 6/6 vs 1/6: one-sided Fisher p ≈ 0.008.
- **Thinking time.** The two arms do not overlap at all (Mann-Whitney one-sided p ≈ 0.001 at n=6 each). With its own
  last reasoning restored, the model does not re-derive its decision; it thinks for about 2 s and acts. That is the
  mechanism of bug #2, measured.
- **Effort.** `medium` helps neither arm in a way that matters, so it is not used for this (§12, adaptation 2).
- **`preserve_thinking=false`** cannot bound `all` in an agent session (§12, adaptation 1).

**The direct-path end-to-end run (P1).** This is the "fast path": Codex 0.166 straight to llama.cpp b12020's
`/v1/responses`, which replays every reasoning block.

| Path | Plan delivered | Run ended | Tokens | Peak context |
|---|---|---|---|---|
| Direct to llama.cpp `/v1/responses` | **15.4 min** | 23.5 min | 48.7k output | 135k |
| Through the router (translated, reasoning dropped) | no plan in 90 min | — | 192.5k output | — |

What it shows:
- **Native works for a real Codex session,** with no 400s from foreign items: §5.1's source reading, confirmed end to
  end for P1's shapes.
- **With its reasoning visible to it, the model thinks far less per turn:** about a quarter of the output tokens
  (48.7k vs 192.5k), for a finished task against an unfinished one.
- **Replaying everything still grows the context** (135k peak with one slot). Native on cloud3 therefore needs the
  context gate in Step 6: the advertised window and compaction set against concurrent capacity and qualified with
  both slots busy, else `reasoning-window`.

---

## 13. Decisions (owner, 2026-11-04)

- **D1. Decided: adopt B (native-first) and the mutation policy (§8.1).** An ADR in `docs/decisions/` follows.
- **D2. Decided: unknown fields on native paths are forwarded.** The router keeps only its invariant guards and the
  per-wire operator-reserved denylist (§7.2). D2 covers unknown *fields*, not known *losses*: a feature the manifest
  lists as lost skips the target unless that loss is explicitly allowed (§9.6).
- **D3. Decided: the response `model` field on native paths is the served name, verbatim.** There is no alias mutation
  (§8.3, §10.6).
- **D4. Decided: dev-worker agent routes translate only through qualified translators,** and otherwise refuse loudly
  (`translate: 'qualified'`, enforced through the qualification manifest, §7.1, §10.4).
- **D5. Decided: fix the template in cloudlab, and the router stops merging system messages.** `system-merge` is
  removed per account once its template is confirmed (§10.3).
- **D6. Decided by evidence (§12.1): replay reasoning, bounded at the router.**
  - Translated: `replayReasoning: {lastTurns: N}`, per account, starting at N=1, the measured arm. Not a blanket
    `local` default.
  - Native on cloud3:
    - the dev workers' advertised window and auto-compaction threshold are set against concurrent capacity (today
      262,144 is advertised, the whole two-slot pool);
    - compaction qualifies with both slots occupied;
    - `reasoning-window` is the fallback.

    This is a **rollout blocker** (codex review).
  - The template's `preserve_thinking` is not the bound (it strips nothing in agent sessions), and effort is not the
    lever.

---

## 14. Codex cross-validation (2026-11-04)

- **Who:** gpt-6-astra, `codex exec --profile plan-review`, reading the source at llm-router `origin/main` `247b19f`
  (#136) and the PR copy. The full text is `scratchpad/codex-design-review/review.md`.
- **Verdict: adopt with changes.** Native-first is right and D1-D5 are retained. Rollout is blocked on context
  capacity, enforced qualification and a replay-compatible rollback. Deliver a thin native Responses path first.

| # | Finding | Severity | Where addressed |
|---|---|---|---|
| 1 | Context control: 262,144 advertised is the whole two-slot pool; compaction is unqualified with two slots | Blocker | §12 adaptations 1 and 3, §13 D6, Step 6 prerequisites and items 7-8 |
| 2 | Native → translated replay rejects llama.cpp's `encrypted_content: ""`; placement spill and moves make it reachable; removing `wire` is not a validated rollback | Blocker | §10.4, Step 3 (d2), Step 1 corpus, Step 6 item 8 and rollback |
| 3 | Qualification is descriptive; D2 must not cover known tool losses; the new reasons must be permanent and request-caused | Blocker | §7.1 (manifest, enforced eligibility, reasons), §9.6, §10.1, §10.2, Step 5, Step 6 (`allowLoss` decision) |
| 4 | Admission is not "unchanged": pre-read eligibility, reservations, translated-candidate size, lifetime, tests | Important | §9.2, §10.6, Step 4, code map |
| 5 | The canary is not isolated on a shared account | Important | §7.1 `nativeWires`, Step 6 isolation |
| 6 | The native adapter is not byte-preserving (`JSON.stringify`, SSE rebuild); the tap needs a backpressure contract | Important | §7.2 (byte patching), §7.3 (one read loop, frames as received, transport tests), Step 4, code map |
| 7 | Non-streaming native responses are missing; today's synthesis conflicts with D3 | Important | §7.3 (non-streaming), Step 4 tests |
| 8 | vLLM: `response.completed` with `status: incomplete`; vLLM does merge function calls | Important | §5.2, §5.4, §7.3 tap, §10.1, Step 5 |
| 9 | Capture is bounded and redacted, not replayable | Important | §8.1 rule 6, §9.5 (completeness label, separate encrypted diagnostic mode) |
| 10 | Critical path: settled items, a short-test gate, a shorter-timeout idle test, plan artifact, continuation and clean retries; a thin native path first | Important | §11 (settled, critical path, short-test gate), Step 6 items and pass criteria |

**Not independently verified by the review**, and still open:
- the deployed template behaviour;
- vLLM's effort-to-template wiring;
- Codex 0.166 compaction on these deployments;
- dsh and Open WebUI replay;
- the new transport's live backpressure and drain behaviour.

---

## Appendix: sources

All accessed 2026-11-04 unless dated.

**Local evidence (this estate).**
- **llm-router source,** `main` `f9eea0d` (in `C:\Users\chifo\work\home\wt-llmr-replay`, committed tree):
  - `packages/modules/protocol/index.ts`, `packages/modules/compat/index.ts`;
  - `packages/adapters/providers/{chatcompletions,common,codex}.ts`;
  - `packages/plugins/api-openai/index.ts`;
  - `packages/modules/inference/index.ts`, `packages/modules/routing/index.ts`;
  - `packages/contracts/index.ts`, `packages/runtime/config.ts`.
- **The stopgap:** the same worktree's uncommitted diff, read ~18:50 local.
- **ailab runbook** `docs/runbooks/llm-router.md` at `bb467f2b`: the 10-31 to 11-03 release sections.
- **ailab memory notes:**
  - `qwen-early-stop-split-history.md`;
  - `local-model-sse-idle-reasoning-invisible.md`;
  - `codex-0166-local-model-traps.md`;
  - `flash-next-agent-failures-2026-11-02.md`.
- **Investigation folder** `scratchpad/flashnext-plan-investigation/`:
  - `chat_template.jinja`, `props.json` (llama.cpp `b12020-0ec4027a2` `/props`), `llama-swap-main.yaml`;
  - `probe_replay.py`, `out/results.jsonl`.

**Upstreams.**
- **llama.cpp:**
  - server README (master): <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>;
  - Responses converter @b12020:
    <https://github.com/ggml-org/llama.cpp/blob/b12020/tools/server/server-chat.cpp>, and @b12110 (L32-326);
  - releases: v0.9.0 = b12110 (2026-11-04), <https://github.com/ggml-org/llama.cpp/releases/tag/v0.9.0>;
  - PRs and issues: #18486 (2026-01-21), #19773, #23041, #25348, #22575, #30863, #30959, #29159, #29412, #30859,
    #24295, #31568, #30773, #27751, #27367: `https://github.com/ggml-org/llama.cpp/{pull,issues}/<n>`.
- **llama-swap** v269 (2026-11-01): `internal/server/server.go`, `internal/server/filters.go`,
  <https://github.com/mostlygeek/llama-swap>.
- **vLLM** v0.28.0 (2026-08-26) and v0.32.0 (2026-10-18): `vllm/entrypoints/openai/responses/{serving,protocol,utils,streaming_events}.py`;
  PRs #57322, #59652, #63238, #56285, #57522; `docs/serving/integrations/codex.md`. <https://github.com/vllm-project/vllm>
- **Codex CLI** rust-v0.166.0 (2026-10-29), <https://github.com/openai/codex>:
  - source: `codex-api/src/common.rs`, `codex-api/src/sse/responses.rs`, `core/src/client.rs`,
    `protocol/src/models.rs`, `tools/src/tool_spec.rs`, `model-provider-info/src/lib.rs`;
  - chat removal: PR #10157 (2026-02-03), first released in rust-v0.95.0 (2026-02-04).

**Gateways.**
- **LiteLLM:**
  - <https://docs.litellm.ai/docs/response_api>
  - <https://docs.litellm.ai/docs/completion/drop_params>
  - <https://docs.litellm.ai/docs/reasoning_content>
  - <https://docs.litellm.ai/docs/proxy/reliability>
  - <https://docs.litellm.ai/docs/pass_through/intro>
  - <https://docs.litellm.ai/release_notes/v1.105.2/v1-105-2>
  - <https://docs.litellm.ai/release_notes/v1.101.6/v1-101-6>
  - issues #27276 (2026-05-06), #16810, #20433, #13608, #12417, #9578: `https://github.com/BerriAI/litellm/issues/<n>`
- **OpenRouter:**
  - <https://openrouter.ai/docs/api-reference/overview>
  - <https://openrouter.ai/docs/features/provider-routing>
  - <https://openrouter.ai/docs/use-cases/reasoning-tokens>
  - <https://openrouter.ai/docs/api/reference/responses/overview.md>
  - <https://openrouter.ai/docs/api/reference/errors-and-debugging>
- **Portkey** (source on `main`, accessed 2026-11-04):
  - `src/services/transformToProviderRequest.ts`, `src/handlers/services/requestContext.ts`,
    `src/handlers/handlerUtils.ts`, `src/providers/anthropic/chatComplete.ts` under
    <https://github.com/Portkey-AI/gateway>
  - <https://portkey.ai/docs/product/ai-gateway/multimodal-capabilities/thinking-mode>
  - <https://portkey.ai/docs/api-reference/inference-api/headers>
- **Kong:**
  - <https://developer.konghq.com/plugins/ai-proxy/>
  - <https://developer.konghq.com/plugins/ai-proxy/changelog/>
- **Envoy AI Gateway / Agent Router:**
  - <https://github.com/envoyproxy/ai-gateway>
  - <https://theagentrouter.ai/docs/concepts/architecture/data-plane> (v1.2)
  - <https://raw.githubusercontent.com/envoyproxy/ai-gateway/main/internal/translator/openai_openai.go>
  - <https://pkg.go.dev/github.com/envoyproxy/ai-gateway/internal/translator> (v1.2.0)
- **Gateway API Inference Extension / llm-d:**
  - <https://github.com/kubernetes-sigs/gateway-api-inference-extension>
  - <https://cloud.google.com/kubernetes-engine/docs/how-to/configure-body-based-routing>
- **vLLM Semantic Router:**
  - <https://vllm-sr.ai/docs/troubleshooting/vsr-headers>
  - <https://agentgateway.dev/blog/2026-07-28-agentgateway-semantic-router-integration/> (2026-07-28)
- **Cloudflare AI Gateway:**
  - <https://developers.cloudflare.com/ai-gateway/usage/providers/openai/> (2026-04-20)
  - <https://developers.cloudflare.com/ai-gateway/usage/chat-completion/> (2026-10-02)
  - <https://developers.cloudflare.com/ai-gateway/configuration/fallbacks/> (2026-04-20)
  - <https://developers.cloudflare.com/ai-gateway/observability/logging/> (2026-11-04)
- **Helicone:**
  - <https://docs.helicone.ai/references/proxy-vs-async.md>
  - <https://docs.helicone.ai/gateway/overview>
  - <https://github.com/Helicone/ai-gateway>
- **TensorZero** (archived 2026-06-12):
  - <https://github.com/tensorzero/tensorzero>
  - `docs/gateway/api-reference/inference.mdx`, `docs/gateway/guides/retries-fallbacks.mdx`,
    `docs/gateway/call-the-openai-responses-api.mdx`

**Providers and clients.**
- **OpenAI** reasoning guide: <https://developers.openai.com/api/docs/guides/reasoning>
- **Anthropic** extended thinking: <https://platform.claude.com/docs/en/build-with-claude/thinking>
- **Gemini** thought signatures: <https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures>
- **Open Responses** (2026-01-15): <https://simonwillison.net/2026/Jan/15/open-responses/>
- **Open WebUI** reasoning models: <https://docs.openwebui.com/features/chat-conversations/chat-features/reasoning-models>

**Codex cross-validation (§14).**
- The review: `scratchpad/codex-design-review/review.md`.
- Its source references, at llm-router `origin/main` `247b19f`:
  - `packages/modules/protocol/index.ts:65`;
  - `packages/modules/compat/index.ts:95`, `:205`;
  - `packages/modules/placement/index.ts:6`;
  - `packages/modules/trajectory/index.ts:32`, `packages/modules/observability/index.ts:3`;
  - `packages/plugins/api-openai/index.ts:445`, `:485`, `:650`;
  - `packages/adapters/providers/codex.ts:471`;
  - `packages/kernel/http.ts:99-101` (checked here).
- Also: ailab `ansible/roles/dev_worker/defaults/main.yml:150`, and llama.cpp b12020
  `tools/server/server-task.cpp:572`.

The lines checked here against `247b19f`: `protocol:60-65`, `compat:93-98`, `placement:1-14`, `trajectory:28-36`,
`kernel/http.ts:93-106`.

**Not verified here.**
- llm-d EPP body rewrites;
- vSR system-prompt injection on the live docs;
- Portkey's mid-stream fallback;
- LiteLLM pass-through byte fidelity;
- a vLLM server-wide `chat_template_kwargs` default flag in 0.28;
- the Qwen3.8 card wording.



