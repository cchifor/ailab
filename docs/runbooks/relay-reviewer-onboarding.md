# Relay and llm-router reviewer onboarding

**Status: activated 2026-10-11** (worker-identities plan, Phase 0). What was applied is in
§ "Activation record"; recovery when the protection stops every merge is in § "Break-glass". The
account below is the state before activation.

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
an allowed author and absence of `no-automerge`. Forge-enforced branch protection is
separate from these application checks and is the operator prerequisite in step 1;
it was absent on the two target main branches until the 2026-10-11 activation below.
`dev-worker-bot` and `workstation-bot` are already allowed authors. This change adds no
persona, author exception, review-policy exception or protection change. Since
2026-10-11 the per-slot `dev-worker-1-bot` … `dev-worker-4-bot` are allowed authors too
(`pr_reviewer_merge_authors`, `plans/2026-10-11-worker-identities-plan.md`).

## Activation record (2026-10-11)

Applied by the operator on 2026-10-11. Both repositories now carry the same `main`
protection as `cchifor/forge`:

| Setting | `cchifor/relay` | `cchifor/llm-router` |
| --- | --- | --- |
| Push and force push | disabled for everyone | disabled for everyone |
| Required approvals | 2, approval whitelist `reviewer-claude`, `reviewer-codex` | same |
| Merge whitelist | `reviewer-claude`, `reviewer-codex` | same |
| Stale approvals | dismissed on push | dismissed on push |
| Outdated branch | merge blocked (`block_on_outdated_branch`) | merge blocked |
| Admin merge override | blocked | blocked |
| Required status context | `Verify / verify (pull_request)` | `CI / verify (pull_request)` |

- `reviewer-claude` and `reviewer-codex` are write collaborators on both repositories.
- `reviewers.yml` was converged at 10:40Z, so the deployed reviewbot admits
  `cchifor/relay`, `cchifor/llm-router` and `cchifor/dsh-acp-plus`.
- Not part of this record (verify, then add the result here): a test PR event with a
  successful org-webhook delivery (reviews appearing are not proof: the reconciler also
  polls every 300 s), and the negative test, in which `workstation-bot` and a dev-worker
  token are refused a push to `main` and a merge, and the reviewbot then merges that PR
  with `merged_by` naming the reviewer.

## Break-glass: when the protection stops every merge

With exactly two whitelisted approvers, two required approvals and one required context,
one reviewer-host outage or a renamed CI context stops every merge on these repositories.
Recovery is an **owner operation**, with a one-shot `chifor` token handled the way
`scripts/gitea-owner-merge.sh` handles its own (that script merges and posts `approve-pin`;
it does not edit protection):

1. Open an issue in the affected repository: the reason, the time (UTC) and what will be
   relaxed.
2. Mint the token in the Gitea pod, captured into a variable and never printed:
   `gitea admin user generate-access-token --username chifor --token-name
   owner-op-<repo>-protection-<UTC timestamp> --scopes write:repository --raw`
   ([`owner-credentials.md`](owner-credentials.md) § Break-glass, step 2).
3. Save the current rule (`GET /repos/cchifor/<repo>/branch_protections/main`), then relax
   only what is broken with a `PATCH` to the same path: `required_approvals` to 0 while a
   reviewer host is down, or `status_check_contexts` corrected for a renamed context.
4. Delete the token at once (`delete from access_token where name='<name>'` on the
   infra-pg primary, database `gitea`) and confirm that its next use answers 401.
5. When the reviewers recover, restore the saved rule with a new one-shot token, read it
   back against the table above, delete that token the same way, and close the issue with
   the read-back.

`gitea_admin` stays the last resort, as everywhere else
([`owner-credentials.md`](owner-credentials.md) § Break-glass).

## Verification and rollback

Run `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`. The shipped
allowlist contract now includes both repositories; existing admission, pagination
and merge-gate tests continue to apply.

To stop future reviews, remove the two entries through a reviewed PR and have the
operator converge both hosts. Remove any repository access only after the allowlist
change is active, so the reconcilers do not poll inaccessible repositories. Leave
existing reviews and CI evidence intact.
