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
4. After this onboarding PR merges, run `ansible-playbook reviewers.yml -t reviewbot`
   from the `ansible/` directory with the normal encrypted variables and inventory.
   The existing `ansible/reviewers.yml` playbook includes `role: pr_reviewer`, whose
   defaults in `ansible/roles/pr_reviewer/defaults/main.yml` contain this allowlist. Do not overwrite unrelated
   live configuration or bypass an interrupted reviewer invocation.
5. Confirm both services load `chifor/forge` in the rendered `repos` key. The role
   template `ansible/roles/pr_reviewer/templates/config.json.j2` maps the Ansible
   variable `pr_reviewer_repos` to that JSON key in `/etc/reviewbot/config.json`. Then observe the first PR through
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

## Why polling-only admission works

`ansible/roles/pr_reviewer/tasks/main.yml` reads the fixed
`/orgs/{{ pr_reviewer_org }}/hooks` endpoint and asserts the persona hook's events.
It does not iterate `pr_reviewer_repos` or assert a hook per repository. No hook
assertion exemption is required or introduced for Forge.

`enqueue()` in `ansible/roles/pr_reviewer/files/reviewbot.py` checks exact repository
membership without an organization-prefix restriction. `reconciler()` iterates
`CFG["repos"]` and calls `/repos/{repo}/pulls?state=open&limit=50`, then enqueues each
head. Its per-repository failure gauge records API/poll failures, not absent webhook
deliveries. A successful Forge poll therefore clears that gauge normally; an access
failure remains an actionable alert. This is why both collaborator grants precede
activation, and why observing an actual review is still required.
