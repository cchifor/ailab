# Implementation review — held-prs — round 2 (converged)

<!-- codex-impl-review-status: finalized -->

Scope: A1–A3, B1–B3, C (PRs #968, #970, #971; #917/#900 closed; deploy key 6 revoked; #972 opened).
A4–A6 are implemented and reviewed separately (the LiteLLM usage-provenance PR).

## Findings

Round 2: codex dropped the timing-risk and runbook-pointer findings after the evidence-backed pushbacks (render order ConfigMap→Deployment, single-reconcile apply, kubelet mount retry; the pointer already exists). Remaining action: rebase #971 after #970 merges, and reword its kustomization comment in the same push.

### Merge conflict in kustomization.yaml when B3 follows B1
**Location:** kubernetes/apps/apps/llm-router/kustomization.yaml
**Severity:** blocker
ACCEPTED. Reproduced with `git merge-tree` (CONFLICT in kustomization.yaml; the runbook and router.yaml auto-merge). Fix: rebase #971 onto main once #970 merges, not stacked. #971 is held with `no-automerge`, so it cannot merge in the conflicted state.



### A2 renovate.json rule order
**Location:** renovate.json
**Severity:** nit
ACCEPTED as verification. It passes (renovate 44, "Config validated successfully"), and #968's round-1 push records in the rule that nothing re-enables automerge.

### B3 public key format / A1 vkeys Job
**Severity:** nit (no issue found)
No action: codex confirms the Ed25519 PEM and the external fingerprint check, and that the vkeys Job is operator-run and stays in sync.

## Diff stat

 kubernetes/apps/apps/ai/litellm-local.yaml |  6 +++---
 kubernetes/apps/apps/ai/litellm-vkeys.yaml |  2 +-
 kubernetes/apps/apps/ai/litellm.yaml       | 12 +++++++-----
 renovate.json                              | 10 ++++++++--
 4 files changed, 19 insertions(+), 11 deletions(-)

 docs/runbooks/llm-router.md                        | 31 -------
 kubernetes/apps/apps/llm-router/kustomization.yaml |  1 -
 kubernetes/apps/apps/llm-router/network-preflight.yaml | 69 ------
 kubernetes/apps/apps/llm-router/router.yaml        |  8 +--
 4 files changed, 11 insertions(+), 98 deletions(-)

 docs/runbooks/llm-router.md                        | 26 ++++++++++++++++++++++
 kubernetes/apps/apps/llm-router/admin-bridge.yaml  | 20 +++++++++++++++++
 kubernetes/apps/apps/llm-router/kustomization.yaml |  1 +
 kubernetes/apps/apps/llm-router/router.yaml        | 11 +++++++++
 4 files changed, 58 insertions(+)
