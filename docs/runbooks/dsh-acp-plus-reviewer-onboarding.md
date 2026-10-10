# dsh-acp-plus reviewer onboarding

Relay's R2 adapter lives in `cchifor/dsh-acp-plus`, a private maintained derivative
of the MIT-licensed dsh ACP plugin. The operator requested milestone PRs and
reviewer-owned merges for the complete Relay plan. Add this repository to the
existing allowlist; retain both personas, current-head approvals, successful CI,
allowed authors and the `no-automerge` exclusion without exceptions.

## Activation

1. Verify `reviewer-claude` and `reviewer-codex` have repository write access for
   normal review/merge APIs, and inspect main branch protection. Require two
   current-head approvals, dismiss stale approvals and require the conformance
   status checks. Do not create an implementation-worker override.
2. After reviewed merge, converge the existing reviewer hosts through the normal
   operator route: `ansible-playbook reviewers.yml -t reviewbot` from `ansible/`.
   This source change does not prove live activation.
3. Inspect only the `repos` field of `/etc/reviewbot/config.json` on both hosts;
   never print the full file because it contains credentials. Confirm the new
   name, healthy services and existing organization webhook delivery. The
   five-minute reconciler also reads all pages of admitted repositories.
4. Observe both personas reviewing the latest dsh milestone head, successful
   conformance checks and a normal reviewer merge. Repair findings before any
   rollout; native fixture tests do not qualify a production router or worker.

Validation: `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`.
Rollback removes this entry through review, converges both hosts, and only then
removes repository access. Keep posted reviews and conformance evidence.
