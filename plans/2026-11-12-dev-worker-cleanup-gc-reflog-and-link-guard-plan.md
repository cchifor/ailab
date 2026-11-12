# Dev-worker cleanup: gc-proof idle detection and a cross-tree link guard

## Context

On 2026-11-12, `DevWorkerDiskFilling` (critical) fired for 7 h on dev-worker-3 (192.168.0.10). `/workspace` was at
94 %, and `dev-worker-disk-guard` reported EXHAUSTED on every 5-minute tick: each step of its ladder ran and none
reclaimed anything. The investigation found two defects in `ansible/roles/dev_worker/files/cleanup`. They have to
ship together.

**A. `git gc` makes every worktree look active.**
- `scan_worktree` counts the mtime of each worktree's HEAD reflog (`<gitdir>/logs/HEAD`) as activity.
- `git gc --auto` runs `git reflog expire --all`, which rewrites the reflog of EVERY linked worktree of the repository
  at once. That gives each of them a new mtime, even when no entry was dropped.
- On dw3, 282 of the `relay` worktrees' reflogs had mtime 2026-11-12 02:03:24, the same second as the new pack.
  Platform's 76 worktrees all had 2026-10-31 10:31.
- After every gc, nothing is idle for `--deps-days` (the guard passes 3) or for `--merged-days`. The deps step and
  the worktrees step therefore reclaim nothing.
- With the reflog signal ignored, the deps step would have planned 13 worktrees, 10.9 GB, mostly Rust `target/` dirs.

**B. Dependency dirs shared through symlinks are unprotected.**
- The relay agent avoids `npm ci` per task. It symlinks a new worktree's `node_modules` (or each entry in it, plus
  `.vite`/`.vite-temp`) and `holder/target/debug` into an OLDER worktree's dirs.
- Chains exist: `candidate-*/node_modules -> relay-workers-integration/node_modules -> relay-workers/node_modules`.
- The deps step judges a dep dir by its own worktree's idleness, `busy_paths()` (/proc) and container binds. Reading
  through a symlink never touches the target's mtime, and the consumer process runs in ANOTHER worktree. So a shared
  dir that is idle by its own clock looks free.
- A manual `cleanup --deps --deps-days 3` (run with the reflog signal patched out) removed
  `relay-workers/node_modules`. That left 4,793 symlinks in about 60 worktrees dangling, including worktrees created
  that day. It was restored by `npm ci` from the unchanged lockfile, about 15 minutes later.
- The whole-worktree step (`--worktrees`) has the same hole: `git worktree remove` deletes a worktree that other trees
  link into.

**Why together:** today A masks B, because nothing ever looks idle. Fixing A alone would let the guard do
automatically, every 5 minutes, what the manual run did.

**Authority:** the dev-worker infrastructure is frozen until Relay workers are validated. On 2026-11-12 the owner
asked for "the best solution to properly address these two issues". This plan touches the cleanup tool only: no VM,
IP, RAM, threshold or credential change.

## Approach

### A. Activity is the time of the last reflog ENTRY, not the file's mtime

