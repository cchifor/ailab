# Dev-worker cleanup: gc-proof idle detection and a cross-tree link guard

## Context

On 2026-11-12, `DevWorkerDiskFilling` (critical) fired for 7 h on dev-worker-3 (192.168.0.10). `/workspace` was at
94 %, and `dev-worker-disk-guard` reported EXHAUSTED on every 5-minute tick: each step of its ladder ran and none
reclaimed anything. The investigation found two defects in `ansible/roles/dev_worker/files/cleanup`. They have to
ship together.

**A. `git gc` makes every worktree look active.**
- `scan_worktree` counts the mtime of each worktree's HEAD reflog (`<gitdir>/logs/HEAD`) as activity.
- When `git gc --auto` actually does housekeeping, it runs `git reflog expire --all`. That covers every linked
  worktree unless `--single-worktree` is given, and it rewrites each reflog file even when no entry is dropped, so
  every file gets a new mtime. (`--auto` often exits without doing anything.)
- On dw3, 282 of the `relay` worktrees' reflogs had mtime 2026-11-12 02:03:24, the same second as the new pack.
  Platform's 76 worktrees all had 2026-10-31 10:31.
- After every gc that ran, nothing is idle for `--deps-days` (the guard passes 3) or for `--merged-days` (1). The
  deps step and the worktrees step therefore reclaim nothing.
- With the reflog signal ignored, the deps step would have planned 13 worktrees, 10.9 GB, mostly Rust `target/` dirs.

**B. Dependency dirs shared through symlinks are unprotected.**
- The relay agent avoids `npm ci` per task. It symlinks a new worktree's `node_modules` (or each entry in it, plus
  `.vite`/`.vite-temp`) and `holder/target/debug` into an OLDER worktree's dirs.
- Chains exist: `candidate-*/node_modules -> relay-workers-integration/node_modules -> relay-workers/node_modules`.
- The deps step judges a dep dir by its own worktree's idleness, `busy_paths()` (/proc) and container binds. Reading
  through a symlink never touches the target's mtime, and an idle consumer has no open file. So a shared dir that is
  idle by its own clock looks free. `busy_paths()` does catch a consumer that is RUNNING, through the resolved paths
  of its open and mapped files, but it cannot see a link that is only waiting to be used.
- A manual `cleanup --deps --deps-days 3` (run with the reflog signal patched out) removed
  `relay-workers/node_modules`. That left 4,793 symlinks in about 60 worktrees dangling, including worktrees created
  that day. It was restored by `npm ci` from the unchanged lockfile, about 15 minutes later.
- The whole-worktree step (`--worktrees`) has the same hole: `git worktree remove` deletes a worktree that other trees
  link into.

**Why together:** today A masks B, because nothing ever looks idle. Fixing A alone would let the guard do
automatically, every 5 minutes, what the manual run did.

**Authority:** the dev-worker infrastructure is frozen until Relay workers are validated. On 2026-11-12 the owner
asked for "the best solution to properly address these two issues". This plan touches the cleanup tool, plus a
canary that pauses dw3's two reclaim timers during its rollout. No VM, IP, RAM, threshold or credential changes.

## Approach

### A. Activity is the newest reflog ENTRY time, not the file's mtime

`reflog_time(path)` returns one of three things:
- the activity time, in Unix seconds;
- `0`, meaning no reflog activity;
- `None`, meaning unknown. The caller treats unknown as activity now, so the worktree counts as fresh.

`scan_worktree` uses it for `logs/HEAD`. The index and `HEAD` files keep using mtime: gc does not touch them, and a
checkout or commit writes them.

- **Read discipline.** Root reads files in trees the user can write, so:
  - Open with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, then `fstat` the DESCRIPTOR and require a regular file.
    The decision about what was read rests on the descriptor, not on an earlier `lstat`. Fallback metadata (size,
    mtime) comes from that same `fstat`.
  - Read at most the last 64 KiB.
  - `ENOENT` returns 0, as today: a worktree created without a reflog has no reflog activity. Any other error, or a
    file that is not regular (FIFO, device, a symlink refused by `O_NOFOLLOW`), returns `None`.

<!-- codex: Copying `read_line` is insufficient: its lstat/open race permits replacement after the type check, and O_NOFOLLOW protects only the final component of the user-controlled gitdir/logs path. Pin parent directories without following symlinks and fstat the opened descriptor before seeking or reading; obtain fallback metadata from that same descriptor. -->
<!-- opus-pushback: Accepted the fstat-on-descriptor part (above). Not pinning parent components. The gitdir path comes from the worktree's own `.git` file, which only that tree's owner can write, and the result is only a timestamp that decides that same owner's tree. A redirected parent can make the owner's own tree look older or newer, which the owner can already do by editing it; it can neither read another user's data back nor touch another user's tree. `O_NONBLOCK` plus the regular-file fstat already rule out the FIFO hang and device opens. -->

