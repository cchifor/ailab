# Relay and llm-router reviewer onboarding

The operator requested review, feedback repair and reviewer-owned merges for the Relay
control-plane stack and its llm-router token-management dependency. Both repositories
are absent from the central reviewer allowlist at AILab main `903066b`. The allowlist
admits both webhook and reconciliation jobs; adding only these two entries uses the
existing two-persona review and merge policy.

At 2026-10-10 08:26 UTC, Relay PRs #14–59 were open, with no posted reviews: nine current
heads passed CI and 37 remained pending. Router PR #91 also had no reviews. This is
source/API evidence, not proof of the live reviewer configuration. The implementation
worker cannot read live configuration (SSH public-key denial on both reviewer hosts)
or administrator-only collaborator/protection endpoints (HTTP 403). The access and
activation steps below therefore remain operator work after this PR is reviewed.
The readable `/branches/main` API reports `protected: false`, zero required approvals
and no required status checks for both repositories. The reviewer application still
checks its own merge gates, but those are not forge-enforced branch protections.

## Activation

1. Confirm `reviewer-claude` and `reviewer-codex` can read and review both
   `cchifor/relay` and `cchifor/llm-router`, with the write access required for normal
   review and merge APIs. Check repository protection with the operator identity.
   Review the currently absent main protections and establish the operator-approved
   checks, current-head approval requirements, stale-approval dismissal and merge
   restrictions before activation; this PR does not select or apply those settings. Do not grant the implementation worker merge
   overrides. Resolve missing prerequisites through the normal operator procedure.
2. Let the reviewers merge this AILab PR. From `ansible/`, the operator then applies
   `ansible-playbook reviewers.yml -t reviewbot` using the normal inventory and
   encrypted variables. This PR does not deploy or change permissions/protection.
3. Read only the `repos` field of `/etc/reviewbot/config.json` on both reviewer hosts
   and confirm both names are present. Avoid printing the complete config: it
   contains credentials. Check that both services are healthy and that the existing
   organization webhooks deliver the required PR events. Reconciliation also polls
   admitted repositories every 300 seconds and reads all pages.
4. Observe fresh `reviewer-claude` and `reviewer-codex` verdicts on the current heads
   of [Relay PR #14](https://git.chifor.me/cchifor/relay/pulls/14) and
   [llm-router PR #91](https://git.chifor.me/cchifor/llm-router/pulls/91). Monitor the
   entire Relay stack, including later result pages. Repair findings and preserve
   dependency order as predecessors merge; do not merge from the implementation
   worker. Record reviewer-owned merge SHAs and required CI outcomes in the PRs.
5. Investigate `ReviewbotReconcileRepoFailing`, missing verdicts or failed CI before
   declaring onboarding complete. A source merge or a queued CI run alone does not
   establish that the requested review loop is active.

Existing merge gates remain both personas clean at the current head, successful CI,
an allowed author and absence of `no-automerge`, plus repository branch protection.
`dev-worker-bot` and `workstation-bot` are already allowed authors. This change adds no
persona, author exception, review-policy exception or protection change.

## Verification and rollback

Run `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`. The shipped
allowlist contract now includes both repositories; existing admission, pagination
and merge-gate tests continue to apply.

To stop future reviews, remove the two entries through a reviewed PR and have the
operator converge both hosts. Remove any repository access only after the allowlist
change is active, so the reconcilers do not poll inaccessible repositories. Leave
existing reviews and CI evidence intact.
