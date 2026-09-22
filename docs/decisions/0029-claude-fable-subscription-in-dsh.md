# ADR 0029 — Claude Fable 5.1 in dsh, on the metered key (the Max subscription is not permitted)

**Status:** SUPERSEDED by ADR 0031 (2026-09-22) — the route was removed; the findings below remain the record. Original status:
**Status:** ACCEPTED (2026-09-21), operator-directed: *"Add support in dsh for claude fable model
(chifor@gmail.com) subscription"*. **The subscription half of that request cannot be honoured** —
see decision 1 — so Fable is served on the estate's existing metered Anthropic key instead.
Design, measurements and the abandoned subscription design in
`plans/2026-09-21-dsh-claude-fable-subscription-plan.md`.
**Relates to:** ADR 0026 (the ChatGPT subscription through LiteLLM — the sibling this was modelled
on, and the reason the subscription design looked routine), ADR 0025 (the reviewer-1 Claude seats —
whose account this would have spent, and which a ban would have taken with it), ADR 0027
(`chatgpt_chat.py`, the estate's custom-provider precedent), ADR 0022 (model registration single
source).

## Context

dsh (`docs/runbooks/dsh.md`) reaches every model through LiteLLM and nothing else — its
NetworkPolicy allows the gateway and its own comment states the invariant, *"LiteLLM is the ONLY
model path … no OpenAI, no Anthropic, no DeepSeek"* (`networkpolicy.yaml:63`). The estate already
serves Claude through that gateway on a **metered** key (`claude-sonnet-4-6`, `claude-sonnet-5`,
`os.environ/ANTHROPIC_API_KEY`, `litellm.yaml:565-568`). The operator asked for Fable on the
**Max subscription** instead, by analogy with ADR 0026, which does exactly that for a ChatGPT
subscription.

The subscription design was built and then abandoned. It is recorded here in full because it was
technically sound, took a day to get right, and will look like an obvious idea again.

### The blocker

**Anthropic's Consumer Terms prohibit using a Free/Pro/Max OAuth token in any other product, tool
or service.** The terms were updated 2026-02-20 and enforcement began 2026-04-04; the stated
alternative is pay-as-you-go API billing. dsh is such a third-party tool, so routing it to
chifor@gmail.com's Max subscription is not permitted however the plumbing is arranged.

Two things make this worse than a licensing footnote:

* **Making it work would require impersonating Claude Code.** OAuth-token inference is gated on
  Claude Code client identity; a harness that wants to use a subscription token has to inject
  `"You are Claude Code, Anthropic's official CLI for Claude."` as the leading system message and
  the `claude-code-20250219` beta. That is circumvention, not configuration, and the policy names
  account bans for evasion.
* **The blast radius is not dsh.** chifor@gmail.com is reviewer-1 claude seat `a` (ADR 0025). An
  enforcement action against it takes the claude review persona's top-tier seat with it.

### What was measured anyway, and is worth keeping

All measured 2026-09-21 against the pinned `ghcr.io/berriai/litellm@sha256:d295634e…` (1.101.0), the
live dsh pod and the live TSDB. Findings 1-4 are the abandoned design's autopsy; 5-8 apply to the
route that shipped.

1. **LiteLLM speaks Anthropic subscription OAuth natively.** `optionally_handle_anthropic_oauth()`
   (`llms/anthropic/common_utils.py:102`): an `api_key` beginning `sk-ant-oat` drops `x-api-key`,
   sets `Authorization: Bearer` and merges `anthropic-beta: oauth-2025-04-20`. Proven by calling
   `AnthropicConfig().validate_environment()` in the pod with a fake key — no real credential, no
   quota. *This is the trap: the mechanism is right there, works first time, and is not allowed.*
2. **A rotating credential cannot be delivered by env var.** Two independent causes: Kubernetes
   never updates env vars from a Secret after container start (only mounted volumes), and
   `ProxyConfig._check_for_os_environ_vars` (`proxy/proxy_server.py:4763-4795`) resolves
   `os.environ/` once at config load. `litellm.yaml:1010-1012` already says the same from the other
   side. The ChatGPT sibling escapes this only because `litellm/llms/chatgpt/` ships its own
   Authenticator that re-reads a file per request; `litellm/llms/anthropic/` contains **no file
   reads at all**.
3. **Rolling the gateway to pick up a rotated token is self-defeating.** `litellm.yaml:997-998`:
   `max_budget` is tracked **in-memory per replica** and "resets on any restart". Rolling ~3×/day
   would convert the shared $50/30d third-party ceiling into a $50/~8h one, and there is no
   Reloader in-cluster (`:1010-1012`), no `terminationGracePeriodSeconds` (30 s default against a
   900 s `request_timeout`), and `STORE_MODEL_IN_DB=False` (`:1109`) rules out a runtime model
   store.