- **Parsing.**
  - A record is `<old-oid> <new-oid> <name> <<email>> <unix-ts> <+|-hhmm>`, then optionally `\t<message>`. The tab is
    omitted when there is no message.
  - The identity part is the text before the first tab, or the whole record when there is no tab. It must match
    `^([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64}) .* (\d{1,12}) [+-]\d{4}$`, which allows SHA-1 or SHA-256
    oids.
  - The timestamp is the Unix-seconds field. No timezone arithmetic is applied.
  - Only COMPLETE records count, which means records ending in `\n`.
  - When the read started mid-file, the leading fragment is discarded.
  - When the file does not end in `\n`, an append is in progress: return `None` (fresh).
- **Value.**
  - The result is the MAXIMUM timestamp over all valid complete records in the tail, not simply the last one.
    Committer dates are not monotonic, and a recent entry followed by an older-dated one must still count.
  - If the LAST complete record is malformed, or no complete record fits in 64 KiB, fall back to the descriptor's
    mtime. That is conservative: the old behaviour. A malformed tail must never cause an older valid entry to be
    selected.
  - An empty file (size 0) returns 0. `reflog expire` empties a log only when every entry is past `gc.reflogExpire`
    (90 days) or `gc.reflogExpireUnreachable` (30 days), so the last HEAD movement is at least that old. Using the
    file's mtime here would keep the false freshness that A fixes. The index, `HEAD` and file mtimes still count.
  - A timestamp later than now plus 5 minutes is treated as now (fresh).
- **What the value means.** It is the time of the newest surviving reflog entry. Expiry drops old entries but never
  changes the timestamps of the ones it keeps. Entry times are committer-identity times, so an owner can fake them
  (`GIT_COMMITTER_DATE`). That affects only the owner's own tree. It is documented in the docstring.
- **Callers.** This changes `scan_worktree` only. `plan_deps`, the re-judgement in `remove_deps`, `judge_worktree` and
  `remove_worktree` all inherit it.

### B. A dir another tree links into is never removed

#### `link_index(roots, excluded)`: a complete symlink inventory

- **Roots: always `DEPS_ROOTS ∪ --deps-roots`, independent of the deletion roots.**
  - `disk-guard` passes `--deps-roots` with only the filesystems that are under pressure (`disk-guard:321`). A `/home`
    consumer of a `/workspace` dir must still be seen.
  - `--deps-roots` keeps deciding only where candidates come from.
