# ADR 0039 — dsh takes every model from llm-router, on one all-routes key

**Status:** ACCEPTED (2026-10-19), owner-directed: *"update the dsh https://dsh.chifor.me/
implementation, remove the existing models and generate a key on llm-router allowing all models
supported there. Show the models in dsh models list so they can be selected."*

**Supersedes** ADR 0031's decision ("dsh serves no Claude model, by any path"); its record of the
two withdrawn Claude designs stays accurate. **Retires dsh's use of** ADR 0026's second Codex
subscription route and of the native `openai-codex` provider (seat b). **Relates to:** ADR 0022
(LiteLLM model registration — dsh stops being one of its consumers). Runbook:
`docs/runbooks/dsh.md`, "Models: llm-router".

## Context

Before this change the dsh model picker offered four model sources:

| Picker heading | Provider | Path |
|---|---|---|
| DeepSeek | `deepseek-official` | the shipped `dsh-llm-deepseek` composition row; no `DEEPSEEK_API_KEY` exists in the estate, so every model failed |
| OpenAI Codex (realjaysage) | `openai-codex` | native Codex subscription, reviewer-2 seat b, token projected into `af/dsh/credentials` |
| OpenAI Codex (realjaynesage) | `openai-codex-realjaynesage` | seat d through LiteLLM's `chatgpt/` provider |
| litellm | `litellm` | LiteLLM's self-hosted Qwen routes, with the LiteLLM MASTER key in the pod's env |

llm-router (`router.chifor.me`) already serves all of those families — five Codex subscriptions in
a quota-aware smart pool, five Claude subscriptions, and the self-hosted Qwen servers — to the dev
workers, the reviewer bots and Relay.

## Decision

1. **One router key, all routes.** `key_353539972cec`, `dsh all routes (ailab dsh.chifor.me, ns
   dsh)`, `routes: all`, 365 days (expires 2027-10-19), created with the admin API. Its only copy is
   the field `LLM_ROUTER_API_KEY` of `af/dsh/credentials`, written with a CAS-guarded KV-v2 PATCH
   (document version 11 → 12, every other field preserved). The existing discovery ExternalSecret
   projects it and `openbao-credentials.mjs` resolves `apiKeyEnv: LLM_ROUTER_API_KEY` per request;
   no new ExternalSecret, env var or OpenBao policy.
2. **Three providers, by protocol.** The router serves Codex on both `/v1/responses` and
   `/v1/chat/completions`, but Claude and Qwen only on chat completions (`response.failed` on
   Responses, measured). So:
   * `llm-router-codex`: `openai-responses`, header `X-Router-Route: pool-codex` (model mode on the
     smart pool rather than the direct policy's fixed priority order), `gpt-6-astra`, `gpt-6-sol`,
     `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`. It carries the two workarounds the
     LiteLLM Codex route needed, because both defects reproduce through the router:
     `compat.supportsStrictMode: true` (else optional tool properties are filled in) and an effort map
     with no `off` row (else `reasoning.effort: "none"` → HTTP 400).
   * `llm-router-claude`: `openai-completions`, `opus`, `sonnet`, `haiku`, `fable`, by model name.
   * `llm-router-local`: `openai-completions`, the seven chat models the key lists.
   The two image-generation models the key also lists are not offered: they are not chat models.
   Image input is declared only on `gpt-6-luna`, the one model whose router accounts accept images.
3. **The old sources are removed, not hidden.** `deployment.yaml` unwrites the `litellm`,
   `openai-codex` and `openai-codex-realjaynesage` blocks from the PVC's `settings.yaml` on every
   boot (safe to keep indefinitely), `cordis.patch.yml` disables the `llm-deepseek` row, and a saved
   `agent-default-model` that still names any of the four is repointed to the new default
   (`llm-router-local` / `qwen3.8-flash-next`, the same model the old default named) by
   `reconcile-default-model.js`. A default the user picks from the new providers is never touched.
4. **The Conductor follows.** Its role routes become `llm-router-codex/gpt-6-astra` (conductor),
   `llm-router-local/qwen3.8-27b` (worker, the same cloud2 vLLM server the old
   `qwen3.8-27b-vllm-cloud` route reached) and `llm-router-local/qwen3.8-flash-next` (reviewer),
   with the admission bounds carried over unchanged; the team-conductor composition routes its
   spawned workers to `llm-router-local/qwen3.8-27b`.
5. **The LiteLLM master key leaves the pod.** `LITELLM_API_KEY`, the `dsh-litellm` ExternalSecret and
   the NetworkPolicy rule to LiteLLM are removed; the model path is the existing 443 egress rule to
   `router.chifor.me`. The router's own NetworkPolicy admits only its edge, and the edge only
   cloudflared, so the public hostname is the documented client path and the one that is held
   across router rollouts.
6. **`gen-litellm-consumers.py` stops writing dsh's model list** (its span 2 and the `dsh_only` /
   vision machinery that existed only for it). LiteLLM's own config is untouched, so its checksum
   and pod do not change.