4. **The working shape, had it been permitted,** was a ~30-line `CustomLogger` whose
   `async_pre_call_hook` injects `data["api_key"]` from a mounted Secret file for one model id.
   `/v1/messages` runs `pre_call_hook` (`common_request_processing.py:2014`) **before**
   `route_request` (`:2373`) and replaces `data` with its return, and the Router merges request
   kwargs over the deployment's params. Exercised against the pinned image: other routes untouched,
   missing/empty/wrong-prefix tokens all fail closed with 503, and a token replaced on disk is
   picked up with **no restart**. A custom *provider* would not have worked:
   `get_provider_anthropic_messages_config` (`utils.py:8445-8492`) admits only `ANTHROPIC`,
   `AZURE_AI`, `BEDROCK`, `VERTEX_AI` to the native path, and anything else is downgraded to
   chat-completions — fatal for a model whose thinking blocks must replay unchanged.
5. **`/v1/messages` exists on this image but is LAZILY REGISTERED.** It is absent from
   `app.routes`; the endpoint (`proxy/anthropic_endpoints/endpoints.py`) mounts on first matching
   request via `proxy/_lazy_features.py:196`. **Probe it, don't enumerate it:** an unauthenticated
   POST returns 401 where a nonexistent path returns 404. That mistake was made once here already.
6. **`baseURL` must NOT carry `/v1` for an `anthropic-messages` provider.** dsh's adapter hands it
   to the official `@anthropic-ai/sdk` (0.123.0 in the pod), which appends `/v1/messages` itself.
   The `/v1` form both Codex provider blocks use would yield `/v1/v1/messages`.
7. **Thinking blocks are silently discarded unless the adaptive branch is forced.** Without
   `compat.forceAdaptiveThinking`, dsh's adapter sends legacy
   `thinking: {type: enabled, budget_tokens, display}`; LiteLLM rewrites that to `{type: adaptive}`
   + `output_config.effort` and **drops `display`**, so Fable defaults to `omitted`, every returned
   thinking block has empty text, and LiteLLM's
   `strip_empty_content_blocks_from_anthropic_messages` deletes those blocks on the next replay —
   signature or not. A declared `off` reasoning row is separately fatal: it makes the adapter send
   `thinking: {type: disabled}`, which Fable 400s.
8. **Fable is the most expensive model on this gateway.** $10/$50 per MTok; one 100K-in/10K-out
   turn prices at exactly $1.50, and dsh resends conversation state every turn. `claude-sonnet-5`
   is $2/$10 by comparison.

## Decision

1. **Do not use the Max subscription; serve Fable on the existing metered `ANTHROPIC_API_KEY`.**
   This is a compliance decision, not a technical one — the subscription path was built, measured
   and proven to work. Do not "fix" this route onto a subscription token later without revisiting
   the Consumer Terms, and do not add the Claude Code identity header that would make it succeed.
2. **The route is the stock `anthropic/` provider on `/v1/messages`,** so the conversation stays
   Anthropic-native end to end and Fable's thinking blocks replay unchanged (finding 4's provider
   note explains why a custom provider cannot be substituted).
3. **dsh gets one new provider, `anthropic-fable`,** with `api: anthropic-messages`, a `baseURL`
   without `/v1` (finding 6), `compat.forceAdaptiveThinking: true` and no `off` reasoning row
   (finding 7). It is reconciled per boot like the Codex providers, because `settings.yaml` lives
   on the PVC and a seed-only addition never reaches it.
4. **The route keeps its real price.** No zero-cost override: this key is metered and the spend is
   real, so it must be counted. (The earlier subscription design priced the route at zero, which
   was correct *for a subscription* and would be a lie here.)
5. **`rpm: 4` is a concurrency bound, not a spend cap.** With `enable_pre_call_checks` unset on
   this proxy — deliberately (`litellm.yaml:834`, `:296`) — LiteLLM turns `rpm` into
   `max_parallel_requests`. It limits a runaway parallel fan-out and bounds nothing per minute.
   Naming it a rate limit in a later edit would be a false claim about a route that bills per token.

## Consequences

* **dsh can spend real money now, which no other route in its picker could.** Finding 8 is the
  number to hold: ~33 turns is $50. The controls are `rpm: 4` (concurrency only), the global
  `max_budget`, and reading the bill. If Fable use becomes routine rather than occasional, a
  per-consumer budget is the next step and it needs the DB this deployment deliberately does not
  have (`STORE_MODEL_IN_DB=False`).
* **The reviewer's Fable window is untouched.** An unintended benefit of the compliance outcome:
  dsh no longer shares seat `a`'s weekly quota, so the `fable → opus → sonnet` ladder cannot be
  starved by interactive dsh use — the risk the subscription design spent most of its effort
  bounding.
* **No new credential, publisher, vault path, ESO chain or callback.** The subscription design
  needed all five; the compliant one needs a `model_list` entry, a provider block and a reconcile
  line. `af/litellm/*` gains nothing.
* **A known follow-up, unrelated to this route but found here:** the global `max_budget`
  accumulator appears not to fire for streaming or chat responses in 1.101.0, which would make the
  $50 guard weaker than `litellm.yaml:851-853` claims for *every* metered route. Not fixed here;
  worth its own investigation before the gateway goes public.
* **`gen-litellm-consumers.py` correctly ignores this route** (no `api_base` ⇒ not
  `dsh_visible`), verified by the drift check. Do **not** set `model_info.dsh_only: true` on it:
  that would also list `claude-fable-5-1` in dsh's `openai-completions` provider, offering one
  model on two protocols — the second being the chat-completions path decision 2 exists to avoid.
