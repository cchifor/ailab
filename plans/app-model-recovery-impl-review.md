# Implementation review — app-model-recovery — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### The content cap does not bound retained response memory
**Location:** kubernetes/apps/apps/ai/chatgpt_chat.py:225
**Severity:** important
Accepted, fixed in 8564dacb: every delta payload (text, refusal, reasoning, tool-call JSON) counts
against `MAX_CONTENT_BYTES`, and `MAX_CHUNKS` (262 144) bounds the chunk count so empty or tiny
events cannot escape the byte count; both lowered-limit cases are in the image test. Non-text
payloads are counted but no fixture can reach them: the 1.101.0 bridge emits none (see the refusal
finding below).

### The startup smoke never executes its model assertions
**Location:** .gitea/workflows/litellm-route-contract.yaml:148
**Severity:** important
<!-- codex: docker exec without -i does not forward the heredoc to python -, so the /v1/models assertions never execute; because exhausting the liveliness polling loop also does not fail the step, a still-running but unready proxy can pass this smoke. Add -i, explicitly fail when the readiness deadline expires, and check the provider-registration log required by the plan. -->
<!-- opus-pushback: -i and the explicit readiness failure are fixed in 26fa408e (reviewer-codex raised the same on #799). The "provider-registration log" cannot be checked because it does not exist: in 1.101.0 `litellm.utils.custom_llm_setup()` only appends to `provider_list`/`_custom_providers` and logs nothing, and `ProxyConfig.load_config` logs no line for custom_provider_map either — the plan's wording was aspirational. A module that fails to import raises out of `get_instance_fn` inside load_config and the proxy exits (the smoke's "exited during startup" branch), and the proxy advertising both gpt-5.6-sol aliases on /v1/models after a successful start is the positive evidence the config loaded; the plan's post-merge check is corrected to those two signals. -->

### Cleanup tests count helper calls instead of resource closure
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:373
**Severity:** important
Accepted, fixed in 8564dacb: the mock transport is now a real `httpx.AsyncByteStream` whose
`aclose()` is what `closed` records; asserted after a normal drain (a, c), after a mid-drain stall
at the deadline (`d-stall-mid`, the body hangs after 256 bytes) and after every bound abort. Doing
so exposed a real gap — the bridge iterator's `aclose()` never reaches the httpx response — which
the handler now closes explicitly (`_close`).

### Existing metadata can still disable native streaming
**Location:** kubernetes/apps/apps/ai/chatgpt_chat.py:150
**Severity:** important
Accepted, fixed in 8564dacb: `_ensure_model_info` merges `mode: responses` and
`supports_native_streaming: True` over whatever entry exists for the inner id (not only when one is
missing); case `h` pre-seeds an entry saying `false`/`chat` and asserts the wrapped POST still
streams natively and the pins are merged. Every wrapped POST now also asserts `stream=True` on the
transport call.

### The planned API switch-back fails the required integration test
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:409
**Severity:** important
Accepted, fixed in 8564dacb: the routes are found by shape (`chatgpt-chat/…`, `openai/gpt-5.6-sol`,
the dsh route by name); with no `chatgpt-chat/` route in the manifest the module prints why and
exits 0, so pointing `gpt-5.6-sol` back at the API key is a config change that leaves CI green. The
paid-path wire assertions live in `test_litellm_platform_route_contract.py` against `gpt-5.6-sol-api`
and are unaffected.

### The refusal assertion permits losing the refusal entirely
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:522
**Severity:** important
<!-- codex: The OR assertion accepts an empty response with no refusal metadata, whereas the finalized plan requires message.refusal to be populated and content to be empty so the consumer's explicit refusal guard fires. Assert both properties separately, including the fixture's refusal text, and preserve that metadata during aggregation if the builder drops it. -->
<!-- opus-pushback: Measured in the pinned image with the refusal fixture: the bridge iterator yields 6 chunks and NONE carries the refusal (no `delta.refusal`, no provider_specific_fields) — the text is dropped before the handler ever sees it, so there is nothing to preserve during aggregation without re-implementing the SSE parse. The test now asserts the two properties separately as the measured 1.101.0 shape (content == "" AND refusal is None, so a pin bump that starts carrying the text flips it), and ADR 0027 records the loss: the consumer still fails closed on empty content (it is not its JSON), just not through its refusal guard. -->

## Diff stat

 .gitea/workflows/litellm-route-contract.yaml       |  84 +++
 .../0027-gpt-5.6-sol-on-the-codex-subscription.md  | 112 ++++
 kubernetes/apps/apps/ai/chatgpt_chat.py            | 294 ++++++++++
 kubernetes/apps/apps/ai/kustomization.yaml         |  15 +
 kubernetes/apps/apps/ai/litellm.yaml               |  76 ++-
 scripts/check-inline-hashes.py                     |  28 +
 .../test_litellm_chatgpt_chat_handler_contract.py  | 596 +++++++++++++++++++++
 .../test_litellm_platform_route_contract.py        |  21 +-
 scripts/tests/test_litellm_platform_routes.py      | 102 +++-
 9 files changed, 1305 insertions(+), 23 deletions(-)
