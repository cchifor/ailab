# App-model recovery: Knowledge grant (platform) + `gpt-5.6-sol` on the Codex subscription (ailab)

## Context

The Codex agent activating the Strive App-model feature on dev-worker-3 filed
`/workspace/c4/app-model-deploy-artifacts/recovery-ailab-operations-request.md` (2026-09-20 16:06)
asking the ailab operator for two bounded GitOps changes, with the feature flag
`APP__AIRLOCK__APP_MODEL_POLICY_ENABLED=false` held until both acceptance checks pass:

1. **platform** (`cchifor/platform`): add `knowledge:write` to `svc-airlock -> svc-knowledge` in
   `deploy/secrets/ailab/gatekeeper-secrets.enc.yaml`, bump `gatekeeper.serviceRegistry.checksum`
   in `deploy/helm/values/providers/ailab.yaml`, merge, roll both Gatekeeper replicas. Acceptance:
   a request authenticated as `svc-airlock` to
   `GET http://knowledge.strive-ailab.svc.cluster.local:5010/api/v1/internal/embedding-profiles/bge-m3-local-v1/availability`
   returns 200 with `available=true` (the last attempt was 403 `scope_required`).
2. **ailab**: `gpt-5.6-sol` through the `ai/litellm` proxy answers 401 (`OpenAI AuthenticationError /
   Incorrect API key` — the `OPENAI_API_KEY` in `litellm-cloud-keys` is rejected by api.openai.com).
   Requested: repair the key. **Operator's directive instead (2026-09-20):** keep the API-key path
   available, but *for now* serve `gpt-5.6-sol` from the existing LiteLLM Codex subscription
   (`chatgpt/` provider, reviewer-2 seat d `realjaynesage`, ADR 0026 — the route dsh's second Codex
   provider uses). No replacement API key is being supplied. Acceptance: an authenticated
   `POST /v1/chat/completions` for `gpt-5.6-sol` returns a real non-empty completion.

Facts established live on 2026-09-20 (from inside `ai/litellm-866ff7c6bf-k52sn`, pinned image
LiteLLM 1.101.0, one probe request each; no secret left the pod):

- Live `strive-ailab/gatekeeper-secrets.service-registry` == the git plaintext (leaf sha256 `9c610bfd…`,
  4207 B): `svc-airlock -> svc-knowledge` is `['knowledge:read']` only. No out-of-band repair to
  reconcile; the runbook's plain path applies.
- **`chatgpt/gpt-5.6-sol` is accepted by chatgpt.com/backend-api/codex**: streaming chat-completions
  returned `{"answer":"OK","n":7}` in 2.4 s, model id echoed `gpt-5.6-sol`, usage present.
- **Non-streaming chat-completions through `chatgpt/` 500s** (`Unknown items in responses API
  response: []`): the bridge's empty-output recovery in `completion_extras/litellm_responses_transformation`
  does not recover this SSE shape. The platform's consumer
  (`services/workflow/src/worker/document_fields/model.py`) is **non-streaming** and validates
  `usage.prompt_tokens`/`completion_tokens` (ints), `choices[0].finish_reason == "stop"`,
  `message.role == "assistant"`, `message.content` parseable JSON.
- The provider's Responses allow-list (`llms/chatgpt/responses/transformation.py`) drops `text` and
  `max_output_tokens`. Measured with a widened list: **`text.format` (strict json_schema) is accepted**
  by the backend and enforced (`{"answer":"OK","n":7}` vs the free-text `answer='OK', n=7` without it);
  **`max_output_tokens` is rejected** (`400 {"detail":"Unsupported parameter: max_output_tokens"}`),
  so upstream is right to drop that one. The platform sends `response_format` (strict json_schema),
  `max_completion_tokens: 32000`, `reasoning_effort: "medium"`.
- `litellm.stream_chunk_builder` over the provider's stream (with `stream_options.include_usage`)
  yields a `ModelResponse` with `finish_reason: stop`, `role: assistant`, `usage` 26/55/81 — the
  shape the platform validates.