- **New helper `reflog_time(path)`:**
  - It returns the timestamp of the last reflog entry. Each entry has the shape
    `<old> <new> <name> <<email>> <unix-ts> <tz>\t<msg>`.
  - It reads at most the last 4 KiB of a REGULAR file, with the same open discipline as `read_line`: lstat
    regular-file check, `O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, because root reads user-writable trees.
  - It takes the last line whose identity part ends in `<digits> <+|-hhmm>`.
- **Callers:** `scan_worktree` uses `reflog_time` for `logs/HEAD` instead of `mtime`. The index and HEAD files keep
  using mtime: gc does not touch them, and a checkout or commit writes them.
- **Fallbacks:** no parseable entry falls back to the file's mtime (conservative: the old behaviour). A future
  timestamp counts as activity (fresh).
- **Why this signal:** `reflog expire` keeps each surviving entry's own timestamp, so the last entry's time is the last
  HEAD movement in that worktree, and gc cannot change it.
- **Scope:** this changes `scan_worktree` only. Every caller inherits it: `plan_deps`, `remove_deps`'s re-judgement,
  `judge_worktree` and `remove_worktree`.

### B. A dir another tree links into is never removed

- **`link_index(roots, excluded)`, the symlink inventory:**
  - It walks the same trees as `find_worktrees`: regular-user top dirs under each root, same filesystem, no
    `SKIP_NAMES`, no container roots (`container_roots()`).
  - It records every symlink it meets. Inside a dep dir (`dep_kind`) it lists only the direct children, and descends
    one more level only into npm `@scope` dirs. That is where per-entry links live, and it keeps the walk off the
    millions of files inside packages.
  - For each link it records its targets: every hop, made absolute against the `realpath` of the link's own directory
    and normalised (followed while the hop is itself a symlink, at most 40), plus `realpath(link)`.
  - Recording the hops catches chains through an intermediate link. Recording the realpath catches symlinked path
    components.
- **`linked_from(index, path)`:** the links that are not under `path` and have a target under `path`, including
  `path` itself. A dangling link whose target lies under `path` counts too (conservative). A link inside the candidate
  that points into the candidate does not count.
- **Built lazily, once per `scan()`:** only when the deps or worktrees step has at least one candidate. `main()` scans
  twice (the plan, then the fresh re-scan right before `execute`), so the index is at most one scan old at execute
  time. The deps and worktrees steps share one index.
- **Deps step:** `plan_deps` refuses a linked dep dir with
  `Not touching <dir>: linked from N path(s), e.g. <first>`. The worktree's unlinked dep dirs are still planned.
- **Worktrees step:** `plan_worktrees` keeps a linked worktree. The reason is `linked from N path(s), e.g. <first>`,
  listed in the "Kept" output once the worktree is idle past `--worktree-days`.
- **Execute:** `still_planned` already drops a deps action whose dirs the fresh scan no longer plans, and a worktree
  action that is no longer in the fresh plan. Neither `remove_*` function rebuilds the index.
  - Residual race: a link created between the fresh scan and the removal. The removal renames the dir aside
    atomically, so the window is the execution of the items before it. Accepted; documented in the code.
- **Measured cost on dw3 (prototype, read-only):** 3.03 M entries, 23.7 k links, 29 s on a cold cache and 6 s on a
  warm one. It found all 4,793 links from the incident (0 missed).
  - The cost is paid only by runs that have candidates. When every candidate is linked, a guard tick under pressure
    pays one walk per tick.
- **Why not detect consumers some other way:** a symlink is a name, not a reference. Nothing at the target records
  who points at it, and no /proc or mount signal shows it. An inventory is the only sound check.

### Out of scope: capacity, measured afterwards

- **Growth:** worktrees created in the last 3 days held 14.0 GB. About 7.0 GB was dependency or build output, which A
  lets the deps step reclaim after 3 idle days. The other 7.0 GB was source and agent evidence, which goes only when
  the worktree is merged (1 idle day) or stale (30 days with HEAD pushed).
- **What blocks removal of old worktrees today:** among the 146 worktrees idle 14 days or more, the main blockers are
  uncommitted changes (29), shallow repositories (24) and HEAD not pushed (7). Lowering `--worktree-days` from 30 to 7
  adds only 1 stale worktree today.
- **Follow-up:** watch `dev_worker_disk_guard_*` and `dev_worker_dir_bytes` for 7 days after rollout. If dw3 still
  trends under 15 %, the levers are the owner's: the stale threshold, the agent's own worktree hygiene, or a larger
  `/workspace` (infra, frozen).
- **Not changed here:** any threshold, the disk-guard ladder, `DISPOSABLE`.

## Critical files

- `ansible/roles/dev_worker/files/cleanup`
  - `reflog_time`; `scan_worktree` uses it.
  - `link_index` and `linked_from`.
  - `plan_deps` and `plan_worktrees` take a `linked` callable.
  - `main.scan()` builds the index lazily.
  - The module docstring and the `--deps`/`--worktrees` help text: A and B, the residual race.
- `scripts/tests/test_dev_worker_cleanup.py`: new tests (Linux-only, like `DepsTest`/`WorktreesTest`).
  - A: a fresh reflog mtime with an old last entry is idle; a recent last entry is fresh; an unparseable reflog falls
    back to the mtime; a future timestamp is fresh.
  - B:
    - whole-dir link, per-entry link, `@scope/pkg` link, relative link and a two-hop chain → refused;
    - an unlinked sibling dep dir in the same worktree is still planned;
    - a link inside the candidate pointing into itself does not block;
    - a dangling link into the candidate blocks;
    - a worktree linked into is kept by the worktrees step;
    - the fresh re-scan drops a dir linked after the first scan (the execute path).
- `docs/runbooks/dev-workers.md`, disk section: one paragraph on both behaviours and the "linked from" message.

## Verification

1. Unit tests: `python3 -m unittest discover -s scripts/tests -p "test_dev_worker_*.py"` in a Linux container with
   git (`python:3.12` image; mirrors `script-tests.yaml`). All existing tests stay green.
2. CI: script unit tests, plus the PR's reviewbot reviews.
3. Rollout to dw3 first: `ansible-playbook dev-workers.yml -l dev-worker-3 --tags cleanup` from WSL with
   `ANSIBLE_CONFIG` set.
   - Dry run: `sudo cleanup --dry-run --no-docker --deps --worktrees --deps-days 3 --worktree-days 30 --merged-days 1`.
     Expect `relay-workers/node_modules`-style sources reported as "linked from …", and a timing in the measured range.
   - Real run of the guard's own step (`systemctl start dev-worker-disk-guard` when under 15 %, or the cleanup step by
     hand), with a dangling-link census (`find /workspace/c4 /home/c4 -xdev -xtype l`) before and after. Pass: no new
     dangling links.
4. Then roll out to dev-worker-1, -2 and -4 the same way (same tool, no drift between workers). Run a dry run on each.

<!-- codex-review-status: pending -->
