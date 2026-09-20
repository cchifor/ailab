# Implementation review — app-model-recovery — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- The gateway implementation largely follows Part B, including route isolation, retained API access, scoped schema forwarding, and disabled retries.
- The Responses prefix and raw-iterator aggregation are justified by the supplied measurements. Estimated streaming usage is a documented narrowing of the original contract.
- Important gaps remain in memory bounds, stream-cleanup verification, and the startup smoke. The promised configuration-only switch-back also breaks the new integration contract.
- The supplied passing tests and live Router results support the main request path; they do not establish Part A or the workflow-pod and post-rollout acceptance checks in Part C.

## Findings

### The content cap does not bound retained response memory
**Location:** kubernetes/apps/apps/ai/chatgpt_chat.py:225
**Severity:** important
<!-- codex: Every full chunk is retained, but only choices[0].delta.content contributes to the limit: tool arguments, refusal data, other metadata, and arbitrarily many empty chunks escape accounting, while tiny text chunks can consume far more memory than their payload size. Bound all retained data and chunk count, or aggregate incrementally with explicit limits, and add oversized non-text and many-small-chunk cases to verify protection of the shared proxy. -->

### The startup smoke never executes its model assertions
**Location:** .gitea/workflows/litellm-route-contract.yaml:148
**Severity:** important
<!-- codex: docker exec without -i does not forward the heredoc to python -, so the /v1/models assertions never execute; because exhausting the liveliness polling loop also does not fail the step, a still-running but unready proxy can pass this smoke. Add -i, explicitly fail when the readiness deadline expires, and check the provider-registration log required by the plan. -->

### Cleanup tests count helper calls instead of resource closure
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:373
**Severity:** important
<!-- codex: The closed counter increments before _close runs, so it passes even if no underlying resource is closed, and the stall fixture blocks inside post before the handler receives a stream rather than during its drain. Add a close-tracked streaming transport and exercise cancellation and timeout after iteration begins, asserting that the underlying iterator/HTTP response actually closes and that only one upstream request occurs. -->

### Existing metadata can still disable native streaming
**Location:** kubernetes/apps/apps/ai/chatgpt_chat.py:150
**Severity:** important
<!-- codex: _ensure_model_info only registers missing entries, leaving a present remote entry with supports_native_streaming unset or false untouched and then caching that decision in _REGISTERED; this does not enforce the claimed independence from the remote map and can restore the unsupported non-streaming transport path. Explicitly pin the required native-streaming capability while preserving other metadata, and test both absent and present-but-incompatible entries. -->

### The planned API switch-back fails the required integration test
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:409
**Severity:** important
<!-- codex: This test always exercises the manifest's gpt-5.6-sol route while requiring the ChatGPT wire body and custom-provider callback, so changing that route to the expressly admissible OpenAI shape makes required CI fail despite the promise that switch-back needs only a configuration change. Make test loading and assertions follow the active route shape, retaining exact API wire-body and one-attempt checks when the primary alias returns to OpenAI. -->

### The refusal assertion permits losing the refusal entirely
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:522
**Severity:** important
<!-- codex: The OR assertion accepts an empty response with no refusal metadata, whereas the finalized plan requires message.refusal to be populated and content to be empty so the consumer's explicit refusal guard fires. Assert both properties separately, including the fixture's refusal text, and preserve that metadata during aggregation if the builder drops it. -->

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
