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
- **Side finding (not in scope, must be reported):** `chatgpt/gpt-6-astra` with the same token and
  headers now returns **403 with a Cloudflare JS-challenge page** ("Enable JavaScript and cookies to
  continue") in 0.1–0.4 s, streaming and non-streaming alike, while `gpt-5.6-sol` passes. The
  `gpt-6-astra-realjaynesage` route (and dsh's `openai-codex-realjaynesage` provider) verified on
  2026-09-19 is therefore currently broken by an upstream edge rule, not by anything in git.

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
   decrypted registry == before + `knowledge:write` (structure diff of the svc-airlock audiences only).
3. `sha256sum deploy/secrets/ailab/gatekeeper-secrets.enc.yaml` → `gatekeeper.serviceRegistry.checksum`
   in `deploy/helm/values/providers/ailab.yaml`; run `deploy/helm/scripts/check-service-registry-checksum.sh`.
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
`litellm_settings.custom_provider_map` (a `CustomLLM` subclass loaded by `get_instance_fn` from the
config directory or `PYTHONPATH`).

Files (all under `kubernetes/apps/apps/ai/` unless stated):

- **`chatgpt_chat.py`** (new; ~120 lines, stdlib + litellm only). Registered as provider
  `chatgpt-chat`. Behaviour:
  - `acompletion(model, messages, optional_params, …)`: one inner
    `litellm.acompletion(model="chatgpt/<model>", messages, stream=True, stream_options={"include_usage": True}, "no-log": True, **optional_params minus stream/stream_options)`,
    drained and rebuilt with `litellm.stream_chunk_builder(chunks, messages=messages)`; the built
    response's `model` is set to the bare model id. `no-log` keeps the proxy's spend/metrics
    logging from counting the inner call a second time.
  - `astreaming`: the same inner call, each `ModelResponseStream` chunk mapped to a
    `GenericStreamingChunk` (`text`, `is_finished`, `finish_reason`, `usage`, `index`), so a
    streaming client (Open WebUI under External) keeps working.
  - Sync `completion`/`streaming`: `CustomLLMError(501)` with a message — the proxy only calls the
    async pair.
  - **Scoped allow-list widening.** A module-level `contextvars.ContextVar` is set only around the
    inner call; the module wraps `ChatGPTResponsesAPIConfig.transform_responses_api_request` so that,
    when the var is set, `text` (from the parent `OpenAIResponsesAPIConfig` transform, i.e. the
    caller's `response_format`) is re-added to the request. `max_output_tokens` stays dropped
    (backend 400). Outside the var — every `chatgpt/` call that does not come through this handler,
    i.e. `gpt-6-astra-realjaynesage` — the request is byte-identical to today's; the existing
    image-level test's `EXPECTED_BODY_KEYS` for that route stays true.
  - Import-time guards: the upstream class, method and its keyword signature are asserted at import;
    a pin bump that moves them makes the proxy **fail to start** (Flux rollout stalls at 1 new pod,
    the old ReplicaSet keeps serving under `maxUnavailable: 0`) rather than silently serving without
    the schema. The image-level test runs in the pinned image in CI, so the failure surfaces at PR
    time for any pin bump that touches `litellm.yaml`.
- **`kustomization.yaml`**: `configMapGenerator` entry `litellm-handlers` with
  `files: [chatgpt_chat.py]` (hash-suffixed name → Flux/kustomize rewrites the Deployment's volume
  reference → the pod template moves → an automatic roll on any handler change, with no hand-stamped
  checksum to drift).
- **`litellm.yaml`**:
  - Deployment: volume `handlers` (configMap `litellm-handlers`) mounted read-only at
    `/etc/litellm-handlers`; env `PYTHONPATH=/etc/litellm-handlers` (`get_instance_fn` looks for
    `<config dir>/chatgpt_chat.py` first, then `importlib.import_module`).
  - `litellm_settings.custom_provider_map: [{provider: chatgpt-chat, custom_handler: chatgpt_chat.handler}]`.
  - `model_list`: `gpt-5.6-sol` → `chatgpt-chat/gpt-5.6-sol`, `num_retries: 0` (the caller's one-attempt
    policy is unchanged); `gpt-5.6-sol-api` → `openai/gpt-5.6-sol`, `api_key: os.environ/OPENAI_API_KEY`,
    `num_retries: 0` (the retained paid path — flipping back is swapping the two names). No
    `fallbacks` between them: a fallback to a paid key contradicts the route's "never replay paid
    generation" policy, and the key is dead anyway. Neither has an `api_base`, so neither enters the
    generated pickers (Open WebUI Local, dsh); both stay discoverable under External, as today.
  - Comment record on the route: the measurements above, the accepted loss (`max_completion_tokens`
    is dropped at the provider — the platform still enforces its 32 000 cap client-side on
    `usage.completion_tokens`), the subscription-quota exposure (document-table drafts are large:
    up to 512 K input / 32 K output per attempt against seat d's weekly window, shared with dsh), the
    cost-map warning (no price for `chatgpt-chat/…`), and the Cloudflare-challenge exposure seen on
    `gpt-6-astra` the same day.
  - `checksum/config` bumped by `python scripts/gen-litellm-consumers.py --write` (records the routes
    and `custom_provider_map`; the handler file rolls through the generator hash).
- **`scripts/tests/test_litellm_platform_routes.py`**: contract updated — exactly one `gpt-5.6-sol`
  route, `chatgpt-chat/gpt-5.6-sol` with `num_retries: 0` and no `api_key`; exactly one
  `gpt-5.6-sol-api` route, `openai/gpt-5.6-sol` + `os.environ/OPENAI_API_KEY` + `num_retries: 0`;
  `custom_provider_map` names `chatgpt_chat.handler`; the generator lists `chatgpt_chat.py`; the
  Deployment mounts it on `PYTHONPATH`; no fallback rule names either route.
- **`scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py`** (new, image-level,
  sockets denied, same harness as `test_litellm_chatgpt_route_contract.py`): loads the handler from
  the manifest path, registers the provider, builds a Router from the two routes as written, mocks
  `AsyncHTTPHandler.post` with an SSE fixture in chatgpt.com's **real** shape (`response.created`,
  `output_item.added`, `output_text.delta`×n, `output_text.done`, `output_item.done`, and a
  `response.completed` whose `output` is `[]` — the measured trap) and asserts:
  (a) non-streaming `router.acompletion` with the platform's exact body returns `content ==
  '{"answer":"OK","n":7}'`, `finish_reason stop`, integer usage, and the wire body carries
  `text.format` == the schema, `reasoning`, `stream: true`, `store: false`, and **no**
  `max_output_tokens`; (b) the same fixture through `gpt-6-astra-realjaynesage` still sends no
  `text`; (c) streaming yields the text and a final chunk; (d) upstream 401 → `AuthenticationError`
  in seconds; (e) the placeholder auth.json builds the Router with no socket attempt.
  Wired into `.gitea/workflows/litellm-route-contract.yaml` (third `docker run`) with path triggers
  for `chatgpt_chat.py`, `kustomization.yaml` and the new test.
- **`docs/decisions/0027-gpt-5.6-sol-on-the-codex-subscription.md`**: the operator's directive, the
  measurements, the handler design and its scoping, what flips it back (rename), the quota and
  edge-rule exposures, and that the API key remains the intended steady state once replaced.

### Part C — acceptance (both run in-cluster, secrets never leave their pods)

- Platform: from inside an `airlock` pod, mint the S2S token the way airlock does for `svc-knowledge`
  (its own client credentials, gatekeeper's token endpoint) and call the availability URL; expect
  200 `{"available": true, …}`. Record status + body keys, not the token.
- ailab: from inside a litellm pod with its master key from env, `POST /v1/chat/completions` with
  the platform's exact body shape (`response_format` strict schema, `max_completion_tokens`,
  `reasoning_effort`); expect 200, `finish_reason stop`, JSON content matching the schema, integer
  usage. Also one streaming call, and one `gpt-5.6-sol-api` call to show it still answers 401 (the
  retained path is wired, just keyless).
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
| `kubernetes/apps/apps/ai/kustomization.yaml` | `configMapGenerator` `litellm-handlers` |
| `kubernetes/apps/apps/ai/litellm.yaml` | routes, `custom_provider_map`, mount + `PYTHONPATH`, comments, `checksum/config` |
| `scripts/tests/test_litellm_platform_routes.py` | route contract (updated) |
| `scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py` | image-level handler contract (new) |
| `.gitea/workflows/litellm-route-contract.yaml` | runs the new test in the pinned image |
| `docs/decisions/0027-gpt-5.6-sol-on-the-codex-subscription.md` | the decision record |

## Verification

1. Local: `python -m unittest scripts.tests.test_litellm_platform_routes`,
   `python scripts/gen-litellm-consumers.py --check`, `python scripts/check-inline-hashes.py`,
   `kustomize build kubernetes/apps/apps/ai` (the generator ConfigMap renders and the Deployment's
   volume reference is rewritten to the hashed name); the new integration test inside the pinned image
   (`docker run --network none …` as the workflow does, or `kubectl exec` into a litellm pod with the
   repo files copied to `/tmp`, since this workstation has no docker).
2. CI on the ailab PR: `manifests`, `litellm-route-contract` green; both reviewer personas clean.
3. Post-merge: Flux `apps` reconciled; `ai/litellm` 2/2 on the new ReplicaSet; proxy log shows the
   custom provider loaded; Part C acceptance results captured.
4. Platform: PR checks green, merged, Flux reconciled, gatekeeper 2/2 rolled, Part C acceptance
   200/`available=true`.

<!-- codex-review-status: pending -->
