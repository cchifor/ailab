# Workbricks reviewer onboarding

`cchifor/workbricks` is a private, standalone product repository, and `cchifor/workbricks-apps`
holds its business apps (the owner chose a separate repository for them on 2026-10-24). Both get
the same review loop as the other admitted repositories: both personas review the current head,
CI is green, the author is allowed, and `no-automerge` holds stacked pull requests. Admitting
them adds allowlist entries only, with no new persona, author exception or merge-gate exception.

For architectural or policy changes requiring owner consent, use `owner-approval` (the
configurable `owner_approval_label`) and have the owner click Gitea's **Approve** button on
the current head; keep the label in place so CI can recognize the policy-change lane.
Reviewbot then merges when every existing gate passes, including green CI and clean reviews
from both personas. The approval must be `APPROVED`, match the current `commit_id`, and be
neither dismissed nor stale; a new head requires a new approval. Eligible logins are the
global `owner_approvers` plus this repository's `owner_approvers_by_repo` entry (role variables
use the `pr_reviewer_` prefix); defaults grant only `chifor` on `cchifor/workbricks` and no
global approvers. Persona accounts and the PR author never qualify, even if listed, and an
empty eligible list holds the PR indefinitely. This applies to ordinary and chunk reviews;
waiting logs `owner approval` without paging `ReviewbotMergeBlocked`. `no-automerge` remains
an absolute hold even with owner approval, and branch protection still applies.

History:

