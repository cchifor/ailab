# Forge reviewer onboarding

## Scope

The operator requested the existing reviewer-claude/reviewer-codex automation for
`chifor/forge`. The repository is private and user-owned. Add only this repository to
the central allowlist; retain both personas, the existing author allowlist and all CI,
current-head and branch-protection requirements. No repository transfer is needed.

## Prerequisites and activation

1. Give `reviewer-claude` and `reviewer-codex` **write** collaborator access to
   `chifor/forge`. Confirm both identities can read PRs and post reviews.
2. Ensure the repository has an eligible Actions runner. Its workflow initially selects
   `self-hosted-hv`; an org-only runner cannot serve a user-owned repository. Configure
   the repository's `RUNNER_LABEL` for an available instance/repository runner if needed.
3. Configure main protection using the actual successful `forge-quality` status context,
   an up-to-date head, two reviewer approvals, stale-approval dismissal and reviewer-only
   approval/merge whitelists. Verify with a deliberately failing disposable PR.
4. After this onboarding PR merges, converge `ansible/reviewers.yml` on both reviewer
   hosts with the normal encrypted variables and inventory. Do not overwrite unrelated
   live configuration or bypass an interrupted reviewer invocation.
5. Confirm both services load `chifor/forge` in `repos`, then observe the first PR through
   review and merge. Polling discovers new heads within `pr_reviewer_reconcile_s` (300
   seconds by default). The existing **cchifor organization hooks do not cover this
   user-owned repository**. Polling is the explicit delivery mechanism here; repo-scoped
   authenticated hooks may be added independently for lower latency.

## Verification and rollback

Record the Forge PR URL/head, successful CI status, both persona verdicts at that head,
and the reviewer identity that merged it. An allowlist edit alone does not prove live
activation. Investigate `ReviewbotReconcileRepoFailing` if either reviewer cannot read
the repository. The existing org-hook assertions still protect existing org delivery;
they do not attest this repository's polling delivery.

To stop new Forge reviews, remove `chifor/forge` from the allowlist through a reviewed PR
and converge both hosts. Remove collaborator grants only after that change is active,
so the reconciler does not repeatedly request an inaccessible private repository.
