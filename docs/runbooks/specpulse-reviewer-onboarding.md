# SpecPulse reviewer onboarding

The owner asked on 2026-11-25 for `cchifor/specpulse` to join the review bots: "add support to include
cchifor/specpulse in the reviewers list, so they can review and merge". It gets the same loop as the other admitted
repositories:
- both personas review the current head;
- CI is green;
- the author is allowed;
- `no-automerge` holds a pull request.

Its pull requests come from `workstation-bot`, already a merge author, so admitting it adds the allowlist entry
only. There is no new persona, author exception or merge-gate exception.

## State before admission (2026-11-25)

- **Repository:** private, created 2026-11-22, Python 3.12+ (`pyproject.toml`). `scripts/test.sh` is its single
  check: ruff, then pytest without the `live` and `model` markers.
- **Protection and access:** `main` was unprotected, and any writer could merge. Pull requests #1 to #4 were
  merged by `chifor`. Neither reviewer identity had access.
- **CI:** there was no workflow, and the combined status of `main` was `pending` with no statuses. Reviewbot
  merges only on `success`, so the repository needs a CI workflow before anything can merge through the bots.
- **Checks on a clean Linux checkout of `main` `5fd6680`** (`python:3.13`, editable install):
  - 346 tests passed;
  - ruff 0.17.6 found one error: `I001` in `tests/test_secrets.py`, an import order. `ruff` is not pinned in
    the `dev` extra, so a newer release added it.
- **Two facts the CI workflow must respect:**
  - `scripts/test.sh` is mode 100644 in Git, so run it with `bash`.
  - The package must be installed **editable** (`pip install -e`). `pipeline/golden.py`'s `repo_root()` finds the
    checkout from the module's own path. A plain install puts the module in site-packages, and
    `test_cli_golden_write_refuses_a_target_inside_the_repository` then fails.
- Pull requests #2 to #4 were stacked and merged into their base branches, `feat/plan-1-foundation` to
  `feat/plan-3-agent-layer`, not into `main`. Plans 2 to 4 are therefore not on `main` yet. That is the
  SpecPulse author's to resolve. Admission does not change it.

## Activation

**Access comes before the allowlist.** The allowlist entry takes effect on ANY reviewer-host converge after this
change merges, not only the one in step 3. The repository is private, so if the entry is live before the reviewers
have access, both personas poll a repository they cannot see. Step 2 therefore runs before this change merges, as
it did for `cchifor/workbricks-apps`.

1. **CI first.** A SpecPulse pull request adds `.gitea/workflows/ci.yml`:
   - workflow `CI`, job `verify`, on the organization's `self-hosted-hv` runners, giving the context
     `CI / verify (pull_request)`;
   - it runs `bash scripts/test.sh` in a pinned `python:3.13` container after `pip install -e ".[dev]"`;
   - it also fixes the `I001` import order.
2. **Access and protection, before the allowlist change merges.** An owner operation from the workstation:
   ```bash
   scripts/gitea-owner-op.sh scripts/owner-ops/reviewer-onboard.ops --var REPO=specpulse             # dry run
   scripts/gitea-owner-op.sh scripts/owner-ops/reviewer-onboard.ops --var REPO=specpulse --execute
   ```
   - Read the rule back with `GET /repos/cchifor/specpulse/branches/main`, as `workstation-bot`. Expect
     `protected=true`, `required_approvals=2`, `status_check_contexts=["CI / verify (pull_request)"]`,
     `user_can_push=false` and `user_can_merge=false`.
3. **The allowlist entry** (this change), then a converge of the reviewer hosts. Run from `ansible/`, through the
   container controller: `ansible-playbook reviewers.yml -t reviewbot`.
   - Afterwards, confirm on both hosts that the rendered configuration lists `cchifor/specpulse`. The file holds
     credentials, so check the presence of the name only, never print the file.
4. **Observe the first pull request,** the CI one from step 1: a green `CI / verify (pull_request)`, both
   personas' verdicts at its current head, and a merge by a reviewer identity.

## Activation record (2026-11-25)

- **Step 1:** [specpulse#5](https://git.chifor.me/cchifor/specpulse/pulls/5). Its exact command was run first on a
  clean archive of its head `860ede2`, with a `.git` as the checkout leaves it: 346 passed, 2 deselected, ruff clean.
- **Step 2:** applied at 20:58:55Z, before this change merged, from the preset at this change's reviewed head:
  `gitea-owner-op.sh scripts/owner-ops/reviewer-onboard.ops --var REPO=specpulse --execute`.
  - Results: 204, 204, 201. The token was deleted, its next use answered 401, and `chifor` holds no token.
  - Read-back as `workstation-bot`: `protected=true`, `required_approvals=2`,
    `status_check_contexts=["CI / verify (pull_request)"]`, `user_can_push=false`, `user_can_merge=false`.
  - The `protected_branch` row is identical, field for field, to `cchifor/workbricks-apps`'s.
  - Collaborators: `reviewer-claude` and `reviewer-codex`, mode 2 (write), and no one else.

Rollback, in this order:
1. remove the allowlist entry through review;
2. converge both hosts;
3. only then remove the protection and the reviewers' access.

Posted reviews stay.

Validation:
- `python3 -m unittest discover -s scripts/tests -p test_reviewbot.py`
- `python3 -m unittest discover -s scripts/tests -p test_gitea_owner_op.py`
