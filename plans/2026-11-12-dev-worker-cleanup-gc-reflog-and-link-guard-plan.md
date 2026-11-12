# Dev-worker cleanup: gc-proof idle detection and a cross-tree link guard

## Codex Review

- Shipping A+B together is correct. Descriptor-based reflog reads, cross-root inventory coverage and fail-closed traversal are useful improvements.
- The empty-reflog justification and “newest surviving entry” claim exceed what the bounded parser proves. Resolution errors and ordinary directories traversed before `..` also need explicit handling.
- The ancestor-above-worktree exception leaves a real consumer gap; the pushback incorrectly assumes later application access paths are inventoried.
- The 120 s refresh needs precise scheduling and deletion-boundary semantics, particularly when a cold inventory itself takes 154 s.
- The canary deployment restarts the daily prune timer. Timer isolation, complete deletion manifests, rollback recovery and disk-guard failure reporting need tightening before rollout.

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
<!-- codex: A recent entry followed by more than 64 KiB of older-dated entries is still missed, so this is not necessarily the newest surviving entry claimed below. Add that boundary case and either explicitly accept the bounded-tail limitation or use a conservative policy when the omitted prefix could contain newer activity. -->
  - If the LAST complete record is malformed, or no complete record fits in 64 KiB, fall back to the descriptor's
    mtime. That is conservative: the old behaviour. A malformed tail must never cause an older valid entry to be
    selected.
  - An empty file (size 0) returns 0. `reflog expire` empties a log only when every entry is past `gc.reflogExpire`
    (90 days) or `gc.reflogExpireUnreachable` (30 days), so the last HEAD movement is at least that old. Using the
    file's mtime here would keep the false freshness that A fixes. The index, `HEAD` and file mtimes still count.
<!-- codex: The 90/30-day values are configurable defaults, and explicit expiry such as the planned `--expire=now` test can empty a recently active log; emptiness proves no minimum age. Either retain conservative freshness when that evidence is unavailable or explicitly accept the lost activity signal, and test aggressive expiry of recent entries rather than only already-idle fixtures. -->
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
<!-- codex: `.git` also contains user-managed consumers such as `.git/hooks/pre-commit -> W/node_modules/tool`; skipping it can leave those links broken. Include such hook links or explicitly document this consumer exclusion in the safety guarantee. -->
  - Not covered: a consumer on ANOTHER filesystem mounted below a root. That is stated in the docstring.
  - Measured on dw3 (prototype, read-only): 3.9 M entries, 520 k dirs, 37.6 k links. 7 s warm, 154 s right after a
    page-cache drop.
- **Traversal: descriptor-relative.**
  - Each dir is opened with `openat(parent_fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)` and listed with
    `scandir(fd)`. A dir swapped for a symlink fails the open instead of being followed.
<!-- codex: With `scandir(fd)`, `DirEntry.path` is only the entry name, so keep an explicit absolute source path for comparisons and reporting while reading the discovered link with `readlink(name, dir_fd=parent_fd)`. Reopening that source by its reconstructed pathname would lose the traversal's protection against parent-directory swaps; reuse `open_dir_nofollow` to anchor inventory roots. -->
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
<!-- codex: Define a nonzero exit status for incomplete inventory even when it leaves an empty plan: otherwise `main()` returns success and disk-guard can report EXHAUSTED instead of a failed safety check. An incomplete execute-time refresh must stop all remaining deps/worktree removals without reusing the previous index, while allowing independent Docker actions to finish. -->

#### Resolving each link

- **A bounded resolver of our own**, component by component, like `posixpath.realpath`, with no
  `os.path.realpath` call.
  - At most 40 symlink expansions in total.
  - A relative target resolves against the RESOLVED parent dir.
  - A `..` applies to the resolved path, never lexically before earlier components are resolved.
- **For each link it records the paths its resolution TRAVERSED:**
  - every symlink location met, including the link itself and any symlinked path component on the way;
  - the final path.
<!-- codex: `consumer/link -> W/node_modules/../src` depends on the ordinary directory `W/node_modules`, but the listed records omit it and deleting it breaks the link. Record these ordinary traversal dependencies separately from terminal directory targets, applying only the into-candidate predicate to ordinary prefixes so recording `W` does not accidentally protect every sibling dependency. -->
- **When resolution fails** (dangling tail, a loop, too many expansions, an error), it records what it traversed so far
  plus the unresolved remainder appended to the resolved prefix. Unresolved never counts as "references nothing".
<!-- codex: An unresolved spelling after `EACCES`, `EIO` or another unexpected lookup error cannot reveal an unreadable intermediate symlink into a different candidate. Distinguish these failures from expected dangling/loop/expansion-limit outcomes and propagate them to the incomplete-inventory refusal, with injected `lstat` and `readlink` failures in tests. -->
  - Resolution reads link targets and checks existence and type with `lstat` and `readlink`. It never opens a
    target's contents.

#### `linked_from(index, C, W)`