- **Coverage: everything below each root**, on the root's filesystem, at any depth, whoever owns it.
  - That includes dependency dirs and tool caches, walked in full.
  - Skipped: `.git` dirs (git's own bookkeeping) and the container roots (`container_roots()`).
  - Not covered: a consumer on ANOTHER filesystem mounted below a root. That is stated in the docstring.
  - Measured on dw3 (prototype, read-only): 3.9 M entries, 520 k dirs, 37.6 k links. 7 s warm, 154 s right after a
    page-cache drop.
- **Traversal: descriptor-relative.**
  - Each dir is opened with `openat(parent_fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)` and listed with
    `scandir(fd)`. A dir swapped for a symlink fails the open instead of being followed.
  - Entry types come from `d_type` (`DirEntry.is_symlink` and `is_dir(follow_symlinks=False)`), so there is no
    per-entry `stat`.
  - A dir whose `fstat` shows another device is not entered.
  - When an open fails with `ELOOP` or `ENOTDIR` (the entry changed type), the entry is re-checked with `lstat` through
    the dir fd. If it is now a symlink, it is recorded.
- **Errors fail closed.**
  - `ENOENT` (an entry that vanished) is ignored.
  - Any other error leaves the index `incomplete`, holding the first few paths and errors. An incomplete index makes
    the run remove NOTHING from the deps and worktrees steps, and it says so:
    `link inventory incomplete (N errors, e.g. <path>: <err>): not removing anything`. Docker categories are unaffected.

#### Resolving each link

- **A bounded resolver of our own**, component by component, like `posixpath.realpath`, with no
  `os.path.realpath` call.
  - At most 40 symlink expansions in total.
  - A relative target resolves against the RESOLVED parent dir.
  - A `..` applies to the resolved path, never lexically before earlier components are resolved.
- **For each link it records the paths its resolution TRAVERSED:**
  - every symlink location met, including the link itself and any symlinked path component on the way;
  - the final path.
- **When resolution fails** (dangling tail, a loop, too many expansions, an error), it records what it traversed so far
  plus the unresolved remainder appended to the resolved prefix. Unresolved never counts as "references nothing".
  - Resolution reads link targets and checks existence and type with `lstat` and `readlink`. It never opens a
    target's contents.

#### `linked_from(index, C, W)`

`C` is the candidate (a dep dir, or a whole worktree). `W` is the worktree that holds `C` (`W == C` for a whole
worktree). The function returns the links not under `C` with a traversed path `T` such that either:
- `under(T, C)`: into the candidate, or the candidate itself; or
- `under(C, T) and under(T, W)`: `T` is an ancestor of `C` inside its worktree. Example: `consumer/lib -> W`, used as
  `lib/node_modules/x`.

`under` compares on path-component boundaries, as it does today.

<!-- codex: A link to an ancestor also exposes the candidate: consumer/repo -> source-worktree allows consumer/repo/node_modules to use a dependency that this predicate would delete. Conservatively protect descendants of linked directory targets as well, with component-boundary tests to avoid confusing sibling prefixes. -->
<!-- opus-pushback: Accepted for ancestors from C up to its worktree root W, the `consumer/repo -> source-worktree` case. Not for ancestors ABOVE W: a convenience link such as `~/ws -> /workspace/c4` or `~/work -> /workspace` would mark every candidate on the box as linked, and nothing would be reclaimed again. A consumer reaching C through such a link does it by a path whose resolution traverses that link and ends under C, and that resolution is already recorded. -->

- A link inside `C` that points into `C` does not count.
- A dangling link whose traversed path is under `C` does count (conservative).

#### Using the index

- **Built lazily, once per `scan()`**, and only when the deps or worktrees step has at least one candidate. The deps
  and worktrees steps share it within one process.
  - `disk-guard` runs the worktrees and deps steps as SEPARATE `cleanup` processes, each with a plan scan and a fresh
    scan. A tick under pressure with candidates in both steps can therefore walk up to 4 times: about 30 s warm, about
    10 min worst case cold.
  - The guard's timer is `OnUnitInactiveSec`, so ticks never overlap. The step timeout is 2700 s.
  - The canary measures the whole ladder.
- **Deps step:** `plan_deps` refuses a linked dep dir with `linked from N path(s), e.g. <first>`. The worktree's
  unlinked dep dirs are still planned.
- **Worktrees step:** `plan_worktrees` keeps a linked worktree. A kept worktree that is merely linked keeps its unlinked
  dep dirs eligible for the deps step.
- **Reporting.** Every run prints, after the per-item lines and before the final summary,
  `Linked from other trees, not removed: N dep dir(s), M worktree(s) (e.g. <candidate> <- <link>)`. That puts it in the
  last 15 lines `disk-guard` keeps. A linked worktree is listed whatever its idle age, so a linked merged worktree is
  never retained silently.
- **Freshness at execute time.** The index records its start time. `execute()` rebuilds it before an action when it is
  older than 120 s, and skips the action if the fresh index shows a link into it. Same as today, `still_planned` is
  all-or-nothing for a deps action: if one of its dirs gains a link in the fresh scan, the whole action is skipped this
  run. That is intended and tested.
- **What is guaranteed, precisely.** There is no removal of a dir or worktree that a symlink on the inventoried
  filesystems resolved through when an index no older than 120 s plus one walk was taken.
  - What remains is a link created after that, to a candidate. Such a link would point to a dir that has already
    been, or is being, deleted.
  - The consequences are bounded by what is eligible at all. Deps are regenerable (`npm ci`, `cargo build`). A whole
    worktree is eligible only if it is clean and merged, or clean with its HEAD on a forge branch, so it can be
    recovered from the forge. The docstring states this.
  - It does not rely on the atomic rename for consumer safety.

### Out of scope: capacity, measured afterwards

- **Growth:** worktrees created in the last 3 days held 14.0 GB. About 7.0 GB was dependency or build output and
  7.0 GB was source and agent evidence.
- **These are potential amounts, not expected reclaim:** busy processes, links (B) and the existing safety checks will
  retain some of them. The canary dry run (Verification 4) records the bytes actually eligible with A+B, and the PR
  reports it.
- **What blocks old worktrees today:** among the 146 worktrees idle 14 days or more, the main blockers are uncommitted
  changes (29), shallow repositories (24) and HEAD not pushed (7). Lowering `--worktree-days` from 30 to 7 adds only
  1 stale worktree today.
- **Follow-up:** watch `dev_worker_disk_guard_*` and `dev_worker_dir_bytes` for 7 days after rollout. If dw3 still
  trends under 15 %, the levers are the owner's: the stale threshold, the agent's own worktree hygiene, or a larger
  `/workspace` (infra, frozen).
- **Not changed here:** any threshold, the disk-guard ladder, `DISPOSABLE`.

## Critical files

- `ansible/roles/dev_worker/files/cleanup`
  - `reflog_time`, used by `scan_worktree`.
  - `link_index` (with its incomplete state), the resolver, and `linked_from`.
  - `plan_deps` and `plan_worktrees` take the index and report linked candidates.
  - `main.scan()`: lazy build, inventory roots, the incomplete-inventory refusal, the summary line.
  - `execute()`: the 120 s refresh.
  - The module docstring and the help text: A, B, the exact guarantee, the residual cases.
- `scripts/tests/test_dev_worker_cleanup.py`: Linux-only, like `DepsTest`/`WorktreesTest`.
  - **Fixtures:** `WorktreesTest.age()` also rewrites the reflog ENTRY timestamps. Real git writes recent entries, so
    without this the existing stale and merged tests would stop producing candidates. Separate tests keep "recent git
    activity is fresh".
  - **Real-git regression:**
    - a main clone and two linked worktrees with old-dated entries (`GIT_COMMITTER_DATE`);
    - `git reflog expire --all --expire=never --expire-unreachable=never`, then `git gc`;
    - assert every `logs/HEAD` mtime changed and the worktrees are still judged idle, by both the deps and the
      worktrees step;
    - then `--expire=now`: an empty reflog stays idle.
  - **Parser:**
    - recent entry first, then an older-dated one: max wins;
    - partial final record (no trailing `\n`): fresh;
    - malformed last record after valid ones: mtime fallback;
    - a final record longer than 64 KiB: mtime fallback;
    - no tab or message: parsed;
    - SHA-256 oids: parsed;
    - missing file: 0;
    - empty file: 0;
    - FIFO, and a symlink at `logs/HEAD`: unknown (fresh);
    - future timestamp: fresh.
  - **Inventory and `linked_from`:**
    - blocks:
      - whole-dir link;
      - per-entry link;
      - `@scope/pkg` link;
      - a deep link (`node_modules/.bin/tool`, nested `node_modules`, `.venv/bin`, below a real `target/debug`);
      - relative link;
      - two-hop chain;
      - a symlinked middle component;
      - `..` after a symlinked component;
      - ancestor inside the worktree;
      - dangling link into the candidate;
      - a link in a root-owned top dir;
      - a link inside a tool cache;
      - a `/home` link when `--deps-roots=/workspace` (the inventory roots are independent);
    - does not block:
      - an ancestor above the worktree;
      - a link inside the candidate to itself;
      - a sibling with the same prefix (`node_modules2`);
    - loop: the traversed paths are recorded, and the walk terminates;
    - an inventory error removes nothing, with the message.
  - **Combined flags and execute:**
    - a worktree kept because it is linked still has its unlinked deps planned;
    - a link created after the scan, with an injected clock making the index older than 120 s: skipped;
    - a deps action with one newly linked dir: the whole action is skipped.
- `docs/runbooks/dev-workers.md`, disk section: both behaviours, the "linked from" line, the "inventory incomplete"
  line, and the canary procedure.

## Verification

1. **Unit tests** in `python:3.12` (with git): `python3 -m unittest discover -s scripts/tests -p "test_*.py"`.
   - Record the python and git versions.
   - Assert that `DepsTest` and `WorktreesTest` RAN, by test count and skips, not merely that the run passed.
2. **CI (`script-tests.yaml`, the runner's own python and git):** check in the job log that the Linux and git suites
   ran (`skipped=` count), plus the PR's reviewbot reviews.
3. **Canary on dw3. Stop conditions:** any unexpected deletion, any newly broken link, an incomplete inventory, or a
   ladder run over 15 min. On any of them: restore `cleanup.prev`, re-enable the timers, and stop the rollout.
   1. `sudo systemctl stop dev-worker-disk-guard.timer dev-worker-deps-prune.timer`, then wait until neither service is
      active.
   2. Baseline: `sudo cp /usr/local/bin/cleanup /usr/local/bin/cleanup.prev`. Record the set of links on `/workspace`
      and `/home`, each with its resolved target (NUL-separated, outside the container roots).
   3. Deploy: `ansible-playbook dev-workers.yml -l dev-worker-3 --tags cleanup` from WSL with `ANSIBLE_CONFIG` set.
   4. Dry runs:
      - the guard's EXACT step commands, with its actual `--deps-roots` (the worktrees step, then the deps step);
      - the daily timer's commands.
      Check that:
      - the known linked sources (`relay-workers/node_modules`, …) are reported as linked;
      - positive controls are still planned (an idle unlinked dep dir, or an eligible unlinked worktree), or there are
        none;
      - there is no "incomplete";
      - the timings are recorded.
      Record the planned paths and the eligible bytes.
   5. A real run of the guard's exact commands.
   6. Check:
      - every link that resolved in step 2 and still exists still resolves;
      - what disappeared equals the recorded plan;
      - a consumer smoke test: `node -e "require.resolve('vitest')"` in one linked worktree.
   7. Re-enable both timers.
4. **dev-worker-1, -2 and -4:** deploy and run the dry runs (their timers stay on; their disks are healthy). Same tool
   everywhere, no drift.

<!-- codex-review-status: complete -->
