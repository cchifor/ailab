# Workbricks reviewer onboarding

`cchifor/workbricks` is a new, private, standalone product repository (created 2026-10-20).
The owner approved its stage-1 plan on 2026-10-21 and requires the same review loop as the
other admitted repositories: both personas review the current head, CI is green, the author is
allowed, and `no-automerge` holds stacked pull requests. This change adds the repository to the
allowlist only; it adds no persona, author exception or merge-gate exception.

State at 2026-10-21: the repository's `main` holds a bootstrap commit; stage-1 pull requests #1
(repository skeleton) and #2 (product specification, stacked on #1, labelled `no-automerge`) are
open. Its CI workflow is `.gitea/workflows/ci.yml` (job `verify`, context
`CI / verify (pull_request)`), which runs on the organization's `self-hosted-hv` runners.

## Activation

1. Give `reviewer-claude` and `reviewer-codex` write access to `cchifor/workbricks`. Apply the
   same `main` protection as `cchifor/relay`: push and force push disabled for everyone; two
   required approvals whitelisted to the two reviewers; merge whitelist limited to them; stale
   approvals dismissed on push; merge blocked on an outdated branch; admin override blocked;
   required status context `CI / verify (pull_request)`. Do not grant the implementation worker
   a merge override.
2. After this change merges, converge the reviewer hosts through the normal operator route:
   `ansible-playbook reviewers.yml -t reviewbot` from `ansible/`. The source change alone does not
   activate anything.
3. Read only the `repos` field of `/etc/reviewbot/config.json` on both hosts (never the whole file;
   it contains credentials) and confirm `cchifor/workbricks` is present. Confirm both services are
   healthy and the organization webhooks deliver pull-request events for the repository; the
   five-minute reconciler also reads all pages of admitted repositories.
4. Observe fresh verdicts from both personas on the current head of
   [workbricks PR #1](https://git.chifor.me/cchifor/workbricks/pulls/1), a green CI context, and a
   reviewer-owned merge. Stacked pull requests are retargeted to `main` after their base merges.

Validation: `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`.

Rollback removes the allowlist entry through review, converges both hosts, and only then removes
repository access and protection. Posted reviews stay.