7. **Claude is offered again.** ADR 0031 removed Claude because both designs failed (a rejected
   metered key, an unauthenticated CLI) and because the operator then wanted none; the operator now
   asks for every model the router supports. dsh holds no Claude credential: the router's Claude
   provider runs the Claude Code CLI on the subscriptions' own tokens — ADR 0030's carve-out ("the
   unmodified binary on the owner's own token") — exactly as it already does for the reviewer bots
   and Relay. ADR 0029's compliance finding concerned a subscription token inside a third-party
   adapter, which this does not do; whether harness traffic on those subscriptions is acceptable
   remains the owner's call, made with this request.

## Verification (2026-10-19)

* The key: `GET /v1/models` lists 17 models (15 chat, 2 image) and `GET /v1/routes` 17 routes.
* From the dsh pod over its real egress: a tool-calling chat-completions turn succeeded on Codex,
  Claude and Qwen. Through pi-ai, the client dsh uses, each provider block was resolved with the
  adapter's own strict `resolveProfiles`, and one streamed tool-calling turn was run per listed
  model. Every one succeeded except `qwen3.5-122b`, which hit the router's 300 s pre-output wait on
  a cold start. The other cloudlab-hosted Qwen models answered in 45–200 s, because the server at
  192.168.0.26 loads models on demand. The Codex call returned `{command}` only, so the strict-mode
  workaround holds.

## Consequences

* **One credential for everything.** The key can spend every Codex and Claude subscription's
  window. It sits in a pod that runs model-authored code with cluster-admin (ADR 0025), so the key
  adds no reach the pod lacked: cluster-admin can already read the router's admin token. Revoke at
  the issuer: `DELETE /admin/v1/keys/key_353539972cec`.
* **dsh shares the subscriptions' capacity** with the dev workers and the codex reviewer
  (`pool-codex`), and with the Claude reviewer and Relay (the Claude accounts). Smart placement
  spreads that load; it does not create capacity.
* **Model lists are hand-maintained.** A model added on the router reaches dsh only when a row is
  added to `settings.seed.yaml`; nothing generates or checks it against `/v1/models`.
* **Lost relative to LiteLLM:** image input on the Qwen models. LiteLLM declared vision on
  `qwen3.8-27b`, `qwen3.8-flash-next` and `qwen3.8-27b-ailab`; the router reports `vision: false`
  for every local account and refuses images there. Restoring it is a router account setting.

## Follow-ups (not done here)

* Narrow policy `af-app-dsh` (owned by `security/openbao/dsh-provision-job.yaml`) to drop the now
  unused `af/estate/litellm` read grant. Its drift guard accepts only reviewed forms, so this is its
  own change.
* Retire reviewer-2 seat b's projection into `af/dsh/credentials` (`DSH_CODEX_*`, the
  `dsh_codex_publisher` config and its `CodexProjection*` alerts for that document), and decide
  whether LiteLLM's `gpt-6-astra-realjaynesage` route (seat d) still has a consumer.

## Rollback

Revert the PR. The old reconcile lines re-insert the `litellm`, `openai-codex` and
`openai-codex-realjaynesage` blocks on the next boot, the DeepSeek row returns, the LiteLLM key comes
back through its ExternalSecret, and the Conductor returns to its old routes. The `llm-router-*`
blocks would then linger on the PVC copy until unwritten (`DSH_PROVIDER=<name> DSH_PROVIDER_REMOVE=1`
for one boot). Revoke the router key only after that.
