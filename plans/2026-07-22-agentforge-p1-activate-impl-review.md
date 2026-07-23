# Implementation review — agentforge-p1-activate — round 1

<!-- codex-impl-review-status: finalized -->

Codex Phase-B review (profile=review) of the PR-A + PR-B diff against the finalized plan. Two
findings, both ACCEPTED and fixed in commit `fix(agentforge): go-live comments + atomic token-fill`.

## Findings + dispositions

### 1. (blocker) Runtime token fills would be reverted by GitOps
Codex: `agentforge-runtime.sops.yaml` stays in the Flux render with placeholder token values; if the
operator fills the resulting *Secret* at runtime, Flux reverts it, and the pod captures the tokens as
env at startup — so PR-B could launch with placeholders.

**ACCEPT.** The runbook already directed a SOPS-file fill (not a live edit), but the fix makes it
airtight: the token-filled `agentforge-runtime.sops.yaml` must land **as ciphertext on the PR-B
branch itself**, merging atomically with `- deployment.yaml`. Clarified in the kustomization go-live
note + runbook step 2. (Nuance recorded: `/readyz` checks only the DB, so placeholders would not
wedge the apps layer — they would fail create-workspace→tenants-commit; the fix stands regardless.)

### 2. (important) Contradictory placeholder / exclusion comments after go-live
Codex: the manifests still described the digest as a placeholder / `deployment.yaml` as deliberately
unlisted / the migrate argv as needing adjustment, contradicting the activated state.

**ACCEPT (partial — one comment was already current).** Updated the `deployment.yaml` header in PR-B
to the active/go-live state. The db-migrate "adjust the argv" instruction and the line-51 image
comment were already rewritten in PR-A (`agentforge-platform migrate` verified against the image
Entrypoint), so no further change there.

## Converged
No pushback markers remain; both findings resolved in one round.

## Diff stat (finalize..HEAD, both PRs)
```
 docs/runbooks/agentforge-platform-activation.md    | 199 +++++++++++++++++++
 kubernetes/apps/apps/agentforge/db-migrate.yaml    |  15 +-
 kubernetes/apps/apps/agentforge/deployment.yaml    |  36 ++-
 kubernetes/apps/apps/agentforge/kustomization.yaml |  20 +-
 kubernetes/apps/apps/auth/authelia-secret.sops.yaml|  35 +--   (SOPS re-encrypt; +af:tenant-zero:owner)
```
