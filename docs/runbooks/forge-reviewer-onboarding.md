# Forge reviewer onboarding

## Scope

The operator requested the existing reviewer-claude/reviewer-codex automation for
`cchifor/forge`. The private repository was relocated into the cchifor organization
on 2026-10-03. Its repository ID (117), PRs, branch protections and repository-scoped
runner registration were preserved. Add only this repository to the central allowlist;
retain both personas, author allowlists and all CI/current-head requirements.

## Prerequisites and activation

1. Confirm `reviewer-claude` and `reviewer-codex` retain **write** access after the
   transfer. Both received collaborator access before transfer; verify PR reading
   and review posting after activation.
2. The `self-hosted-hv` organization runner pool now serves this repository. A temporary
   repository-scoped `forge-workspace-runner` supplements it while that pool is busy.
   Stop the temporary runner once the organization pool is serving Forge reliably.
3. Main protection requires `forge-quality / forge-quality (pull_request)`, an
   up-to-date branch, two reviewer approvals and stale-approval dismissal. Direct
   push, force push and administrator merge override are disabled. Only the reviewer
   identities are in approval/merge allowlists. Preserve these settings.
4. After this PR merges, run `ansible-playbook reviewers.yml -t reviewbot` from the
   `ansible/` directory using normal encrypted variables and inventory. The existing
   `ansible/reviewers.yml` playbook includes `role: pr_reviewer`, whose defaults in
   `ansible/roles/pr_reviewer/defaults/main.yml` contain the allowlist.
5. Confirm `/etc/reviewbot/config.json` on both hosts contains `cchifor/forge` in `repos`.
   `ansible/roles/pr_reviewer/templates/config.json.j2` maps `pr_reviewer_repos` to that
   JSON key. Existing cchifor org webhooks provide delivery; reconciliation also polls
   each `/repos/{repo}/pulls` endpoint every 300 seconds by default.
6. Observe [Forge PR #1](https://git.chifor.me/cchifor/forge/pulls/1) through review and
   reviewer-owned merge. Record its final head, CI status and both persona verdicts.

## Verification and rollback

Forge's initial committed Stage 1 head passed the actual quality workflow. The
[isolated failing PR #2](https://git.chifor.me/cchifor/forge/pulls/2) used the same
required CI context and admin-override block with no approval requirement. CI failed
at `ddc689203ceb574cfcb40aed4391d6a4e122b6d2`, and a normal merge attempt returned HTTP
405, `Not all required status checks successful`. It was closed without merging.
Main protection remains in place; the probe did not modify main.

Allowlist source alone does not prove live activation. Investigate
`ReviewbotReconcileRepoFailing` if either reviewer cannot read the repo. The role's
existing org-hook assertions check `pr_reviewer_org: cchifor` and the persona event
subscriptions. No exception or review-policy change is introduced by this onboarding.

To stop new Forge reviews, remove `cchifor/forge` from the allowlist through a reviewed
PR and converge both hosts. Remove collaborator grants only after that change is
active, so the reconciler does not poll an inaccessible private repository.