- **2026-10-20/21:** the first `cchifor/workbricks` was created and onboarded (allowlist entry,
  bot access, `main` protection; ailab #1382).
- **2026-10-24:** that repository was renamed to `cchifor/workbricks-old`, and a new
  `cchifor/workbricks` was created under the old name. A rename keeps the repository's
  collaborators, protection, deploy keys and Actions secrets, so all of them went with
  `workbricks-old`. The new repository started with no bot access and no protection. The
  allowlist entry, which keys on the name, then pointed at the new repository. Both reviewer
  hosts logged `HTTP Error 404` on its reconcile and on workbricks#1's review jobs until access
  was restored (§ Activation record). `cchifor/workbricks-apps` was created the same day.

`cchifor/workbricks` follows the `cchifor/docs` repository template. Its CI workflow is
`.gitea/workflows/ci.yml` (workflow `CI`, job `verify`, context `CI / verify (pull_request)`)
and runs on the organization's `self-hosted-hv` runners; it arrives with workbricks#1.
`cchifor/workbricks-apps` has **no CI yet** (its `main` is an empty root commit): its first pull
request must add the same template workflow, because the `main` rule below already requires
`CI / verify (pull_request)` and nothing produces that context there until then.

## Activation

1. Give `reviewer-claude` and `reviewer-codex` write access to the repository. Apply the same
   `main` protection as `cchifor/relay`: push and force push disabled for everyone; two required
   approvals whitelisted to the two reviewers; merge whitelist the two reviewers and the owner
   `chifor` (added 2026-10-31, `relay-reviewer-onboarding.md` § "The owner in the merge
   whitelist"); stale approvals
   dismissed on push; merge blocked on an outdated branch; admin override blocked; required
   status context `CI / verify (pull_request)`. Do not grant the implementation worker a merge
   override. An empty repository needs a `main` first, or its first pull request has no base
   (an empty root commit is enough).
2. After the allowlist change merges, converge the reviewer hosts through the normal operator
   route: `ansible-playbook reviewers.yml -t reviewbot` from `ansible/`. The source change alone
   does not activate anything.
3. Read only the `repos` field of `/etc/reviewbot/config.json` on both hosts (never the whole file;
   it contains credentials) and confirm the repository is present. Confirm both services are
   healthy and the organization webhooks deliver pull-request events for the repository; the
   five-minute reconciler also reads all pages of admitted repositories.
4. Observe fresh verdicts from both personas on the current head of the first pull request, a
   green CI context, and a reviewer-owned merge. Stacked pull requests are retargeted to `main`
   after their base merges.

## Activation record (2026-10-24)

Applied 2026-10-24 ~16:16Z under the one-shot `chifor` token discipline (`owner-credentials.md`).
The rule body is a copy of `workbricks-old`'s `main` rule. Before the apply, that rule was read
and checked against `cchifor/relay`'s: the two differ only in the status context
(`Verify / verify (pull_request)` on relay). The read-back of both new rules matched the body,
and the token answered 401 after deletion.

| Setting | `cchifor/workbricks` | `cchifor/workbricks-apps` |
| --- | --- | --- |
| `reviewer-claude`, `reviewer-codex` | write collaborators | write collaborators |
| Push and force push | disabled for everyone | disabled for everyone |
| Required approvals | 2, approval whitelist `reviewer-claude`, `reviewer-codex` | same |
| Merge whitelist | `reviewer-claude`, `reviewer-codex`; `chifor` since 2026-10-31 | same |
| Stale approvals | dismissed on push | dismissed on push |
| Rejected reviews | block the merge | block the merge |
| Outdated branch | merge blocked (`block_on_outdated_branch`) | merge blocked |
| Admin merge override | blocked | blocked |
| Required status context | `CI / verify (pull_request)` | `CI / verify (pull_request)` |

- `cchifor/workbricks-apps` was empty. `main` was seeded with an empty root commit (`6624cd7`, as
  `workstation-bot`) before the rule was applied, the same way `cchifor/workbricks`'s `main` was
  seeded by dev-worker-2.
- **Negative test:** run as `workstation-bot`. It reads `protected=true`, `user_can_push=false`
  and `user_can_merge=false` on `main` in both repositories. Its direct push of an empty commit to
  `workbricks-apps` `main` was refused (`pre-receive hook declined`). The test stands for the
  dev-worker bots because their grants were checked to be the same, in the Gitea database
  (`team_user`, `collaboration`, `user.is_admin`, read-only):
  - `dev-worker-bot` and `dev-worker-1-bot` … `dev-worker-4-bot` are non-admin and members of team
    `automation` only (id 37, `repo.code`/`repo.pulls` write), the same team as `workstation-bot`.
    None of them is in `Owners`.
  - Neither repository has a collaborator except `reviewer-claude` and `reviewer-codex` (mode 2,
    write).

  The rule also disables push for everyone, whatever the grant. Dev workers therefore change `main`
  only through pull requests that the reviewers merge.
- `cchifor/workbricks-apps` has no CI yet (see above). Reviewbot also merges only when the
  combined status is `success`, so nothing merges there before that workflow exists.

## Not carried over from the old repository

These belong to the Workbricks cell (`workbricks-cell.md`), not to review. The recreate broke
them, and they were found during the reviewer re-activation. Tracked with the fix steps in
[ailab#1415](https://git.chifor.me/cchifor/ailab/issues/1415); do both before the first stage-2
deployment PR unsuspends `workbricks-c1`:

- **Deploy key: fixed 2026-10-26.** Id 72 (read-only, used by Flux `GitRepository`
  `workbricks-c1`) stayed on `workbricks-old`. From the recreate until the fix, `workbricks-c1`
  was `Ready=False` (`GitOperationFailed ... invalid pkt-len found`). Its Kustomization is
  suspended, so nothing deployed from it. The SOPS-held public key (fingerprint
  `SHA256:vVx9amr3…`, unchanged) is now read-only deploy key **id 73** on the new repository.
  `workbricks-c1` is `Ready=True`, and id 72 has been removed from `workbricks-old`.
- **Actions secrets `REGISTRY_USERNAME` / `REGISTRY_PASSWORD`** (zot user `workbricks-ci`) are on
  `workbricks-old`. Fix: set both on the new repository from `registry.sops.yaml` (also in
  OpenBao `af/estate/registry` `workbricks_ci_password`).

Validation: `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`.

Rollback removes the allowlist entry through review, converges both hosts, and only then removes
repository access and protection. Posted reviews stay.
