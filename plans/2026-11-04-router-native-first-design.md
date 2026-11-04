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

**What changes first.** The changes are small and reversible, in this order (§11):
1. a golden round-trip suite that pins today's behaviour;
2. a mutation registry and journal event;
3. stopping today's silent drops;
4. a per-account, per-model `wire` declaration with late binding of the body;
5. a Codex → llama.cpp `/v1/responses` canary on cloud3 (after a cloudlab template fix), then cloud2 (vLLM) once it
   is qualified;
6. chat-client pass-through.

**The in-flight stopgap** (`Account.replayReasoning` plus `Account.reasoningEffort`) fits this direction. Keep it, with
two adaptations, as the translator's fix for replayed reasoning and effort:
- `pass` becomes the default for local accounts;
- the context-growth risk is handled at the server template, not in the router (§12).

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
- **Probe.** The replay A/B probe (`out/results.jsonl`) completed phase A at 17:04Z. These are its results at the
  decision point K=46; other phases are still running:

  | Arm | Committed the plan | Reasoning before acting |
  |---|---|---|
  | `asis` (reasoning dropped, as the router sends it) | 1/6 | 48-233 s |
  | `last1` (last block restored) | 5/6 | 1.2-3.2 s |

  The arms' thinking times do not overlap at all. Details and caveats are in §12.1.

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
| Output-limit enforcement (Codex only) | visible output (read) | Truncates the stream; declared (`x-router-output-limit`) |
| Journal, telemetry, `explain_request` | everything (read) | No |
| Vision / tools / Assistant / image-model gates | facts about the request: images, tools, agent id (read) | No |
| Progress keepalives (#131) | nothing | Adds events to the stream (§9.4) |
| Drain | nothing | Cuts the stream without a terminal event (deliberate: Codex reconnects) |

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
| `systemMessages: merge` folds every system/developer message, in order, into one leading system message, moving mid-conversation text to the head | `:182-189`, `:200` | `x-router-adapted: system-messages-merged` | N for the current GGUF template, but lossy (position changes meaning). Better fixed at the server's template (§10.3). |
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
| **Unknown `finish_reason` → `"stop"`** | `:99-113` | no | **R** (silent; an `error`/`abort` would read as a normal stop) |
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
- **The terminal event is always `response.completed`, even when the status is `incomplete`.**
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
  - merging contiguous assistant items (bug #4): verified for llama.cpp (#19773, `merge_prev`). **Not verified for
    vLLM 0.28.** Bug #4 was measured on cloud2's vLLM. If vLLM's Responses converter emits a `message` item and its
    `function_call` items as two assistant messages, native vLLM would bring the early stops back. Step 5 must
    render-check it;
  - reasoning streamed as Responses events (bug #1).
- **What neither does well:**
  1. `namespace` / `custom` / `web_search` tools: silently dropped (llama.cpp; vLLM 0.28 custom), a 400 (vLLM 0.32
     grammar custom), or supported (vLLM namespaces).
  2. Foreign item types, `additional_tools` and encrypted or content-less reasoning: 400s.
  3. Mid-conversation system/developer messages under Qwen templates: a template raise.
  4. No `response.incomplete`; truncation reported as `completed`.
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
| **B. Native-first, translate as fallback** | Forward the client's body verbatim when the target speaks its protocol; translate only otherwise, under a fidelity contract | Zero translation where it matters most (Codex → llama.cpp/vLLM Responses; chat clients → chat servers). Routing jobs unaffected. New upstream features reach clients with no router change. Same shape as the existing Codex-native path. | Two paths to maintain. Gates must read native bodies. Failover is limited to targets of the same protocol, or to a qualified translator. Upstream converters (llama.cpp's Responses → chat) have their own gaps. | **Recommended** |
| **C. Pure pass-through, never translate** | Helicone- or Cloudflare-style: a route is pinned to one protocol; a mismatch is a 400 | No mutation at all; simplest | The Assistant and internal callers speak the canonical type; there is no Codex on an upstream without a usable Responses API (vLLM's non-harmony path may be one, §5.2); no heterogeneous fallback | Reject; but B must be able to run in C's mode per route (`translate: never`) |
| **D. Put a translating proxy (LiteLLM) in front of the local servers** | The router stays a pure router; LiteLLM does Responses → chat | Moves code out of the router | The translation bugs move with it (§4.1: LiteLLM's bridge split choices and dropped `additional_tools` in earlier releases, and #27276 on tool names and tool types is open); an extra hop, extra config and an extra failure domain; the router can no longer see or gate the content | Reject |
| **E. Let the upstream translate** (a variant of B) | Forward Responses to llama.cpp's own `/v1/responses`, which converts to chat internally, next to its template, reasoning parser and tool parser | The converter is maintained by the people who own the template machinery, and maps reasoning input items to the template's `reasoning_content` (§5.1) | Its fidelity is version-dependent (b12020 vs latest), and its gaps are now the router's problem to detect | **Adopt within B** for llama.cpp, gated by the same golden suite as any translator |

**Why B over A.** A cannot be made side-effect-free: a normaliser changes the model's prompt by construction. The best
it can do is make the changes visible, which is what `x-router-adapted` and `parameters_adjusted` do today, and bugs
#2-#4 shipped anyway. B removes the changes rather than disclosing them, wherever a native path exists, and that is
where the traffic is: Codex agents on local models (Responses) and chat clients on local models (chat). A remains for
what has no native path. Today that is the Assistant and internal callers (canonical → chat), and Codex on any
upstream whose Responses API fails the golden suite.

**Why B over C.** C would break the Assistant (canonical request type), and Codex on vLLM if vLLM's Responses path
does not qualify for Qwen tool calling. B with `translate: never` on a route gives C's guarantee where the owner wants
it.

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
  mutations?: DeclaredMutation[];    // the ONLY changes allowed besides `model` (see §8.2)
  qualified?: string;                // the golden-suite run that qualified it (journal id / date), for audit
}>;
// RoutePolicy (new, optional)
translate?: 'allow' | 'never';       // never: a target whose wire lacks the client's protocol is skipped (C's guarantee)
```

The existing Codex mechanism (`RoutePolicy.native: ['codex-responses']`, `ProviderSupport.native`) becomes one case of
this: the codex provider declares `wire: [{models:['*'], protocol:'responses'}]` implicitly. The route-level `native`
setting stays as a compatibility alias during migration.

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
  - body size.

  Anything else goes through untouched, and the upstream validates it.

### 7.3 Response flow: verbatim stream, observing tap

The upstream's bytes go to the client as they come. A tap parses the SSE events in parallel (the Codex path already
does this: `codex.ts:489-521`) and extracts what the router needs:
- usage: `response.completed.usage`, or the chat usage chunk;
- the finish reason;
- first output: the first output item or delta, which is the failover boundary;
- error codes;
- reasoning/text/tool character counts for the journal.

It never writes. The only permitted differences between upstream bytes and client bytes are:
1. SSE comments (`: ok`, `: keepalive`), which every SSE parser ignores;
2. holding lifecycle events (`response.created`/`in_progress`/`queued`) until the first other event, so a failure
   before output can fail over (a timing change, as today);
3. a router-originated terminal event when the router itself ends the stream (timeout, budget, client gone), or
   after an upstream error that is not a protocol event (llama.cpp's untyped `data: {"error":…}`, §5.1), and only
   when the upstream sent no terminal event;
4. declared response mutations (§8.2), each opt-in, e.g. `overload-code` (today's `server_is_overloaded` rewrite).

A test asserts this per protocol: strip comments and the declared mutations from the client stream, and the result
must equal the upstream stream byte for byte.

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
2. **Every other change is a declared mutation.** It is:
   - named, from a fixed registry;
   - configured per account (or per route), opt-in;
   - journaled per attempt (`request.wire.mutations`, names only, never values);
   - disclosed to the client in one header (`x-router-mutations`; it replaces `x-router-adapted` and
     `x-router-ignored-params`, which stay during migration).
3. **No silent drops.** An unknown field is forwarded (native) or refused by name (translated). A known field the target
   cannot take is refused by name, unless the account declares the drop. "Neutral" drops are allowed only for a value
   that equals **the target's** documented default; a target that supports the control always gets the client's value.
4. **No silent rewrites of meaning.** Mapping a value (finish reasons, roles, effort levels) is a translation and lives in
   the contract. An unmappable value is an error, not a default (no `?? 'stop'`).
5. **Policy about what the model sees belongs at the server, not the router.** Examples: preserving past thinking,
   where system text goes, sampling defaults. These are template and server settings (`--chat-template-file`,
   `--chat-template-kwargs`, `--reasoning-preserve`, llama-swap presets), owned in cloudlab IaC. The router forwards.
6. **The invariant is testable.** `upstreamBody == applyDeclared(clientBody, mutations)` is checked in the golden suite.
   With diagnostic capture on, the journal can store both digests (`sha256` of client and upstream bodies) so any request
   can be audited after the fact.

### 8.2 The mutation registry (initial)

| Name | Direction | What | Default | Why it exists |
|---|---|---|---|---|
| `model` | req | replace `model` with the deployment model | always | routing |
| `store-false` | req | force `store: false` (Codex backend) | codex wire | no server-side state |
| `cache-key-salt` | req | salt `prompt_cache_key` after overloads | codex wire | Codex re-key |
| `stream-usage` | req+resp | add `stream_options.include_usage: true`; remove the usage-only chunk from the client stream if the client did not ask | chat wire | budgets (§9.3) |
| `system-merge` | req | today's `systemMessages: merge` | off | template workaround; prefer a template fix |
| `developer-as-system` | req | `developer` → `system` | off (translated: per account) | template compatibility |
| `drop:<param>` | req | today's `params.drop` | off | a server that chokes on a control |
| `effort:<level>` | req | fixed reasoning effort (the stopgap's level form) | off | operator policy |
| `overload-code` | resp | `server_is_overloaded` → `server_error` after output | codex wire | Codex retry behaviour |
| `progress-events` | resp | `response.in_progress` while the upstream thinks (#131) | translated only | Codex idle timer |
| `set:return_progress` | req | add `return_progress: true` (llama.cpp prompt-progress events) | off; on for the llama.cpp responses wire | Codex idle timer during a long prefill, from the upstream's own events |
| `drop-replayed-reasoning` | req | today's behaviour, kept as an explicit opt-in for a template that breaks on it | off | compatibility |

### 8.3 What happens to each of today's mutations

| Today | In B |
|---|---|
| Codex adapter drops (hints, `include`, non-function tools, reasoning items) | **Native:** none happen; the body goes as sent. **Translated:** hints (`prompt_cache_key`, `client_metadata`, `text.verbosity`, `reasoning.summary`, encrypted-only `include`) become contract line items, "mapped: ignored by chat servers", because they carry no instruction to the model. Reasoning items: mapped (the stopgap). `namespace` tools: flattened, then re-wrapped, or refused. `web_search` / `custom` tools: refused by name unless the account declares `drop:tool:<type>`. |
| Subset allowlist (unknown keys → 400) | Native: forwarded. Translated: stays a 400 (loud is right). |
| Chat `reasoning_content` dropped on replay | Native chat: forwarded (llama.cpp and vLLM take it). Translated: mapped. |
| Neutral-value drops | Removed for targets that support the control. Kept only where the target lacks the control and the value is its default. |
| `reasoning_effort` dropped for local | Native: forwarded. Translated: forwarded (the provider table says `true` for local). An account may declare `effort:<level>` or `drop:reasoning_effort`. |
| `systemMessages: merge` | Kept as `system-merge`, opt-in; recommend a template fix instead (§10.3) |
| `developer` → `system` | Native: forwarded. Translated: `developer-as-system`, per account |
| `content: null` → `''` | Translated: send `null` (the OpenAI shape); change per account only if a template needs it |
| Unknown `finish_reason` → `stop` | Native: verbatim. Translated: the contract maps the known values; unknown → `INCOMPLETE_STREAM` / `UPSTREAM_MALFORMED` |
| Upstream error text hidden | Native: the upstream's error JSON goes to the caller as sent (it is the caller's own conversation). The journal still keeps only the classification and code. |
| Response ids, `model` alias, usage shape | Native: verbatim, **including the upstream `model`**. This is a decision point (§13 D3): mask the alias as a declared `model-alias` response mutation, or show the served model. |
| Reasoning after visible output not forwarded | Native: verbatim. Translated: contract line (multiple reasoning items in order) |
| Progress events, keepalive comments | §9.4 |
| Codex-native mutations (`model`, `store`, headers, salt, overload code) | Unchanged, now registry entries |

---

## 9. The router's legitimate features in each mode

### 9.1 Failover

- **Before first output: both modes, as today.** The boundary is the first output item or delta the tap sees.
  Lifecycle events are held until then (today's Codex rule, generalised). Reasoning counts as output once forwarded,
  as #132 decided.
- **After first output: never,** in both modes. That is today's rule (`inference/index.ts:851`, "emitted"), and the
  research (§4.9) shows it is the industry norm. A mid-stream failover would splice two models' outputs into one
  response.
- **Across heterogeneous targets.** A failover target must be able to serve the *same client body*: it speaks the
  client's protocol natively, or a qualified translator exists for the pair. Otherwise it is skipped
  (`protocol_unavailable`). Encrypted reasoning (`encrypted_content` from an OpenAI account) cannot go to a local
  model. The translator refuses it (today's `replayedReasoning` already returns false for non-null
  `encrypted_content`); native local targets must refuse it too, and the facts extractor flags it
  (`reasoning.encrypted`), so routing skips those targets instead of letting the upstream fail. Conversation
  placement already keeps a conversation on one account, which makes this rare.

### 9.2 Admission queue, concurrency, drain

Unchanged in both modes: they act before the body is sent, and on the connection, not the payload. The queue (#133)
holds the request with its body budget reservation, as today. Drain cuts a stream with no terminal event (deliberate,
so Codex reconnects). On a native stream that already carries the upstream's terminal event, it ends normally, as
today (`api-openai:557-562`).

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
  it. Router enforcement (`OutputLimiter`) stays Codex-only.

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
- **Content capture:** as today (`capture.level`). On a native path the captured request is the client body, which
  is now also exactly what the upstream saw, minus the listed mutations. That is a debugging win: a journal entry
  replays straight to the upstream with no router in between, which is how the #130 and flash-next investigations
  had to work by hand.

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

Some targets cannot handle a kind the request uses. llama.cpp silently drops `custom` and `namespace` tools (§5.1);
vLLM 0.32 refuses grammar tools (§5.2). When the target's `wire` qualification records that, the router either skips
the target before the network, with the reason named, or forwards and journals the upstream's drop. It never sends a
request that will 400, and never lets a drop go unrecorded. Which of the two is per wire configuration: D2's
default is to forward and journal.

---

## 10. Risks and edge cases

### 10.1 Upstream Responses-API gaps

| Gap | llama.cpp b12020 | vLLM 0.28 | Router response in B |
|---|---|---|---|
| Truncation reported as `completed` | yes (#30773) | yes (always `response.completed`) | Tap: an output-token count at the client's limit is journaled as `finish: length (upstream says completed)`; no rewrite. Codex sends no output limit, so truncation is mostly context exhaustion. |
| Mid-stream error | untyped `data: {"error":…}`; Codex skips it, then retries on "stream closed" | — | Tap recognises it: forward the bytes, then end with the router's own `response.failed` (rule 3, §7.3), so Codex sees an error instead of a mystery retry |
| `namespace` / `web_search` / `custom` tools | silently dropped (server log only) | namespace OK; `custom` dropped (0.28) or 400 (0.32, grammar) | Facts: `toolKinds`. The wire's qualification lists what the upstream drops; routing either refuses or journals it as an **upstream drop** (`upstream-drops: tool:namespace`), never hidden |
| `text.format` (structured output) | silently ignored | mapped | Facts: `responseFormat`; a llama.cpp native wire refuses it (or the request is translated for that target, where `response_format` works) |
| Foreign items (`additional_tools`, `custom_tool_call`, `web_search_call`, `item_reference`) | 400 | 400 in 0.32; passed through raw in 0.28 (may break the renderer) | Facts: refuse before the network with the item named (saves a prefill), or skip the target |
| Reasoning items: encrypted, content-less, `summary: null` | 400 | 400 if encrypted | Facts: `reasoning.encrypted` / `reasoning.contentless`; skip local targets; placement keeps GPT conversations on GPT accounts |
| Mid-conversation system/developer | template raise (Qwen) | template raise (Qwen) | Template fix in cloudlab (§10.3); `system-merge` stays available as a declared mutation on the translated path |
| `previous_response_id` / `store: true` | 400 | 404 / store off | The router already refuses (`parseNativeResponses`) |
| Stream item ids differ from the final response | no | **yes** (re-parse) | Qualify with the real Codex CLI (it builds items from `output_item.done`) |
| Text + calls of one reply rendered as one assistant turn (bug #4) | yes (`merge_prev`) | **unverified** | Step 5 render check with the four 11-01 stop turns. If it splits, vLLM stays translated (the translator has the #130 fix). |
| Non-standard events | — | `response.reasoning_part.*` (0.28) | Verbatim; Codex ignores unknown types [infer]; qualify |

**Version drift is the standing risk.** Each upstream upgrade re-runs the qualification (Step 5) before the `wire`
entry's `qualified` field moves. llama.cpp's open PRs (#31568 namespaces, #29412 content-less reasoning) and vLLM 0.32
change several rows.

### 10.2 What Codex sends, and what each path does with it

Sources: §5.1-5.3. Codex 0.166 for a model outside its catalog, which is what the dev workers' local profiles are.

| Codex sends | Native → llama.cpp b12020 | Native → vLLM 0.28 | Translated (today + stopgap) |
|---|---|---|---|
| Leading `developer` message (base instructions) | → system; fine | → template; fine as the first message | → system |
| Mid-conversation `developer` | **template raise** | **template raise** | `system-merge` (lossy) |
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
tools, and replayed reasoning. The first needs the template fix. The second is today's behaviour as well (the router
already drops them), so it is not a regression; it becomes a journaled upstream drop. The third is the point of the
change.

The facts extractor knows these kinds. Per wire, a kind the target cannot handle either skips the target before the
network, with the reason named, or is forwarded with the upstream's drop journaled (§9.6). A request is never sent
just to fail.

### 10.3 Mid-conversation system messages

- **The cause.** Codex and Claude Code inject `developer`/`system` messages mid-conversation. Qwen's GGUF template
  raises "System message must be at the beginning" (`chat_template.jinja:109-110`). It merges only the *leading*
  run (`:45-56`).
- **The router's options are all lossy.** Merge everything into the head (today, `system-merge`), or turn the message
  into a user message. Each changes what the model sees, in the router, where nobody looks.
- **Recommended.** Fix it where the template lives. A cloudlab-owned `--chat-template-file` (llama.cpp) /
  `--chat-template` (vLLM) that renders a mid-conversation system/developer message as its own `<|im_start|>system`
  block is a one-line template change, versioned with the server, and testable against the golden corpus. Until
  then, keep `system-merge` as a declared mutation on the accounts that need it.
- **On the native path,** llama.cpp's Responses converter decides what `developer` items become. That is one more
  reason the golden suite runs against the real upstream.

### 10.4 Failover across heterogeneous accounts

A route chain mixing native and translated targets serves the same request two different ways. The journal says
which (`request.wire.path`); the golden suite qualifies both. Rules:
- Same conversation: placement keeps it on one account.
- A fallback that cannot take the body is skipped, not "adapted harder".
- `translate: never` on a route forbids the mixed case outright. Use it for agent routes if the owner prefers
  predictability to availability.

### 10.5 Chat clients: dsh, Open WebUI, the Assistant

- **Open WebUI and dsh** speak Chat Completions to local models. On a native chat wire they get exactly what vLLM or
  llama.cpp would give them directly. That is the goal, but it is also a behaviour change in four ways:
  - the response `model` field becomes the served name (§13 D3);
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

- **Upstream `model` in responses.** llama-swap's `useModelName` and vLLM's served name both appear (§13 D3).
- **Body size.** Native Codex bodies use the large native budget (`codexNativeMaxBodyMiB`). Native local Responses
  inherit it; the 2 MiB subset limit stays for translated requests.
- **Security of forwarding upstream errors.** A llama.cpp template exception embeds an excerpt of the prompt
  (`templateRefusal` matches it). Returning it to the same principal leaks nothing new. Journaling it would, so the
  journal stays code-only.
- **Extra fields from clients reach the server.** `chat_template_kwargs` lets a caller override `enable_thinking`,
  `preserve_thinking` or `reasoning_effort`. That is the point of pass-through, but an operator who wants a fixed
  policy must declare it (`drop:chat_template_kwargs`, or an `effort:` mutation).
- **The 8 MiB / 256 MiB stream budgets** apply per path, as today.

---

## 11. Migration plan: small, reversible steps

Every step ships behind configuration, is off by default, and is rolled back by a config write or an image revert.
Each has its tests. Codex review of the plan and of each implementation follows the owner's standing rule.

**Step 1: golden round-trip suite (no behaviour change).**
- **Corpus.** Real captured requests, redacted:
  - Codex 0.166 Responses turns: text, tool loop, parallel calls, images in tool output, reasoning echo, the
    `namespace` and `web_search` tools, mid-conversation developer messages;
  - Open WebUI and dsh chat turns, with and without `reasoning_content`;
  - Assistant canonical requests.
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
- (a) The stopgap: `replayReasoning`, plus `reasoningEffort: 'pass'` (§12).
- (b) `local: { reasoningEffort: true }` in the provider table, once (a) has shown each local template's accepted
  levels. For a template that does not take a level, an upstream 400 is the honest answer; the account can declare
  `effort:<level>`.
- (c) Neutral-drop only for unsupported controls.
- (d) Unknown `finish_reason` becomes an error.
- (e) `content: null` sent as `null`.
- (f) The Codex adapter's tool drops become refusals, unless the account declares `drop:tool:web_search` and
  similar. Declare them on today's local accounts in the same config write, so nothing breaks.

**Step 4: per-account `wire` declaration and late body binding (no behaviour change until configured).**
- Implement §7.1-7.3, generalising `parseNativeResponses` / `runNative` (codex) into a protocol-generic native adapter
  for `local`: Responses and chat.
- Implement the facts extractor and gate parity tests: every gate gives the same answer on the translated and native
  forms of each corpus item.
- Engine: per-attempt wire plan; the `protocol_unavailable` and `translation_unsupported:<field>` reasons.
- Tests:
  - the golden suite in native mode against **recorded** upstream streams from llama.cpp b12020 and vLLM 0.28;
  - the real-CLI harness in docker: Codex 0.166 → in-process router → a fake upstream that replays recorded
    `/v1/responses` streams. The pattern is the one #131/#132 used.

**Step 5: qualify each upstream's Responses API (read-only probes, scheduled when the models are free).** For
llama.cpp b12020 (flash-next) and vLLM 0.28 (fp8, cloud2's 27b), replay the golden corpus straight at the upstream's
`/v1/responses`, then classify and record each item:
- accepted and correct;
- accepted and lossy (e.g. a reasoning item ignored);
- refused.

A wire entry's `qualified` field points at that run.

Also run the Open Responses server compliance suite (§4.11) against each upstream. It is an existing, independent
check of the same surface, and saves writing those cases ourselves. Fold in tonight's fast-path test (§12.1).

The expected outcome, from §5: llama.cpp qualifies for plain sessions, with these known drops and refusals:
- `namespace`, `web_search` and `custom` tools are dropped;
- `text.format` is ignored;
- foreign items are refused;
- truncation is reported as `completed`.

vLLM 0.28 needs two checks:
- the real CLI, for its re-parsed ids and non-standard reasoning events;
- **a render check that one reply's text and calls stay one assistant turn.** Replay the four 11-01 early-stop
  turns, as #130 did. Without it, native vLLM risks bringing bug #4 back.

**Step 6: canary on cloud3 (flash-next only).**
- **Prerequisites (cloudlab):** a patched Qwen template that renders mid-conversation system/developer messages in
  place, loaded with `--chat-template-file` and passing the golden render checks (§10.3); and the `preserve_thinking`
  decision (§12, D6).
- **Config.** `wire: [{models:['qwen3.8-flash-next'], protocol:'responses', mutations:['set:return_progress']}]` on
  `lan-192.168.0.26-8080`. fp8 (vLLM behind the same llama-swap) stays translated.
- **Who.** One dev worker's key first, via a route that only it uses; then the four.
- **Validation with the real Codex CLI** on a dev worker (`codex exec -p flash-next -c show_raw_agent_reasoning=true
  … </dev/null`):
  1. a text turn;
  2. a tool loop (exec_command → output → answer);
  3. parallel calls;
  4. an image tool output;
  5. a turn with ≥ 100k tokens of context (a prefill of ≥ 90 s: the `return_progress` check);
  6. the plan-writing task that never delivered (P1-d2-fn), run to completion.
- **Pass, each turn:**
  - exit 0;
  - the journal shows `request.wire.path: native` and `mutations: [model, set:return_progress]`;
  - the upstream body digest equals `applyDeclared(clientBody)`;
  - Codex displays the thinking, with `show_raw_agent_reasoning`;
  - no `CLIENT_CLOSED` at 300 s on the ≥ 100k-token turn.
- **Watch for 48 h:** early-stop candidates, `CLIENT_CLOSED` at 300 s, and template 500s.
- **Rollback:** remove the `wire` entry (one `If-Match` PUT).

**Step 7: cloud2 (vLLM) and fp8.** Only if Step 5 qualifies vLLM's Responses path for Qwen tool calling and reasoning
items (§5.2). Otherwise these stay translated, on the now-lossless translator.

**Step 8: native chat for chat clients.** `wire: [{models:['*'], protocol:'chat', mutations:['stream-usage']}]`.
- **Order:** the platform eval key first, then dsh, then Open WebUI. Each one is checked for reasoning replay and the
  `model` field.
- **Rollback:** per account.

**Code map for Steps 2-4.** A guide, not a spec.

| Change | Where |
|---|---|
| Mutation registry (names, directions, defaults); `applyDeclared(body, mutations)`; the invariant test helper | new `packages/modules/wire/` (pure) |
| `Account.wire`, `RoutePolicy.translate`; config schema; validator: a `wire` entry's models ⊆ `allowedModels`; `native` routes keep their Codex check | `contracts/index.ts`, `runtime/config.ts`, `api-openai` validator (`index.ts:292-315`) |
| Envelope + facts extraction for both protocols (read-only) | `modules/protocol/index.ts` (new `factsOf(body, protocol)`, beside `conversationOf`) |
| Gates on facts | `modules/routing/index.ts:288-305`, `compat/index.ts:206-215` (`requiresTools`, `implementationReasons`) |
| Late binding: carry `{protocol, rawBody, facts}` on `InferenceRequest`; parse to the subset lazily per target; a per-attempt wire plan | `api-openai/index.ts:398-428`, `inference/index.ts` around `:715` (where `adaptRequest` runs today) |
| Protocol-generic native adapter: held lifecycle events, terminal and usage tap, failover boundary (lift from `codex.ts:448-529`); chat variant with the `stream-usage` tap | `adapters/providers/native.ts` (new); `chatcompletions.ts` picks it per wire |
| Client side of native: write upstream SSE frames as they come (exists: `sendRaw`, `api-openai:485`, `:512-514`); no router `response.created` | `api-openai/index.ts:509-539` |
| `request.wire` journal event; `x-router-mutations` header | `inference/index.ts:715-724`, `api-openai:442` |

**Step 9: retire.** Once native carries the Codex and chat traffic for a release, delete:
- the Codex adapter's drop paths (they are refusals already, since Step 3);
- `withoutReplayedReasoning`;
- the neutral-drop table for local.

Keep the translator for canonical callers and unqualified pairs, under its contract.

---

## 12. The stopgap: keep it, with two adaptations

**What is in flight.** It is the uncommitted work in the `wt-llmr-replay` worktree, read 2026-11-04 ~18:50; it was
not reviewed here as a PR.
- `Account.replayReasoning?: boolean`: the parser keeps replayed reasoning, from a Responses `reasoning` item of the
  router's own shape or a chat `reasoning_content`/`reasoning`, as `Message.reasoning` on the assistant message it
  belongs to.
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
1. **Invert the defaults once qualified.** Under §8.1 the lossless behaviour (replay; pass the effort) is the default,
   and the drop is the declared mutation (`drop-replayed-reasoning`, `drop:reasoning_effort`, `effort:<level>`).
   Ship the stopgap opt-in as written. After Step 1's render checks and a week on cloud3, flip the provider-table
   default for `local` and keep the flags as overrides. Track it as a follow-up issue so the opt-in does not become
   permanent.
2. **Context growth is decided at the template, not by a router window.**
   - **The risk.** Codex resends every reasoning item each turn, and Qwen3.8's template renders all of them
     (`preserve_thinking` defaults to true). At K=46 the `asis` prompt was already 105,608 tokens. flash-next's
     slots share 262,144 tokens (`-c 262144 --parallel 2 --kv-unified`), and fp8 has `--max-model-len 131072`.
     Restoring every past think block can multiply the prompt.
   - **Why not a window in the router.** That would be a new mutation, and what the model sees is server policy
     (§8.1 rule 5). The template already has the switch: `preserve_thinking: false` keeps reasoning only after the
     last user query (`chat_template.jinja:119`).
     - That matches OpenAI's minimum guidance, "since the last user message".
     - GPT-5.6's default `reasoning.context: all_turns` keeps every turn (§4.10).
     - So both settings have vendor precedent, and the choice is per model.
     - In a Codex agent task, "since the last user message" is usually the whole task, so `false` limits growth
       only across tasks.
   - **Two decisions per model:**
     - the `preserve_thinking` default, set in cloudlab: `--chat-template-kwargs '{"preserve_thinking": …}'` or
       `--reasoning-preserve` / `--no-reasoning-preserve` on llama.cpp (both in the server README, current master);
       on vLLM, a `--chat-template` file with that default (a server-wide kwargs flag was not verified for 0.28);
     - Codex's `model_auto_compact_token_limit` in the local-model profiles.

     Add an `all` arm and a `preserve_thinking=false` arm to `probe_replay.py` before turning replay on for
     flash-next. The probe's arms restore only the last one or three blocks (`last1`, `last3`, plus effort and
     sampling variants). Neither the router nor llama.cpp produces those.

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

**The fast-path test** (Codex straight at llama.cpp `/v1/responses`) had written no result to
`flashnext-plan-investigation/out/` at 17:06Z. That folder holds only the replay A/B probe. Step 5 must therefore start
from the source reading in §5.1, not from a measured run. When the result lands, it slots into Step 5's
classification. What to read in it:
1. Did any turn get a 400 "Cannot determine type of 'item'", or a template raise?
2. Did Codex show the reasoning and survive long thinking with no reconnects?
3. Did the plan get written?

**The replay A/B probe**, phase A, complete at 17:04Z (`out/results.jsonl`):
- **Setup.** Straight to cloud3's llama-swap chat endpoint, bypassing the router; decision point K=46; flash-next's
  own sampling. Phases B (effort `medium`) and later were still running, deadline 19:40Z, and are not folded in here.

| Arm | Samples | Committed the plan | Thinking before the first call |
|---|---|---|---|
| `asis` (reasoning dropped, as the router sends it today) | 6 | **1** | 48-233 s, 5,357-33,315 reasoning chars |
| `last1` (the last turn's reasoning restored as `reasoning_content`) | 6 | **5** | 1.2-3.2 s, 131-431 chars |

- **Commit rate.** 5/6 vs 1/6: one-sided Fisher p ≈ 0.04, a small sample. The one `last1` DEFER was writing the
  plan body to `/tmp` with a heredoc, arguably the same intent.
- **Thinking time.** This effect is the large one: the two arms do not overlap at all (Mann-Whitney one-sided
  p ≈ 0.001 at n=6 each).
  - With its own last reasoning restored, the model does not re-derive its decision. It thinks for about 2 s and
    acts.
  - With the reasoning dropped, it spends minutes re-deriving, and usually ends up checking something again instead
    of acting.

  That is the mechanism of bug #2, measured.
- **What it does not test.** Neither the stopgap nor llama.cpp's native converter sends only the last block. Both
  send every block, and the template renders all of them under `preserve_thinking`'s default. The `all` and
  `preserve_thinking=false` arms (§12, adaptation 2) are still needed before replay is turned on, both for the plan
  outcome and for context size.

---

## 13. Decisions for the owner

- **D1. Adopt B (native-first)** and the mutation policy (§8.1) as the router's stated contract, in an ADR in
  `docs/decisions/`.
- **D2. The default for unknown fields on native paths:** forward. The alternative is reject-unless-allowlisted per
  wire. Recommend forward: the upstream validates, and the router keeps only its invariant guards (§7.2).
- **D3. The response `model` field on native paths:**
  - **verbatim** (the served name; honest; recommended for agents);
  - or masked to the requested alias, a declared `model-alias` response mutation (keeps chat UIs' labels stable).
- **D4. `translate: never` on the dev-worker agent routes** (predictability over availability), or `allow` with
  qualified translators.
- **D5. Mid-conversation system messages:** a cloudlab template change (recommended) vs keeping `system-merge`.
- **D6. `preserve_thinking` and compaction per local model** (§12), decided with the probe's `all` and
  `preserve_thinking=false` arms.

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

**Not verified here.**
- llm-d EPP body rewrites;
- vSR system-prompt injection on the live docs;
- Portkey's mid-stream fallback;
- LiteLLM pass-through byte fidelity;
- a vLLM server-wide `chat_template_kwargs` default flag in 0.28;
- the Qwen3.8 card wording.



