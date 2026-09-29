# Implementation review — held-prs — round 1

<!-- codex-impl-review-status: complete -->

Scope: A1–A3, B1–B3, C (PRs #968, #970, #971; #917/#900 closed; deploy key 6 revoked; #972 opened).
A4–A6 are implemented and reviewed separately (the LiteLLM usage-provenance PR).

## Findings

### Merge conflict in kustomization.yaml when B3 follows B1
**Location:** kubernetes/apps/apps/llm-router/kustomization.yaml
**Severity:** blocker
<!-- codex: B1 deletes the `network-preflight.yaml` resource entry; B3 adds `admin-bridge.yaml` next to it. When B3 merges after B1, Git reports a conflict. Rebase B3 onto gitea/main after B1 merges, and verify the kustomization lists only router.yaml and admin-bridge.yaml. -->
ACCEPTED. Reproduced with `git merge-tree` (CONFLICT in kustomization.yaml; the runbook and router.yaml auto-merge). Fix: rebase #971 onto main once #970 merges, not stacked. #971 is held with `no-automerge`, so it cannot merge in the conflicted state.

### ConfigMap-to-Recreate deployment timing risk in B3
**Location:** kubernetes/apps/apps/llm-router/router.yaml, admin-bridge.yaml
**Severity:** important
<!-- codex: Kustomize resource order does not guarantee API-server application order; the Recreate pod may start before the ConfigMap exists (MountVolume.SetUp failed), a transient outage risk. Verify post-deployment and document the risk. -->
<!-- opus-pushback: Not a real outage path. `kubectl kustomize` emits the ConfigMap before the Deployment (render order: Namespace, ConfigMap, Service, PVC×2, Deployment, …), and kustomize-controller applies the whole set in one reconcile, so the ConfigMap exists before the new ReplicaSet's pod is even scheduled. And if a ConfigMap volume source were missing, kubelet retries the mount and holds the pod in ContainerCreating until it appears; it never fails the pod. The real risk is only the Recreate roll itself, which the PR already states. Accepted part: the kustomization comment "MUST exist before the Recreate pod starts" overstates it. It will be reworded in the rebase push, and the post-merge check (pod Ready, no MountVolume events) stays in the PR's verification. -->

### B1 runbook: pointer for a re-run
**Location:** docs/runbooks/llm-router.md (B1)
**Severity:** nit
<!-- codex: Suggest "See ailab#901 for the original Job definition". -->
<!-- opus-pushback: Already there: the section ends "For another run, re-add it from #901 under a new versioned Job name." -->

### A2 renovate.json rule order
**Location:** renovate.json
**Severity:** nit
<!-- codex: Relies on later-rules-override; confirm renovate-config-validator --strict passes. -->
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