- The real SSE stream for one gpt-5.6-sol answer is 17 events: `response.created`,
  `response.in_progress`, `response.output_item.added`, `response.content_part.added`,
  `response.output_text.delta`×9, `response.output_text.done`, `response.content_part.done`,
  `response.output_item.done` (the message, `status: completed`), `response.completed` with
  **`output: []`** and the usage block. The request's session id is echoed as `prompt_cache_key`.
- A draft of the handler below, exercised in-process through a real `Router` in the pod: the
  platform-shaped non-streaming call returned `{"answer":"OK","n":7}`, `finish_reason stop`,
  `role assistant`, usage 55/17/72 in 1.9 s with `text` (the schema) and `reasoning` on the wire and
  no `max_output_tokens`; streaming returned 10 chunks; a direct `chatgpt/` call outside the handler
  and the `gpt-6-astra-realjaynesage` Responses call both sent the unchanged key set
  (`include,input,instructions,model,store,stream`) — the scoping holds. **A custom provider's
  params are mapped through `OpenAILikeChatConfig`, whose list has no `reasoning_effort`, so
  `drop_params: true` silently drops it before the handler runs; `litellm_params.allowed_openai_params:
  [reasoning_effort]` on the deployment carries it through** (verified: it reaches the handler and the
  wire as `reasoning`). The transform runs during `await litellm.acompletion(...)`, before the stream
  is iterated (the ContextVar was already reset when the wire body with `text` was recorded).
