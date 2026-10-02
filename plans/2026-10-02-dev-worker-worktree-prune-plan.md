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
2. **The buildx prune is weekly.** `docker-buildx-prune.timer` (Sun 03:00, `--max-used-space 20GB`)
   lets the cache grow between runs: dw1 sat at 38 GB; a manual run of the same unit on 2026-10-02
   took it to 25.8 GB (`docker buildx du`, default builder = the docker driver, the one the unit
   prunes). `--max-used-space` is a best-effort target, not a hard cap — records still referenced
   are kept — so the runbook says "target". Daily is the cheapest reclaim in this plan.

## Approach

### A. `cleanup --worktrees` (same tool, `ansible/roles/dev_worker/files/cleanup`)

A new opt-in pass, root only for removal (like `--deps`, and for the same reason: only root sees
every process in /proc). It reuses the `--deps` machinery: `find_worktrees` (discovery under
`--deps-roots`, container storage excluded), `scan_worktree` (last activity), `busy_paths` /
`mount_points` (in-use), the lock, plan → confirm → re-scan → execute. A non-root `--dry-run` runs
git directly for the caller's own worktrees only and prints that the in-use check is partial.

**Candidate** = a *linked* worktree only. Main clones (`.git` dir) and submodules (`.git` file into
`.git/modules/`) are skipped silently. Metadata is read only from regular files (lstat first —
a FIFO or symlink `.git`/`gitdir`/`commondir` is refused, never opened), bounded to one line:
- `<wt>/.git` → `gitdir: <common>/worktrees/<id>`; `<id>/gitdir` points back at `<wt>/.git`;
  `<id>/commondir` resolves to `<common>`;
- and git agrees: `git worktree list --porcelain` (run in the worktree) lists `<wt>`.
A broken/moved registration is reported, not touched.

**Removable** when ALL hold (any failing check → "kept: <reason>"; never forced):

1. **Owner**: the worktree dir and the common git dir belong to the same account, resolved with
   `pwd` (uid ≥1000, not `nobody`). Every git command runs AS THAT USER — root via an absolute
   `/usr/sbin/runuser -u <user> --` — never as root: root running git in a user's repo would run
   that user's fsmonitor, hooks and config with root's rights. Built env only (no inherited
   `GIT_*`): `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0`,
   `GIT_OPTIONAL_LOCKS=0` (status must not refresh the index — that write would be "activity"),
   `GIT_NO_REPLACE_OBJECTS=1`; `-c core.fsmonitor=false -c core.hooksPath=/dev/null` on every call
   including the removal; stdin `/dev/null`; cwd = the worktree; 300 s timeout per call.
2. **Not in use**: a process cwd/open/mapped file under it (`busy_paths`), a mount under it, or ANY
   container — running or stopped — that bind-mounts it or carries it as its compose
   `working_dir` label (a stopped stack whose directory vanished would later look orphaned to
   `cleanup`, which then deletes its named volumes).
3. **Idle**: `scan_worktree` last activity older than the rule's threshold; and the full pre-walk
   (5) finds no entry, dependency dirs included, newer than that threshold (in-place writes deep in
   node_modules don't touch the dir's own mtime). The worktree's gitdir `HEAD` must exist and be
   readable — unreadable metadata is "kept", not "old".
