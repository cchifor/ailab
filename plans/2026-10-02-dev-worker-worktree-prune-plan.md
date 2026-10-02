# Dev-worker stale-worktree prune + daily buildx cap

## Context

The dev-workers' `/workspace` (125 GB, scsi1) holds both the agents' git worktrees and the
docker/containerd data-root. ailab#1005 (2026-10-01) added `sudo cleanup --deps` and the daily
`dev-worker-deps-prune` timer, which removes node_modules/.venv/target from worktrees idle 14+ days.
That fixed the 100%-full incident, but two growth sources remain (survey 2026-10-02):

| worker | linked worktrees | idle ≥14d + clean + HEAD on a remote ref | build cache |
|---|---|---|---|
| dw1 | 151 (25 GB) | 58 (5.4 GB) | 38 GB (cap is 20 GB) |
| dw2 | 28 (5 GB) | 0 | 0 |
| dw3 | 190 (36 GB) | 8 (0.3 GB) | 4.7 GB |
| dw4 | 105 (22 GB) | 8 (1 GB) | — |

1. **Worktrees are never removed.** Agents create one per task and leave it behind (dw3: 190). The
   runbook currently says "Deleting whole worktrees is the owner's call, not the operator's" — this
   plan reverses that for worktrees that provably hold nothing that is not already in git.
2. **The buildx cap is weekly.** `docker-buildx-prune.timer` (Sun 03:00, `--max-used-space 20GB`)
   lets the cache grow ~18 GB between runs: dw1 sat at 38 GB; a manual run of the same unit on
   2026-10-02 took it to 25.8 GB. Running it daily is the cheapest reclaim in this plan.

## Approach

### A. `cleanup --worktrees` (same tool, `ansible/roles/dev_worker/files/cleanup`)

A new opt-in pass, root only for removal (like `--deps`, and for the same reason: only root sees
every process in /proc). It reuses the `--deps` machinery: `find_worktrees` (discovery under
`--deps-roots`, container storage excluded), `scan_worktree` (last activity, dep dirs ignored),
`busy_paths` / `bind_mount_sources` / `mount_points` (in-use), the lock, plan → confirm → re-scan →
execute.

**Candidate** = a *linked* worktree only: `.git` is a FILE whose `gitdir:` points at
`<common>/worktrees/<id>`, and `<common>/worktrees/<id>/gitdir` points back at `<worktree>/.git`
(a broken/moved link is reported, not touched). Main clones (`.git` dir) are never candidates.

**Removable** when ALL hold (any failing check → "kept: <reason>", reported, never forced):

1. **Owner**: the worktree dir and the common git dir are owned by the same regular uid (≥1000). All
   git commands run AS THAT USER (`runuser -u <user> --`, clean env, HOME set) — never as root: root
   running git in a user-writable repo executes that user's `core.fsmonitor`/hooks/config.
   Also passed: `-c core.fsmonitor=false -c core.hooksPath=/dev/null`.