`C` is the candidate (a dep dir, or a whole worktree). `W` is the worktree that holds `C` (`W == C` for a whole
worktree). The function returns the links not under `C` with a traversed path `T` such that either:
- `under(T, C)`: into the candidate, or the candidate itself; or
- `under(C, T) and under(T, W)`: `T` is an ancestor of `C` inside its worktree. Example: `consumer/lib -> W`, used as
  `lib/node_modules/x`.

`under` compares on path-component boundaries, as it does today.

<!-- codex: round-2: The broad-retention concern is valid, but inventory resolves link payloads, not later application access paths: `~/ws -> /workspace/c4` records only `/workspace/c4`, even when a consumer later uses `~/ws/repo/node_modules/x`. Either protect those ancestor targets or explicitly accept this consumer gap and narrow the guarantee; the claim that the later resolution is already recorded is incorrect. -->
<!-- opus-pushback: Accepted for ancestors from C up to its worktree root W, the `consumer/repo -> source-worktree` case. Not for ancestors ABOVE W: a convenience link such as `~/ws -> /workspace/c4` or `~/work -> /workspace` would mark every candidate on the box as linked, and nothing would be reclaimed again. A consumer reaching C through such a link does it by a path whose resolution traverses that link and ends under C, and that resolution is already recorded. -->

- A link inside `C` that points into `C` does not count.
- A dangling link whose traversed path is under `C` does count (conservative).

#### Using the index

- **Built lazily, once per `scan()`**, and only when the deps or worktrees step has at least one candidate. The deps
  and worktrees steps share it within one process.
  - `disk-guard` runs the worktrees and deps steps as SEPARATE `cleanup` processes, each with a plan scan and a fresh
    scan. A tick under pressure with candidates in both steps can therefore walk up to 4 times: about 30 s warm, about
    10 min worst case cold.
<!-- codex: Four walks is only the initial planning/rescan cost, not a worst-case bound once execute-time refreshes are included. Measure the final implementation across many actions and cold scans, including refresh count and total ladder duration. -->
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
<!-- codex: Age-independent reporting conflicts with building the index only when an otherwise eligible candidate exists; define whether the report covers only runs that actually build an inventory. Emit the aggregate after execution as well as on dry-run/no-action exits, since more than 15 action lines would otherwise push a planning-time summary out of disk-guard's retained output. -->
- **Freshness at execute time.** The index records its start time. `execute()` rebuilds it before an action when it is
  older than 120 s, and skips the action if the fresh index shows a link into it. Same as today, `still_planned` is
  all-or-nothing for a deps action: if one of its dirs gains a link in the fresh scan, the whole action is skipped this
  run. That is intended and tested.
<!-- codex: A 154 s walk is already older than 120 s when it completes, so start-time scheduling can rebuild before every action or repeatedly rebuild without making progress. Use monotonic timestamps and define start versus completion semantics explicitly—for example, retain start time for the observation-age bound and completion time for refresh scheduling—and test a walk longer than the interval. -->
<!-- codex: Checking only at entry to `execute()`'s action branch does not bound freshness at deletion: `remove_worktree` performs Git checks and a full inspection, and one deps action can inspect/delete several large directories. Recheck at each destructive boundary after expensive inspection, or include that additional unbounded delay in the stated guarantee and test a slow action. -->
- **What is guaranteed, precisely.** There is no removal of a dir or worktree that a symlink on the inventoried
  filesystems resolved through when an index no older than 120 s plus one walk was taken.
  - What remains is a link created after that, to a candidate. Such a link would point to a dir that has already
    been, or is being, deleted.
<!-- codex: The walk is not an atomic snapshot: a link can be created or retargeted after its directory was visited, including during the inventory, and remain valid for minutes before deletion begins. State the guarantee in terms of references actually observed in a completed inventory, with its coverage exclusions, and describe this whole race window rather than only links to already-deleted directories. -->
  - The consequences are bounded by what is eligible at all. Deps are regenerable (`npm ci`, `cargo build`). A whole
    worktree is eligible only if it is clean and merged, or clean with its HEAD on a forge branch, so it can be
    recovered from the forge. The docstring states this.
<!-- codex: `judge_worktree` checks local remote-tracking refs and accepts squash-equivalent merges, so it does not prove that the exact HEAD is recoverable from the current forge. Describe recovery of source contents under those assumptions and the loss of the worktree-specific reflog, without adding a new live-network dependency to cleanup. -->
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

<!-- codex: The requested scope includes `ansible/roles/dev_worker/files/disk-guard`, but this list specifies no caller change or caller tests. State whether cleanup's exit/output contract alone is sufficient or describe the required guard edit, and add `test_dev_worker_disk_guard.py` coverage for incomplete-inventory failure classification and retained summaries. -->

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
<!-- codex: Also isolate `DEPS_ROOTS` in fixtures: the new union means passing a temporary `--deps-roots` no longer prevents CLI tests from scanning the runner's actual `/home` and `/workspace`. -->
  - **Real-git regression:**
    - a main clone and two linked worktrees with old-dated entries (`GIT_COMMITTER_DATE`);
    - `git reflog expire --all --expire=never --expire-unreachable=never`, then `git gc`;
    - assert every `logs/HEAD` mtime changed and the worktrees are still judged idle, by both the deps and the
      worktrees step;
    - then `--expire=now`: an empty reflog stays idle.