- **Side finding (not in scope, must be reported):** `chatgpt/gpt-6-astra` with the same token and
  headers now returns **403 with a Cloudflare JS-challenge page** ("Enable JavaScript and cookies to
  continue") in 0.1–0.4 s, streaming and non-streaming alike, while `gpt-5.6-sol` passes, and the
  Codex CLI on reviewer-2 (seat c) reaches `gpt-6-astra` fine — the challenge keys on LiteLLM's HTTP
  client fingerprint for that model, not on the account. The `gpt-6-astra-realjaynesage` route (and
  dsh's `openai-codex-realjaynesage` provider) verified on 2026-09-19 is therefore currently broken by
  an upstream edge rule, not by anything in git. Also seen today: the six dev-workers' shared Codex
  projection (seat a) is `token_revoked`, and the workstation seat is usage-capped until 2026-09-23.

## Approach

### Part A — platform grant (the #1429 recipe, `platform-ailab-secrets-key-never-in-ci`)

Working clone: `$CLAUDE_JOB_DIR/tmp/platform` (autocrlf=false), branch
`ops/app-model-knowledge-write-grant` off `origin/main` (`d58fb6d2a`).

1. In WSL (sops 3.13.1 + pyyaml), `SOPS_AGE_KEY_FILE=/mnt/c/Users/chifo/work/home/ailab/kubernetes/infra/_out/age.agekey`,
   run `EDITOR="python3 <shim>" sops edit deploy/secrets/ailab/gatekeeper-secrets.enc.yaml`. The shim
   imports `scripts/grant-app-model-profile.py::add_knowledge_write` (the reviewed, idempotent
   transformation shipped in #1432 — it refuses a missing `svc-knowledge` audience or missing
   `knowledge:read`) and rewrites ONLY the `stringData.service-registry` leaf. `sops edit` reuses the
   data key, so exactly one leaf re-encrypts and the ciphertext diff stays reviewable.
2. Verify without printing values: sha256 per `stringData` leaf before/after → exactly one changed;
   the **entire decoded registry** after == a deep copy of the registry before with exactly that one
   scope appended (whole-document equality, so a change to any other service inside the same leaf
   fails the check), then print the svc-airlock audiences as the human-readable summary. Repeat the
   comparison against the **forge's blobs** (`origin/main` vs the pushed branch) and post it on the PR
   (every service entry, every `secret_hash`, every other leaf identical).
3. `sha256sum deploy/secrets/ailab/gatekeeper-secrets.enc.yaml` → `gatekeeper.serviceRegistry.checksum`
   in `deploy/helm/values/providers/ailab.yaml`; run `deploy/helm/scripts/check-service-registry-checksum.sh`
   (also enforced by platform CI: `ci.yml` job `helm-netpol`, step "Gatekeeper service-registry
   checksum rolls on a grant change").
4. Commit only the two files; push `origin` (= git.chifor.me); open the PR with the `chifor` PAT from
   dev-worker-3's credential store (the cached Windows `cchifor` credential 403s on the REST API).
   PR body: the verifiable claims (one leaf changed, decrypted-structure diff, checksum derivation).
5. Merge after required checks (merge commit, `Do: merge`); annotate GitRepository `platform` +
   Kustomization `platform-app` with `reconcile.fluxcd.io/requestedAt`; wait for
   `strive-ailab/gatekeeper` rollout (2/2, pod-template annotation `checksum/service-registry` == new
   sha, `restartedAt` untouched).

### Part B — ailab: serve `gpt-5.6-sol` from the subscription through a custom provider handler

Why not a plain second `chatgpt/gpt-5.6-sol` deployment: the consumer is non-streaming (500 today)
and needs its JSON schema honoured (dropped today). Why not a platform change: the request is for
the gateway side, the adapter's `protocol: chat_completions` is a schema literal across three
services, and the operator's ask is a gateway-side switch. Why not a LiteLLM pin bump: the
allow-list is upstream policy, not a bug; the empty-output recovery failure is, but a gateway
upgrade for one route is disproportionate and unproven. LiteLLM's documented extension point is
`litellm_settings.custom_provider_map` (a `CustomLLM` subclass loaded by `get_instance_fn`, which
first looks for `<config dir>/<module>.py` beside the config file).

Files (all under `kubernetes/apps/apps/ai/` unless stated):

- **`chatgpt_chat.py`** (new, standalone; stdlib + litellm only; draft already exercised live, see
  Context). Registered as provider `chatgpt-chat`. Behaviour:
  - `acompletion(model, messages, optional_params, timeout, …)`: one inner
    `litellm.acompletion(model="chatgpt/<model>", messages, stream=True, stream_options={"include_usage": True}, num_retries=0, "no-log": True, **optional_params)`
    with `stream`, `stream_options`, `no-log` and the Router-injected `max_retries` stripped from the
    forwarded params. **Inner retries are disabled explicitly** (`num_retries=0` on the inner call —
    the outer route's `num_retries: 0` does not reach a nested `litellm.acompletion`). The stream is
    drained and rebuilt with `litellm.stream_chunk_builder(chunks, messages=messages)`; the built
    response's `model` is set to the bare model id; a stream that yields no choices raises
    `CustomLLMError(502)`, never a fabricated completion. `no-log` keeps the proxy's success/spend
    logging on the OUTER call only (one record per request, tagged with the outer model name).
  - **One bounded deadline, cancellation, cleanup.** The inner call *and* the drain run under
    `asyncio.timeout(timeout)` (the Router passes the proxy's `request_timeout: 900`); the upstream
    stream is closed in a `finally` (`aclose()` on the wrapper's underlying iterator when present) so a
    client disconnect (`CancelledError`) or the deadline stops the upstream read instead of letting it
    run to completion against the subscription. The aggregated body is capped at 4 MiB of content
    (the platform's own `MAX_RESPONSE_BYTES`); past it the drain aborts with `CustomLLMError(502)`.
    These bounds cap memory per request on the shared 6 GiB proxy; they cannot reproduce the upstream
    `max_output_tokens` the backend refuses, and the plan records that as an accepted loss.
  - `astreaming`: the same inner call; every `ModelResponseStream` chunk is **re-yielded unchanged**
    (the proxy's `CustomStreamWrapper` accepts `ModelResponseStream` from a custom provider as-is —
    `streaming_handler.py`, the `_custom_providers` branch), so role-only, usage-only, empty-choices,
    tool-call and refusal deltas all pass through exactly as the `chatgpt/` route emits them. Only the
    chunk's `model` is rewritten to the bare id.
  - Sync `completion`/`streaming`: `CustomLLMError(501)` with a message — the proxy only calls the
    async pair.
  - **Scoped allow-list widening.** A module-level `contextvars.ContextVar` is set (`token = var.set(True)`)
    around the inner `await litellm.acompletion(...)` and reset in `finally`; the module wraps
    `ChatGPTResponsesAPIConfig.transform_responses_api_request` so that, when the var is set, `text`
    (the caller's `response_format`, already mapped by the chat→Responses bridge into
    `response_api_optional_request_params["text"]`) is re-added to the request. The transform runs
    inside that `await`, before the stream is iterated (measured), so the scope covers it.
    `max_output_tokens` stays dropped (backend 400). Outside the var — every `chatgpt/` call that does
    not come through this handler, i.e. `gpt-6-astra-realjaynesage` — the request is byte-identical to
    today's.
  - Import-time guards: the upstream class, method and its keyword signature are asserted at import;
    a pin bump that moves them raises `ImportError` and the proxy refuses to start. The image-level
    test runs in the pinned image in CI, so the failure surfaces at PR time for any pin bump that
    touches `litellm.yaml`.
- **Where the handler lives, and the rollback story.** The handler is merged INTO `litellm-config`
  (`kustomization.yaml`: `configMapGenerator` `- name: litellm-config, namespace: ai, behavior: merge,
  options: {disableNameSuffixHash: true}, files: [chatgpt_chat.py]` — rendered with
  `kubectl kustomize`: the ConfigMap keeps its name and namespace, gains the `chatgpt_chat.py` key,
  and the Deployment's `config` volume reference is untouched). So `/etc/litellm/chatgpt_chat.py`
  sits beside `config.yaml`, `get_instance_fn` loads it from the config directory, no `PYTHONPATH`,
  no second volume — and, decisively, **an old pod that restarts during a failed rollout reads
  `config.yaml` and the handler from the same in-place ConfigMap**, so it cannot see a
  `custom_provider_map` entry without the module it names (the hazard of a separate generated
  ConfigMap that only the new pod template mounts). Rollback is one operation: `git revert` the merge
  → Flux applies the previous ConfigMap (routes, `custom_provider_map`, the handler key removed) and
  the previous pod template → the annotation moves back → pods roll to the previous config. Nothing
  else to clean.
- **`litellm.yaml`**:
  - `litellm_settings.custom_provider_map: [{provider: chatgpt-chat, custom_handler: chatgpt_chat.handler}]`.
  - `model_list`: `gpt-5.6-sol` → `chatgpt-chat/gpt-5.6-sol`, `num_retries: 0`,
    `allowed_openai_params: [reasoning_effort]` (see Context: without it the caller's effort is
    silently dropped at the custom-provider param mapping); `gpt-5.6-sol-api` → `openai/gpt-5.6-sol`,
    `api_key: os.environ/OPENAI_API_KEY`, `num_retries: 0` (the retained paid path). No `fallbacks`
    between them: a fallback to a paid key contradicts the route's "never replay paid generation"
    policy, and the key is dead anyway. Neither has an `api_base`, so neither enters the generated
    pickers (Open WebUI Local, dsh); both stay discoverable under External, as today.
  - **Switch-back (recorded in the ADR, not a rename):** `gpt-5.6-sol-api` keeps its contract
    (OpenAI, the API key) permanently. When a replacement key lands: (1) update
    `litellm-cloud-keys.sops.yaml`, roll, and prove `gpt-5.6-sol-api` answers 200 upstream; (2) point
    `gpt-5.6-sol` back at `openai/gpt-5.6-sol` + `os.environ/OPENAI_API_KEY` + `num_retries: 0` (the
    original contract `test_litellm_platform_routes.py` pinned until this change); (3) once no route
    uses `chatgpt-chat/`, remove the handler key, the generator entry and `custom_provider_map`, and
    retire ADR 0027's temporary status. The contract test carries both admissible shapes for
    `gpt-5.6-sol` so step (2) is a config change, not a test rewrite.
  - Pod-template annotation `checksum/chatgpt-chat: "<12 hex>"` = sha256 of `chatgpt_chat.py`
    (file bytes), the roll trigger for a handler-only edit — the same discipline as `checksum/config`,
    gated the same way: a new `check_litellm_chatgpt_chat_checksum()` site in
    `scripts/check-inline-hashes.py` (already run by the `manifests` workflow) fails the build when
    the annotation and the file disagree. `checksum/config` itself is bumped by
    `python scripts/gen-litellm-consumers.py --write` (it records the routes and `custom_provider_map`).
  - Comment record on the route: the measurements above; the accepted loss (`max_completion_tokens`
    is dropped at the provider; the platform's post-hoc `usage.completion_tokens <= 32000` check
    bounds what it *accepts*, not what is generated — the backend's own output ceiling for
    `gpt-5.6-sol` is the only generation bound, and the 4 MiB drain cap is the only memory bound);
    the **input limit**: `model_info.max_input_tokens` is set from a one-off live probe at ~130 K
    tokens (`enable_pre_call_checks` is off in this estate, so the value documents rather than
    enforces — the platform's `input_token_cap` (33 025–512 000, its knob) is the pre-generation
    gate, and an oversized request fails upstream with a 400 that the adapter maps to
    `model_unavailable`, one attempt, no retry); the **quota policy**: this route spends seat d's
    weekly window, shared with dsh's `openai-codex-realjaynesage`; no gateway-side concurrency cap is
    added (the backend rate-limits; exhaustion surfaces as upstream 429 → `RateLimitError` → 429 to the
    caller → `model_unavailable`), and it is observed through the hourly usage probe
    (`reviewbot_llm_usage_percent{persona="codex",seat="d",limit="weekly_all"}`) and the seat alerts
    (`ReviewbotSeatExhausted`/`ReviewbotAllSeatsExhausted`, ADR 0024/0026) — the Grafana reviewbot
    dashboard's seat-d panel is where a document-table burst shows; External exposure of this name to
    any master-key/virtual-key holder is the same class as `gpt-6-astra-realjaynesage` today
    (accepted in ADR 0026), restated here; the cost-map warning (no price for `chatgpt-chat/…`, the
    response is unaffected, `usage` is populated); and the Cloudflare-challenge exposure seen on
    `gpt-6-astra` the same day.
- **`scripts/tests/test_litellm_platform_routes.py`**: contract updated — exactly one `gpt-5.6-sol`
  route whose `litellm_params` is one of the two admissible shapes (`{model: chatgpt-chat/gpt-5.6-sol,
  num_retries: 0, allowed_openai_params: [reasoning_effort]}` now; `{model: openai/gpt-5.6-sol,
  api_key: os.environ/OPENAI_API_KEY, num_retries: 0}` after switch-back); exactly one
  `gpt-5.6-sol-api` route with the OpenAI shape; the `custom_provider_map` entry names
  `chatgpt_chat.handler` and the module compiles (`compile(source, …)`); the generator merges
  `chatgpt_chat.py` into `litellm-config` without a name hash; `checksum/chatgpt-chat` is present;
  **the existing no-replay assertions are kept verbatim and applied to both names**:
  `default_fallbacks` falsy, and no rule in `fallbacks`, `context_window_fallbacks` or
  `content_policy_fallbacks` whose key is `gpt-5.6-sol`, `gpt-5.6-sol-api` or `*`.
- **`scripts/tests/integration/test_litellm_platform_route_contract.py`** (existing, runs first in the
  workflow): today it asserts `gpt-5.6-sol` → `https://api.openai.com/v1/chat/completions` with the
  exact caller body preserved. Updated to exercise **`gpt-5.6-sol-api`** with those unchanged paid-path
  wire assertions (the contract moves with the alias), keeping its 400/429/5xx/timeout one-attempt
  cases.
- **`scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py`** (new, image-level,
  sockets denied, same harness as `test_litellm_chatgpt_route_contract.py`): loads the handler from
  the manifest path, registers the provider the way the proxy does (`custom_provider_map` +
  `custom_llm_setup()`), builds a Router from the manifest's **three** routes as written
  (`gpt-5.6-sol`, `gpt-5.6-sol-api`, `gpt-6-astra-realjaynesage`) with the manifest's
  `router_settings` and `litellm_settings.drop_params`, mocks `AsyncHTTPHandler.post` with an SSE
  fixture in chatgpt.com's **real** 17-event shape (Context) and asserts:
  (a) non-streaming `router.acompletion` with the platform's exact body returns `content ==
  '{"answer":"OK","n":7}'`, `finish_reason stop`, `role assistant`, **usage exactly the fixture's**
  (input 55 / output 17 incl. reasoning-token detail / total 72 — never fabricated zeros when the
  fixture omits usage: that case must fail the call), and the wire body carries `text.format` == the
  schema, `reasoning == {"effort": "medium"}`, `stream: true`, `store: false`, and **no**
  `max_output_tokens`; exactly one POST;
  (b) the same fixture through `gpt-6-astra-realjaynesage` (`router.aresponses`, dsh's request shape)
  sends the **complete** expected body from the existing test (`EXPECTED_BODY_KEYS` and values), not
  merely "no `text`"; and a wrapped + an unwrapped call run concurrently (`asyncio.gather`) each see
  their own body;
  (c) streaming yields the text deltas, the role-only first chunk, a final chunk with
  `finish_reason stop`, and the usage-only chunk;
  (d) upstream 401 / 429 / 503 → `AuthenticationError` / `RateLimitError` / `ServiceUnavailableError`
  in seconds, **one POST each** (no inner retry); a transport that stalls → `Timeout` at the
  handler's deadline, one POST, the upstream stream closed;
  (e) negative SSE: the observed **HTML 403** body → an error, not a completion; a stream **truncated
  at EOF** before `response.completed` → error, no partial-text `stop`; a `response.incomplete`
  terminal event → `finish_reason length`, not `stop`; a refusal output item → `message.refusal`
  set and `content` empty, so the consumer's `refusal is not None` guard trips;
  (f) the placeholder auth.json builds the Router with no socket attempt; a missing file still routes
  to the device flow (the existing guard is unchanged by the handler);
  (g) exactly one success-callback record per outer call (a `CustomLogger` registered for the case),
  carrying the outer model name, and none for the inner call.
  Wired into `.gitea/workflows/litellm-route-contract.yaml` (third `docker run`) with path triggers
  for `chatgpt_chat.py`, `kustomization.yaml`, `scripts/check-inline-hashes.py` and the new test.
- **Proxy startup smoke in the pinned image** (same workflow, fourth step): render the ConfigMap with
  `kubectl kustomize kubernetes/apps/apps/ai` (the runner has kubectl), extract `config.yaml` and
  `chatgpt_chat.py` into one directory, start `litellm --config <dir>/config.yaml --port 4010` inside
  `docker run --network none` with a placeholder `CHATGPT_TOKEN_DIR` auth file and dummy
  `LITELLM_MASTER_KEY`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, wait for `/health/liveliness`, then assert
  `/v1/models` lists `gpt-5.6-sol` and `gpt-5.6-sol-api` and the log line that registers
  `chatgpt-chat`. This is the deployed loader path (`get_instance_fn` from the config directory), not
  the in-process import. The non-streaming HTTP serialisation is covered in-process by (a) — the
  smoke proves loading and readiness only; a request here would try chatgpt.com and is not made.
- **`docs/decisions/0027-gpt-5.6-sol-on-the-codex-subscription.md`**: the operator's directive, the
  measurements, the handler design and its scoping, the switch-back procedure above (the API key is
  the intended steady state once replaced; the handler is temporary), the quota/edge-rule exposures,
  and the rollback story.

### Part C — acceptance (both run in-cluster, secrets never leave their pods)

- Platform: from inside an `airlock` pod, use **airlock's own `S2SClient` configuration**
  (`services/airlock/src/app/core/service_clients.py`: gatekeeper `/auth/token`, its mounted client
  credentials) to mint a token for `svc-knowledge` and call the availability URL; expect 200
  `{"available": true, …}`. Record status + body keys, not the token. **Token-cache bound:** gatekeeper
  mints S2S tokens with `internal_token_ttl_seconds = 300` and `S2SClient` refreshes 60 s before
  expiry, so a running airlock holds a pre-grant token for at most ~5 minutes after gatekeeper rolls;
  the acceptance run waits out that window (or restarts `airlock` after the roll) and repeats the call
  through the normal path before reporting, so the requester's App-level qualification starts from a
  post-grant token.
- ailab, from the **workflow worker pod** (`strive-ailab`, its own `LITELLM` base URL, key and model id
  from env — the credential stays in that pod; this proves DNS, NetworkPolicy and key/model access on
  the real path, which a master-key call from inside litellm would not): a python one-shot that
  imports `worker.document_fields.compiler.Observation` and posts the adapter's exact body —
  `Observation.model_json_schema()` as the strict `response_format`, `max_completion_tokens: 32000`,
  `reasoning_effort: medium`, non-streaming — with a sanitized representative document-table prompt
  (the compiler's own prompt builder over a two-page fixture document, or, if that needs a live
  document record, a hand-written prompt of the same shape and ~8 K tokens); expect 200,
  `finish_reason stop`, content that validates against `Observation`, integer usage; **record latency
  and usage** against the adapter's 300 s timeout. Then from inside a litellm pod: one streaming call
  and one `gpt-5.6-sol-api` call, asserting the latter's error is the **upstream** OpenAI
  `AuthenticationError` ("Incorrect API key", provider `openai`) under a valid proxy key — a proxy-side
  401 would look the same on the status line and prove nothing about the retained alias.
- Reply: a file beside the request on dev-worker-3
  (`/workspace/c4/app-model-deploy-artifacts/recovery-ailab-operations-response.md`) with PR/merge
  refs, redacted results, the "no key was replaced" statement, and the `gpt-6-astra` 403 finding.

## Critical files

| Path | Role |
|---|---|
| platform `deploy/secrets/ailab/gatekeeper-secrets.enc.yaml` | one-leaf SOPS edit (`knowledge:write`) |
| platform `deploy/helm/values/providers/ailab.yaml` | `gatekeeper.serviceRegistry.checksum` bump → gatekeeper roll |
| platform `scripts/grant-app-model-profile.py` | `add_knowledge_write` imported by the editor shim (unchanged) |
| `kubernetes/apps/apps/ai/chatgpt_chat.py` | new custom provider: stream-aggregate + scoped `text` pass-through |
| `kubernetes/apps/apps/ai/kustomization.yaml` | `configMapGenerator` merge of the handler into `litellm-config` |
| `kubernetes/apps/apps/ai/litellm.yaml` | routes, `custom_provider_map`, comments, `checksum/config`, `checksum/chatgpt-chat` |
| `scripts/check-inline-hashes.py` | new site: `checksum/chatgpt-chat` vs sha256 of the handler file |
| `scripts/tests/test_litellm_platform_routes.py` | route contract (updated, two admissible shapes) |
| `scripts/tests/integration/test_litellm_platform_route_contract.py` | paid-path wire contract moved to `gpt-5.6-sol-api` |
| `scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py` | image-level handler contract (new) |
| `.gitea/workflows/litellm-route-contract.yaml` | runs the new test + proxy startup smoke in the pinned image |
| `docs/decisions/0027-gpt-5.6-sol-on-the-codex-subscription.md` | the decision record |

## Verification

1. Local: `python -m unittest scripts.tests.test_litellm_platform_routes`,
   `python scripts/gen-litellm-consumers.py --check`, `python scripts/check-inline-hashes.py`,
   `kubectl kustomize kubernetes/apps/apps/ai` (the merged ConfigMap carries both keys, its name and
   namespace unchanged); the new integration test inside the pinned image (`kubectl exec` into a
   litellm pod with the repo files copied to `/tmp`, since this workstation has no docker; CI runs the
   `docker run --network none` form).
2. CI on the ailab PR: `manifests`, `litellm-route-contract` green; both reviewer personas clean.
3. Post-merge: Flux `apps` reconciled at the merge sha (`kustomization.status.lastAppliedRevision`);
   `ai/litellm` 2/2 on the new ReplicaSet with both annotations at their new values; proxy log shows the
   custom provider registered; **the custom route exercised on each new replica** (`kubectl exec` per
   pod, `localhost:4000`), plus an unaffected existing route (`qwen3.8-27b-ailab`) on each — the
   readiness probe is `/health/liveliness` and proves nothing about routing, and the class patch runs
   inside the shared gateway; then Part C acceptance results captured.
4. Platform: PR checks green, merged, Flux reconciled, gatekeeper 2/2 rolled, Part C acceptance
   200/`available=true` after the token-cache window.

<!-- codex-review-status: complete -->
