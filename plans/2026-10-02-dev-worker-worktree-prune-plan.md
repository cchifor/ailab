# Dev-worker stale-worktree prune + daily buildx cap

## Codex Review

- Reusing discovery, running Git as the owner, avoiding `--force`, and starting in report mode are good foundations.
- The deletion policy needs clearer protection for ignored files, detached commits, and worktree-local history; clean status and surviving branch refs do not protect everything.
- Remote-tracking refs and a successful `merge-tree` are conditional evidence, and Git inspection can itself change repository state.
- Root-side traversal, incomplete `/proc` visibility, and concurrent activity need explicit handling. The proposed timestamp restoration can conceal real activity.
- Add Git 2.43, privilege, removal, and service-failure tests, especially to ensure the new pass does not disrupt the existing deps prune.

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

<!-- codex: The 25.8 GB result already shows this is a best-effort prune target, not a hard cap. Verify the unit's builder and the measurement source, and account for cache that cannot currently be reclaimed. -->

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

<!-- codex: Also validate the administrative directory's commondir and Git's worktree registration; matching path strings and a backlink alone do not establish the intended common repository. Resolve relative paths against their containing metadata files and reject inconsistent registrations. -->

<!-- codex: The existing git_dir() follows symlinks and opens an unchecked .git entry as root, while path-based walks remain vulnerable to component replacement. Require bounded reads of verified regular metadata files and directory-fd-based, no-follow traversal before trusting these user-writable paths; a FIFO must not hang the service. -->

**Removable** when ALL hold (any failing check → "kept: <reason>", reported, never forced):

1. **Owner**: the worktree dir and the common git dir are owned by the same regular uid (≥1000). All
   git commands run AS THAT USER (`runuser -u <user> --`, clean env, HOME set) — never as root: root
   running git in a user-writable repo executes that user's `core.fsmonitor`/hooks/config.
   Also passed: `-c core.fsmonitor=false -c core.hooksPath=/dev/null`.

   <!-- codex: UID ≥1000 does not establish a usable regular account: resolve and validate the account, including rejecting unmapped or nobody identities. Specify trusted executable paths, a sanitized environment including inherited GIT_* variables, a safe working directory, and no inherited privileged descriptors or interactive stdin; runuser alone does not supply those guarantees. -->

   <!-- codex: runuser requires root, so the promised non-root dry-run needs a separate path that runs directly only for the caller's own worktrees. Reports without full process visibility must identify their incomplete safety assessment. -->

   <!-- codex: Run status and every HEAD-dependent check in the candidate worktree or its per-worktree gitdir; --git-dir=<common> would resolve the main worktree's HEAD/index. Apply the protective Git options consistently to every invocation, including removal. -->

   <!-- codex: Set GIT_OPTIONAL_LOCKS=0 or use --no-optional-locks for inspection: git status can refresh the index and advance its mtime. Otherwise report mode can postpone the following --deps pass, and the execution rescan can reject candidates because cleanup itself made them appear active. -->