<!-- codex: Pin expiry configuration during the retain-entries phase so the subsequent plain `git gc` cannot empty the fixtures and accidentally test only the empty-file branch. Set reflog mtimes explicitly old before the commands and assert the expected entries survive, avoiding timestamp-resolution-dependent assertions. -->
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
<!-- codex: Exercise the safety mechanisms directly: swap an enumerated directory for a symlink before opening it, inject errors at directory open/scandir and link read, and distinguish ignored `ENOENT` from incomplete inventory. Add exact 40/41-expansion cases and assert descriptors are closed on error paths. -->
  - **Combined flags and execute:**
    - a worktree kept because it is linked still has its unlinked deps planned;
    - a link created after the scan, with an injected clock making the index older than 120 s: skipped;
    - a deps action with one newly linked dir: the whole action is skipped.
- `docs/runbooks/dev-workers.md`, disk section: both behaviours, the "linked from" line, the "inventory incomplete"
  line, and the canary procedure.

## Verification

1. **Unit tests** in `python:3.12` (with git): `python3 -m unittest discover -s scripts/tests -p "test_*.py"`.
<!-- codex: The full discovery suite also imports PyYAML, which stock `python:3.12` does not provide; specify the test environment dependencies rather than only Git. Use verbose output to identify the Linux/Git tests that ran, since an aggregate skip count does not establish their execution. -->
   - Record the python and git versions.
   - Assert that `DepsTest` and `WorktreesTest` RAN, by test count and skips, not merely that the run passed.
2. **CI (`script-tests.yaml`, the runner's own python and git):** check in the job log that the Linux and git suites
   ran (`skipped=` count), plus the PR's reviewbot reviews.
3. **Canary on dw3. Stop conditions:** any unexpected deletion, any newly broken link, an incomplete inventory, or a
   ladder run over 15 min. On any of them: restore `cleanup.prev`, re-enable the timers, and stop the rollout.
<!-- codex: Restoring the executable does not restore deleted targets or repair consumers; for unexpected deletion or broken links, recover and validate the affected paths before resuming automatic reclamation. Specify how an over-15-minute run is stopped and awaited before rollback so an already-running process cannot continue deleting with the old loaded code. -->
   1. `sudo systemctl stop dev-worker-disk-guard.timer dev-worker-deps-prune.timer`, then wait until neither service is
      active.
   2. Baseline: `sudo cp /usr/local/bin/cleanup /usr/local/bin/cleanup.prev`. Record the set of links on `/workspace`
      and `/home`, each with its resolved target (NUL-separated, outside the container roots).
   3. Deploy: `ansible-playbook dev-workers.yml -l dev-worker-3 --tags cleanup` from WSL with `ANSIBLE_CONFIG` set.
<!-- codex: `tasks/cleanup.yml` explicitly sets `dev-worker-deps-prune.timer` to `started`, so this deployment undoes step 1 and its persistent timer may immediately launch cleanup. Keep timer activation suppressed throughout deployment and verify both services remain inactive before proceeding; if disk-guard changes, `--tags cleanup` alone also does not deploy that file. -->
   4. Dry runs:
      - the guard's EXACT step commands, with its actual `--deps-roots` (the worktrees step, then the deps step);
      - the daily timer's commands.
      Check that:
      - the known linked sources (`relay-workers/node_modules`, …) are reported as linked;
      - positive controls are still planned (an idle unlinked dep dir, or an eligible unlinked worktree), or there are
        none;
<!-- codex: Allowing no positive control can pass a canary that refuses all reclamation, reproducing the operational failure this change is meant to fix. Require a controlled eligible unlinked fixture when natural candidates are absent, and verify its actual removal while a linked control survives. -->
      - there is no "incomplete";
      - the timings are recorded.
      Record the planned paths and the eligible bytes.
<!-- codex: Capture a lossless manifest of full candidate paths and identities: the current rendered table groups entries and truncates paths even with `--all`. Compare actual removals against that manifest as a subset, accounting for legitimate execute-time skips; the later requirement for exact equality would incorrectly treat successful revalidation as a canary failure. -->
   5. A real run of the guard's exact commands.
<!-- codex: Running only the worktrees/deps commands does not measure the whole ladder or exercise disk-guard's exit classification, output truncation and heartbeat integration. Include an actual guard invocation with the deployed service arguments and record its complete duration/results separately from the individual cleanup runs. -->
   6. Check:
      - every link that resolved in step 2 and still exists still resolves;
<!-- codex: Use `lstat`/`lexists` to decide whether a symlink still exists: `exists` follows the target and would silently omit newly dangling links from this check. Preserve the original link text/identity as well as its resolved target so replacements and retargeting can be distinguished from cleanup damage. -->
      - what disappeared equals the recorded plan;
      - a consumer smoke test: `node -e "require.resolve('vitest')"` in one linked worktree.
   7. Re-enable both timers.
4. **dev-worker-1, -2 and -4:** deploy and run the dry runs (their timers stay on; their disks are healthy). Same tool
   everywhere, no drift.

<!-- codex-review-status: complete -->