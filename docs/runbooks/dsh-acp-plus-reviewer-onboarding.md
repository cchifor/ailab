# dsh-acp-plus reviewer onboarding

Relay's R2 adapter lives in `cchifor/dsh-acp-plus`, a private maintained derivative
of the MIT-licensed dsh ACP plugin. The operator requested milestone PRs and
reviewer-owned merges for the complete Relay plan. Add this repository to the
existing allowlist; retain both personas, current-head approvals, successful CI,
allowed authors and the `no-automerge` exclusion without exceptions.

## Confirmed prerequisites (2026-10-10)

The repository API confirms main protection is already active: two approvals,
stale-approval dismissal, rejection blocking, and required checks
`Conformance / conformance (pull_request)` and
`Conformance / holder-conformance (pull_request)`. Approvals and merges are
restricted to `reviewer-claude` and `reviewer-codex`; both have scoped repository
write access. Direct and force pushes are disabled and administrator merge
override is blocked. This confirmation precedes allowlist activation; recheck
the same settings before any later convergence.

The holder CI job still needs the organization owner to provision the read-only
`RELAY_SOURCE_READ_TOKEN` Actions secret for the private pinned Relay checkout.
Until it passes, the required check blocks merging. No write token substitution
or CI bypass is authorized.

## Activation

1. Verify `reviewer-claude` and `reviewer-codex` have repository write access for
   normal review/merge APIs, and inspect main branch protection. Require two
   current-head approvals, dismiss stale approvals and require the conformance
   status checks. Do not create an implementation-worker override.
2. After reviewed merge, converge the existing reviewer hosts through the normal
   operator route: `ansible-playbook reviewers.yml -t reviewbot` from `ansible/`.
   This source change does not prove live activation.
3. Run `sudo jq .repos /etc/reviewbot/config.json` on both hosts; never print the
   full file because it contains credentials. Confirm the new repository name,
   healthy services and existing organization webhook delivery.
4. Observe both personas reviewing the latest dsh milestone head, successful
   conformance checks and a normal reviewer merge. Repair findings before any
   rollout; native fixture tests do not qualify a production router or worker.

Validation: `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`.
Rollback removes this entry through review, converges both hosts, and only then
removes repository access. Keep posted reviews and conformance evidence.