2. **Not in use**: `in_use()` (process cwd/open/mapped file, or a running container's bind mount).

   <!-- codex: Root does not guarantee complete /proc visibility, and busy_paths() currently ignores permission/read failures while mount_points() returns an empty set on failure. Distinguish normal process-exit races from unavailable safety evidence and fail closed on the latter. -->

   <!-- codex: Path strings from other mount namespaces and bind aliases need not identify the host worktree path, and querying the default Docker daemon misses rootless Docker or other runtimes. State and verify the supported deployment assumptions instead of treating these helpers as proof that every user of the tree is visible. -->

   <!-- codex: Protect worktrees referenced by surviving containers' compose working-directory labels and consider stopped-container bind mounts, not only running binds. Removing such a directory can break restarts and make a later ordinary cleanup classify the compose stack as orphaned, potentially deleting its named volumes. -->

3. **Idle**: `scan_worktree` last activity older than the rule's threshold (below).

   <!-- codex: In-place writes deep inside node_modules/.venv/target do not necessarily update the dependency directory's own mtime, so the reused scan can miss recent activity. Since whole-tree removal already requires a complete walk, use that walk to detect recent activity inside those directories too. -->

   <!-- codex: Git metadata errors currently fail open: mtime() converts every OSError to None, which scan_worktree treats as timestamp zero. Distinguish legitimately absent metadata from unreadable or inconsistent metadata before using this result to authorize whole-tree deletion. -->

4. **Clean**: `git status --porcelain=v1 --untracked-files=all` is empty (ignored files are allowed —
   they are the deps/build output), no in-progress operation (`MERGE_HEAD`, `CHERRY_PICK_HEAD`,
   `REVERT_HEAD`, `BISECT_LOG`, `rebase-merge/`, `rebase-apply/` in the worktree's gitdir), not
   locked (`<gitdir>/locked`).

   <!-- codex: Ignored files are not necessarily disposable: .env files, local databases, credentials, and manually created assets can all be ignored, and unforced git worktree remove still deletes them. Define an explicit disposable-file policy and retain worktrees containing other ignored data; clean status does not prove the stated no-data-loss condition. -->

   <!-- codex: Empty status can conceal modified files marked assume-unchanged or skip-worktree, including sparse-checkout edge cases. Refuse unsupported index states or verify actual contents without trusting those flags and cached stat information. -->

   <!-- codex: Include sequencer/ and relevant Git lock files in the operation checks: a multi-commit cherry-pick or revert can retain sequence state without the listed HEAD marker. Do not clear stale-looking locks automatically. -->

5. **Safe to walk**: a pre-walk of the tree (also gives the size) finds no nested `.git` (a nested
   clone that is gitignored would otherwise be deleted with the worktree), no mount, and no entry
   owned by another uid (root-owned docker output would make the user's `git worktree remove` fail
   half way).

   <!-- codex: This walk must include all ignored and dependency directories, without the discovery scanner's exclusions or depth limit. Check the worktree root and descendants for mountpoints before entering them, including same-filesystem bind mounts, and reject incomplete inspection. -->

   <!-- codex: Ownership is only a conservative filter: unlink permission depends on containing directories, and same-owner trees can still fail because of modes, ACLs, immutable flags, or read-only mounts. Git removal is not transactional, so anticipate partial deletion, report it accurately, and avoid any force/root retry. -->

6. **Rule** — one of:
   - **merged**: idle ≥ `--merged-days` (default 3) AND the worktree's content is already in the
     remote default branch: `git merge-tree --write-tree <R> HEAD` succeeds with no conflicts and
     its tree equals `<R>^{tree}`, for `<R>` = `refs/remotes/<remote>/HEAD`'s target (fallback
     `<remote>/main`, `<remote>/master`) of any remote. This detects squash merges, which is how
     Gitea merges here (ancestry checks would miss every one). A stale remote ref only makes it
     more conservative.

     <!-- codex: Make the accepted upstream remote explicit; any configured remote can include private forks or local-path repositories. Resolve fallbacks as fully qualified refs/remotes/... names, validate symbolic targets, and pin commit/tree OIDs so branch-name ambiguity or ref movement cannot change the proof mid-check. -->

     <!-- codex: A configured custom merge driver can return the remote side and success while discarding unique HEAD changes, so a no-op merge is not an unconditional content-containment proof. Refuse such configurations or establish a controlled merge policy that cannot silently discard those changes. -->

     <!-- codex: In Git 2.43, --write-tree writes objects into the repository even during --dry-run, including objects produced while calculating conflicts. Isolate those writes or explicitly bound and document the report's disk side effects, especially on nearly full workers. -->

     <!-- codex: Parse Git 2.43's result by exit status and documented output: 0 is clean, 1 is conflicted, and other failures are errors; conflict output can still begin with a valid tree OID. Treat timeouts, malformed output, missing objects, and unsupported Git versions as reasons to keep the worktree. -->

   - **stale**: idle ≥ `--worktree-days` (default 30) AND HEAD is contained in a remote-tracking ref
     (`git for-each-ref --contains HEAD refs/remotes` non-empty) — every commit is on a remote.

     <!-- codex: Both rules inspect local remote-tracking refs, which do not establish that commits remain on a remote server. Staleness is not always conservative after a remote revert, force-push, or deletion; define a freshness policy or explicitly describe the weaker local-ref guarantee without adding an implicit network/authentication dependency. -->

     <!-- codex: Define handling for replacement refs, grafts, and shallow history before treating Git's graph and tree answers as preservation evidence. Disable replacement-object interpretation and conservatively reject unsupported history configurations. -->

**Removal**: right before each one, re-run checks 2–6 with fresh /proc, container and mount state
(as `remove_deps` does), then `runuser -u <user> -- git --git-dir=<common> worktree remove <path>`
— WITHOUT `--force`, so git itself refuses a dirty/untracked/locked/submodule worktree as a second
gate. Then `git worktree prune` once per common dir touched. The branch ref is kept (`worktree
remove` never deletes refs), so nothing reachable is lost; what goes is the working copy and its
ignored files.

<!-- codex: Recheck candidacy and ownership too, and fingerprint the worktree/admin/common directory identities plus HEAD and the proving ref OIDs. Extend still_planned accordingly so a replacement tree or changed repository at the same pathname is not treated as the previously confirmed action. -->

<!-- codex: The cleanup lock excludes only other cleanup removals, and neither repeated checks nor unforced Git removal closes the race with an agent starting work during recursive deletion. Establish coordination that agents honor, or explicitly resolve this residual risk before enabling unattended removal. -->

<!-- codex: Drop the common-directory-wide git worktree prune: successful worktree remove already removes its registration. Prune can affect unrelated missing or temporarily unavailable worktrees that were never displayed or approved, including their administrative history. -->

<!-- codex: Shared branch refs do not preserve detached HEADs, HEAD-reflog-only commits, or per-worktree private refs that disappear with the administrative directory. A squash-merged detached HEAD may have no surviving ref at all; specify retention or refusal rules before claiming nothing reachable is lost. -->

**Report**: the table lists removable worktrees (category "Stale worktree", detail = rule + branch),
then a "Kept" list of worktrees that passed the idle threshold but failed a check, with the reason —
the owner's to-do list. Active worktrees are not listed.

<!-- codex: Report inspection failures separately even when idleness cannot be established; the current last_activity=None conflates activity with unreadability. Otherwise broken registrations and missing safety evidence can disappear from the reports intended to validate rollout. -->

**Fix in `--deps` (needed by A)**: `remove_dep_dir` renames+deletes the dep dir, which bumps the
parent dir's mtime, so `scan_worktree` then sees the worktree as active for another N days and a
worktree whose deps were pruned at day 14 would not reach day 30 until day 44. Restore the parent
dir's atime/mtime (`os.utime(pfd, ns=...)`) after the removal: the removal is not activity.

<!-- codex: Unconditionally restoring timestamps after a potentially long deletion can erase evidence of concurrent real changes to the parent, including sibling deletions. The extra delay is conservative and does not block A; omit this change initially unless a design and race tests establish that it cannot conceal activity, including on partial failure. -->

CLI: `--worktrees`, `--worktree-days N` (30), `--merged-days N` (3). `--no-docker` accepts
`--worktrees` as well as `--deps`. Within one run the worktree pass is planned before deps (no point
pruning the deps of a worktree being removed — dedupe by path).

<!-- codex: Extend every current args.deps gate that also applies to worktrees: root enforcement, watched filesystems, container-root exclusions, discovery, and --no-docker validation, plus action rendering/execution and refusal aggregation. Validate the new day thresholds so invalid values do not silently widen eligibility. -->

<!-- codex: Define deduplication across the initial plan, confirmation rescan, and execution when a whole-worktree action becomes ineligible. Never introduce an unconfirmed deps fallback during execution; the separate later deps invocation can safely make its own fresh plan. -->

### B. Ansible (`roles/dev_worker`)

- `tasks/cleanup.yml`: the `dev-worker-deps-prune.service` gains a FIRST `ExecStart` for the
  worktree pass: `cleanup --y --no-docker --worktrees --worktree-days … --merged-days …` plus
  `--dry-run` while `dev_worker_worktree_prune_mode == "report"`. The existing `--deps` ExecStart
  follows unchanged. (Oneshot; each line takes the lock in turn.) `ExecStart=-` on the report line so
  a report failure never blocks the deps prune.

  <!-- codex: The current main() deliberately skips locking dry-runs, so the report line does not take the lock as claimed. Decide how report scans and their Git writes coexist with a simultaneous manual cleanup. -->

  <!-- codex: The systemd '-' prefix does not ensure continuation after a service startup timeout, and an unignored removal failure or lock-contention exit will block the following deps command. Bound Git subprocesses, their children, and overall worktree-pass runtime, and test a service arrangement that preserves the existing deps pass on these failures. -->

- `defaults/main.yml`: `dev_worker_worktree_prune_mode: report` (report → `remove` after a week of
  clean reports, in a follow-up PR), `dev_worker_worktree_prune_days: 30`,
  `dev_worker_worktree_prune_merged_days: 3`.

  <!-- codex: Assert that mode is an explicitly supported value before rendering the service; appending --dry-run only when mode == "report" would make a typo select deletion. Also document that dev_worker_deps_prune_enabled now controls both passes and provide an operational way to disable the new pass without losing deps pruning. -->

- `tasks/docker.yml`: `docker-buildx-prune.timer` `OnCalendar=*-*-* 03:00` + `RandomizedDelaySec=30m`
  (+ description "Daily"); the comment updated with the 38 GB finding.

### C. Docs

`docs/runbooks/dev-workers.md` § Disk full: replace item 3's "owner's call" with the worktree pass
(rules, report mode, how to read `journalctl -u dev-worker-deps-prune`); buildx is daily.
`cleanup` module docstring + `tasks/cleanup.yml` header comments.

<!-- codex: Document the much shorter three-day merged threshold, git worktree lock as the supported opt-out, and exactly which data can and cannot be recovered after removal. Keep the runbook clear that report mode is the deployed default until the follow-up explicitly enables deletion. -->

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

   <!-- codex: Pin an actual Git 2.43 test environment and exercise real removal, not only classification, including attached/detached HEADs, initialized/uninitialized submodules, protected ignored files, index flags, sequencer state, custom merge drivers, and misleading remote refs. Assert that surviving branches and sibling worktrees remain usable and that report mode does not refresh the index. -->

   <!-- codex: Injecting a current-user runner bypasses runuser entirely, so Linux/WSL alone does not verify privilege switching. Add disposable root integration tests with distinct owners to check UID/GID/environment handling, inaccessible paths, executable lookup, and subprocess timeout behavior. -->

   <!-- codex: Add adversarial failure tests for swapped paths/metadata, symlink or FIFO .git entries, same-device bind mounts, unavailable /proc or Docker evidence, and partial removal. If timestamp restoration remains, test concurrent parent changes rather than only asserting that an unchanged parent's mtime is preserved. -->

   <!-- codex: Exercise combined --worktrees --deps runs and both independent service invocations, covering deduplication, rescans, skipped removals, empty plans, and error propagation. Verify the declared CI job actually discovers these tests and supplies the required Git/Linux environment. -->

2. On one worker (dw1, most candidates): copy the new `cleanup` to /tmp and run
   `sudo /tmp/cleanup --no-docker --worktrees --dry-run`; spot-check 5 "removable" entries by hand
   (`git status`, `git log -1`, branch on remote / content on main) and the "kept" reasons.

   <!-- codex: Stage the privileged smoke-test executable in a unique root-owned location with protected ancestry instead of the predictable /tmp/cleanup path. Extend manual checks to ignored files and worktree-local history, and use disposable repositories for an actual sudo removal rehearsal. -->

3. Roll out with `just` / ansible `--tags cleanup,buildx_prune` to all 4 workers; confirm both
   timers' next run (`systemctl list-timers`) and the unit file content.

   <!-- codex: Before rollout, validate rendered report/remove units with systemd-analyze verify and Ansible syntax/check/diff, including invalid-mode rejection and the selected tags' handler behavior. Test that a failing or timed-out worktree pass still permits the required deps cleanup. -->

4. Next morning: `journalctl -u dev-worker-deps-prune` shows the report and the deps run; the buildx
   journal shows a daily run.

   <!-- codex: Also compare deps eligibility/reclamation with the prior behavior and measure report duration, object-store growth, and reclaimed build-cache space. Successful journal entries alone will not reveal index-mtime interference, excessive repeated full-tree walks, or a prune target that remains unmet. -->

<!-- codex-review-status: complete -->