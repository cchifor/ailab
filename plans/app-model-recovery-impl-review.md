# Implementation review — app-model-recovery — rounds 1–2

<!-- codex-impl-review-status: finalized -->

## Findings

### A streaming deadline can cancel the consumer outside the cleanup scope
**Location:** kubernetes/apps/apps/ai/chatgpt_chat.py:308
**Severity:** important
Accepted, fixed in the following commit: the deadline is absolute and applied per upstream read
(`asyncio.timeout` around `__anext__` only; `yield` is outside every timeout context); a consumer
that stops iterating closes the generator and `finally` closes the upstream transport. New cases
`c-abandon` (consumer stops after two chunks, transport closed) and `c-stall-mid` (upstream stalls
between chunks, `Timeout` at the consumer's read at the deadline, transport closed).

### The metadata assertion runs after restoring the previous entry
**Location:** scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py:572
**Severity:** nit
Accepted, fixed: case h asserts the incompatible entry (`chat`/`False`) before the call and the
merged pins (`responses`/`True`) after it, inside the try block, with restoration in `finally`.

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