4. **Clean, by git**: `git status --porcelain=v1 --untracked-files=all` empty; no assume-unchanged
   or skip-worktree entries (`git ls-files -v`: lowercase or `S` tags — they hide edits from status;
   covers sparse checkouts); no `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `BISECT_LOG`,
   `rebase-merge/`, `rebase-apply/`, `sequencer/`, `index.lock` in the worktree's gitdir; not
   locked (`locked`); no per-worktree refs (`refs/` under the worktree's gitdir, `refs/worktree/*`,
   `refs/bisect/*`); the repository is not shallow.
5. **Nothing else to lose**: a full walk of the tree (no depth limit, dependency dirs included,
   lstat only) finds no nested `.git` (a gitignored clone would go with it), no mount point, no entry
   owned by another uid, and only DISPOSABLE ignored content. Ignored content is listed with
   `git ls-files --others --ignored --exclude-standard --directory`; disposable = a path any of whose
   components is one of: `node_modules`, `.venv`/`venv` (with `pyvenv.cfg`), `target` (beside
   `Cargo.toml`), `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`,
   `dist`, `build`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`, `.parcel-cache`, `.vite`, `coverage`,
   `htmlcov`, `playwright-report`, `test-results`, `.terraform`, `.eslintcache`, `*.egg-info`, or a
   file ending `.pyc`, `.tsbuildinfo`, `.log`. Anything else ignored (`.env`, `*.db`, `secrets/`…) →
   kept, naming the first such path.
6. **Rule** — one of:
   - **merged**: idle ≥ `--merged-days` (default 3) AND the worktree's content is already in an
     upstream default branch: `git merge-tree --write-tree <R> HEAD` exits 0 (1 = conflict, else
     error — both "not merged") and its first line equals `<R>^{tree}`. `<R>` = the target of
     `refs/remotes/<r>/HEAD`, else `refs/remotes/<r>/main`, `…/master`, fully qualified, for remotes
     whose URL is a network URL — `https://`, `http://`, `ssh://`, `git://` or scp-style `user@host:` —
     a local path or `file://` remote proves nothing (tested for both rules).
     Refused when any `merge.*.driver` is configured (a custom driver can return "ours" and hide
     changes). Detects squash merges, which is how Gitea merges here. `--write-tree` writes the
     merged tree's objects into the repo (loose, small, gc'd); accepted and documented.
   - **stale**: idle ≥ `--worktree-days` (default 30) AND HEAD is contained in a remote-tracking ref
     of such a network remote (`git for-each-ref --contains HEAD refs/remotes/<r>/`).
   Both read LOCAL remote-tracking refs: the guarantee is "was on the forge as of the last fetch",
   no network call. Documented as such.

**Removal**: plan fingerprint = (dev, ino) of the worktree dir + HEAD OID. Right before each one,
re-run every check with fresh /proc, container and mount state, require the same fingerprint, then
`git worktree remove <path>` (run in the common dir, no `--force`) as the owner, so git itself
refuses a dirty/untracked/locked/submodule worktree a second time. No `git worktree prune` (it
would touch registrations nobody approved). Git's removal is not transactional: a failure is
reported as a failure with git's message; never retried with force or as root. The branch ref
stays. What goes: the working copy, its disposable ignored files, the worktree's HEAD reflog.

**Report**: the table lists removable worktrees (category "Stale worktree", detail = rule + branch).
Worktrees idle ≥ `--worktree-days` that are not removable are printed as "Kept worktree … — reason":
the owner's to-do list. Active or merely-unmerged younger worktrees are not listed.

CLI: `--worktrees`, `--worktree-days N` (30), `--merged-days N` (3); both ≥1 and merged ≤ worktree.
Every `--deps` gate now also applies to `--worktrees` (root for removal, watched filesystems,
container-root exclusion, `--no-docker` validation). In one run the worktree pass is planned first
and the deps pass skips worktrees planned for whole removal; execution never adds actions.

### B. Ansible (`roles/dev_worker`)

- `tasks/cleanup.yml`: assert `dev_worker_worktree_prune_mode in [off, report, remove]`. The
  `dev-worker-deps-prune.service` gains a FIRST step for the worktree pass unless mode is `off`:
  `ExecStart=-/usr/bin/timeout --kill-after=2m 45m /usr/local/bin/cleanup --y --no-docker --worktrees …` plus
  `--dry-run` in `report` mode. `-` + its own `timeout`: a failing, contended or hung worktree pass
  never stops the existing `--deps` step that follows; the unit's `TimeoutStartSec=2h` leaves
  ≥73 min for it (it takes seconds to minutes today). `dev_worker_deps_prune_enabled` still
  enables the timer for both; `mode: off` disables only the new step.
- `defaults/main.yml`: `dev_worker_worktree_prune_mode: report` (→ `remove` in a follow-up PR after
  a week of reviewed reports), `dev_worker_worktree_prune_days: 30`,
  `dev_worker_worktree_prune_merged_days: 3`.
- `tasks/docker.yml`: `docker-buildx-prune.timer` daily (`*-*-* 03:00`, `RandomizedDelaySec=30m`).

### C. Docs

`docs/runbooks/dev-workers.md` § Disk full: the worktree pass (both rules, the 3-day merged
threshold, disposable-ignored list, `git worktree lock <path>` as the opt-out, report → remove,
what is and is not recoverable: commits on branches and the forge stay; a worktree with
uncommitted, untracked or non-disposable ignored files is kept; the worktree's HEAD reflog IS
deleted, so commits that only it referenced (an earlier detached HEAD, a pre-rebase state) become
unreachable and are eventually garbage-collected; and work written into the worktree during the
seconds of the removal itself — after the last check — can be lost (accepted residual, below)),
buildx daily with a best-effort target. The tool's docstring and the task comments.

## Critical files

- `ansible/roles/dev_worker/files/cleanup` — the pass, CLI, safe metadata reads.
- `scripts/tests/test_dev_worker_cleanup.py` — tests (below).
- `ansible/roles/dev_worker/tasks/cleanup.yml`, `tasks/docker.yml`, `defaults/main.yml`.
- `docs/runbooks/dev-workers.md`.

## Verification

1. Unit tests with REAL git 2.43 repos (WSL Ubuntu 24.04 = the workers' git), real `git worktree
   remove`: linked vs main clone vs submodule; dirty / untracked / disposable-ignored /
   non-disposable-ignored (`.env`); assume-unchanged and skip-worktree; rebase / sequencer state;
   locked; per-worktree refs; shallow; nested ignored clone; recent write deep in node_modules;
   local-only HEAD; local-path remote; squash-merged vs partly merged; custom merge driver; moved
   worktree; FIFO and symlink `.git`; compose working_dir / stopped-container bind; re-check at
   execute (dirty, busy, replaced dir, moved HEAD); removal keeps the branch and a sibling worktree
   usable; status does not touch the index; the root branch of `run_git` builds an absolute runuser
   command with the built env (mocked subprocess); combined `--worktrees --deps` dedup.
2. Real privilege path, read-only: `sudo cleanup --no-docker --worktrees --dry-run` on all 4
   workers (exercises runuser + git as `c4`), from a root-owned `mktemp -d` copy, not a fixed /tmp
   name; spot-check 5 "merged" entries by hand (`git diff --stat origin/main...HEAD` empty or
   content on main) and the "kept" reasons.
3. Real removal rehearsal on dw1 with a disposable repo + worktree owned by `c4`: dirty → kept,
   clean+merged → removed by `sudo cleanup --worktrees --merged-days 1 --deps-roots <tmp>`.
4. `systemd-analyze verify` the rendered units (report and remove); roll out with ansible
   `--tags cleanup,buildx_prune` on all 4; `systemctl cat` + `list-timers`.
5. Next morning: the deps step still ran (its freed bytes vs the previous day), the report's
   duration and candidate count, and `docker buildx du` after the buildx run.

## Residual risks

- **Removal race (accepted, documented).** Work an agent writes into a worktree after the final
  checks — git's own included — and during the seconds of the recursive delete can be lost. The
  worktree has been idle 3+ days and is re-checked immediately before; `git worktree lock` opts a
  worktree out before removal (not during). No cross-agent locking protocol. The runbook states it.
- Process visibility (`busy_paths` read errors = exit races; no hidepid here), mount namespaces
  and rootless runtimes (one rootful dockerd per worker), root-side path swaps (root only reads),
  and the Kept-list scope (idle ≥ `--worktree-days` only) were raised in round 1 and settled in
  round 2.

<!-- codex-review-status: finalized -->