# Implementation review — app-model-recovery — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- The implementation broadly follows the gateway plan, including the retained paid alias, no-replay policy, scoped schema forwarding, and rollout checks.
- The round-1 fixes substantially improve startup verification, memory bounds, and transport cleanup.
- Streaming cancellation remains insufficiently handled and tested; one new metadata assertion examines restored state instead of the change under test.
- The supplied evidence does not establish the separate platform grant or the planned post-rollout acceptance through the workflow pod’s actual credentials and network path.

## Findings

### A streaming deadline can cancel the consumer outside the cleanup scope
**Location:** kubernetes/apps/apps/ai/chatgpt_chat.py:308
**Severity:** important
<!-- codex: The asyncio.timeout context remains active across yield chunk, so its deadline can cancel the consuming task while the generator is suspended—for example, during a blocked downstream send—without immediately entering this generator's timeout handler or finally block; the streaming test always drains successfully and does not exercise this case. Scope the absolute deadline to upstream awaits and ensure the consuming layer explicitly closes the iterator on cancellation, adding transport-level tests for cancellation during a read and after a yielded chunk that verify prompt upstream closure. -->

### The metadata assertion runs after restoring the previous entry
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:572
**Severity:** nit
<!-- codex: get_model_info(inner) runs after finally restores saved, which earlier cases already populated with the required pins, so this assertion does not verify the metadata merge performed by case h. Assert the incompatible starting state and the resulting pinned metadata inside the try block before restoration, retaining cleanup in finally. -->

## Diff stat

 .gitea/workflows/litellm-route-contract.yaml       |  90 +++
 .../0027-gpt-5.6-sol-on-the-codex-subscription.md  | 120 ++++
 kubernetes/apps/apps/ai/chatgpt_chat.py            | 330 ++++++++++
 kubernetes/apps/apps/ai/kustomization.yaml         |  15 +
 kubernetes/apps/apps/ai/litellm.yaml               |  76 ++-
 plans/app-model-recovery-impl-review.md            |  66 ++
 scripts/check-inline-hashes.py                     |  28 +
 .../test_litellm_chatgpt_chat_handler_contract.py  | 680 +++++++++++++++++++++
 .../test_litellm_platform_route_contract.py        |  21 +-
 scripts/tests/test_litellm_platform_routes.py      | 102 +++-
 10 files changed, 1505 insertions(+), 23 deletions(-)