2. **Not in use**: `in_use()` (process cwd/open/mapped file, or a running container's bind mount).
3. **Idle**: `scan_worktree` last activity older than the rule's threshold (below).
4. **Clean**: `git status --porcelain=v1 --untracked-files=all` is empty (ignored files are allowed —
   they are the deps/build output), no in-progress operation (`MERGE_HEAD`, `CHERRY_PICK_HEAD`,
   `REVERT_HEAD`, `BISECT_LOG`, `rebase-merge/`, `rebase-apply/` in the worktree's gitdir), not
   locked (`<gitdir>/locked`).
5. **Safe to walk**: a pre-walk of the tree (also gives the size) finds no nested `.git` (a nested
   clone that is gitignored would otherwise be deleted with the worktree), no mount, and no entry
   owned by another uid (root-owned docker output would make the user's `git worktree remove` fail
   half way).
6. **Rule** — one of:
   - **merged**: idle ≥ `--merged-days` (default 3) AND the worktree's content is already in the
     remote default branch: `git merge-tree --write-tree <R> HEAD` succeeds with no conflicts and
     its tree equals `<R>^{tree}`, for `<R>` = `refs/remotes/<remote>/HEAD`'s target (fallback
     `<remote>/main`, `<remote>/master`) of any remote. This detects squash merges, which is how
     Gitea merges here (ancestry checks would miss every one). A stale remote ref only makes it
     more conservative.
   - **stale**: idle ≥ `--worktree-days` (default 30) AND HEAD is contained in a remote-tracking ref
     (`git for-each-ref --contains HEAD refs/remotes` non-empty) — every commit is on a remote.

**Removal**: right before each one, re-run checks 2–6 with fresh /proc, container and mount state
(as `remove_deps` does), then `runuser -u <user> -- git --git-dir=<common> worktree remove <path>`
— WITHOUT `--force`, so git itself refuses a dirty/untracked/locked/submodule worktree as a second
gate. Then `git worktree prune` once per common dir touched. The branch ref is kept (`worktree
remove` never deletes refs), so nothing reachable is lost; what goes is the working copy and its
ignored files.

**Report**: the table lists removable worktrees (category "Stale worktree", detail = rule + branch),
then a "Kept" list of worktrees that passed the idle threshold but failed a check, with the reason —
the owner's to-do list. Active worktrees are not listed.

**Fix in `--deps` (needed by A)**: `remove_dep_dir` renames+deletes the dep dir, which bumps the
parent dir's mtime, so `scan_worktree` then sees the worktree as active for another N days and a
worktree whose deps were pruned at day 14 would not reach day 30 until day 44. Restore the parent
dir's atime/mtime (`os.utime(pfd, ns=...)`) after the removal: the removal is not activity.

CLI: `--worktrees`, `--worktree-days N` (30), `--merged-days N` (3). `--no-docker` accepts
`--worktrees` as well as `--deps`. Within one run the worktree pass is planned before deps (no point
pruning the deps of a worktree being removed — dedupe by path).

### B. Ansible (`roles/dev_worker`)

- `tasks/cleanup.yml`: the `dev-worker-deps-prune.service` gains a FIRST `ExecStart` for the
  worktree pass: `cleanup --y --no-docker --worktrees --worktree-days … --merged-days …` plus
  `--dry-run` while `dev_worker_worktree_prune_mode == "report"`. The existing `--deps` ExecStart
  follows unchanged. (Oneshot; each line takes the lock in turn.) `ExecStart=-` on the report line so
  a report failure never blocks the deps prune.
- `defaults/main.yml`: `dev_worker_worktree_prune_mode: report` (report → `remove` after a week of
  clean reports, in a follow-up PR), `dev_worker_worktree_prune_days: 30`,
  `dev_worker_worktree_prune_merged_days: 3`.
- `tasks/docker.yml`: `docker-buildx-prune.timer` `OnCalendar=*-*-* 03:00` + `RandomizedDelaySec=30m`
  (+ description "Daily"); the comment updated with the 38 GB finding.

### C. Docs

`docs/runbooks/dev-workers.md` § Disk full: replace item 3's "owner's call" with the worktree pass
(rules, report mode, how to read `journalctl -u dev-worker-deps-prune`); buildx is daily.
`cleanup` module docstring + `tasks/cleanup.yml` header comments.

## Critical files

- `ansible/roles/dev_worker/files/cleanup` — the pass, the utime fix, CLI.
- `scripts/tests/test_dev_worker_cleanup.py` — tests (below).
- `ansible/roles/dev_worker/tasks/cleanup.yml`, `tasks/docker.yml`, `defaults/main.yml`.
- `docs/runbooks/dev-workers.md`.

## Verification

1. Unit tests with REAL git repos in a tempdir (the git runner is injectable; tests run git as the
   current user instead of `runuser`): linked vs main clone; dirty / untracked / ignored-only;
   in-progress rebase; locked; nested ignored clone refused; HEAD only local vs on a remote ref;
   squash-merged (merge-tree no-op) vs unmerged; idle thresholds for both rules; broken backlink;
   re-check at execute drops a worktree touched or made dirty after the scan; deps removal keeps
   the parent mtime. `python -m unittest discover -s scripts/tests -p "test_dev_worker_cleanup.py"`
   (Linux/WSL — git + runuser semantics) and the CI job `broker-inventory` runs the whole dir.
2. On one worker (dw1, most candidates): copy the new `cleanup` to /tmp and run
   `sudo /tmp/cleanup --no-docker --worktrees --dry-run`; spot-check 5 "removable" entries by hand
   (`git status`, `git log -1`, branch on remote / content on main) and the "kept" reasons.
3. Roll out with `just` / ansible `--tags cleanup,buildx_prune` to all 4 workers; confirm both
   timers' next run (`systemctl list-timers`) and the unit file content.
4. Next morning: `journalctl -u dev-worker-deps-prune` shows the report and the deps run; the buildx
   journal shows a daily run.

<!-- codex-review-status: pending -->
