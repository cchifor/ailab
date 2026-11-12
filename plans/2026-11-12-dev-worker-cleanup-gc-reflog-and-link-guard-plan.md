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
- After every gc that ran, nothing is idle for `--deps-days` (3 in the guard) or for `--merged-days` (1), so the deps
  and worktrees steps reclaim nothing.
- With the reflog signal ignored, the deps step would have planned 13 worktrees, 10.9 GB.

**B. Dependency dirs shared through symlinks are unprotected.**
- The relay agent avoids `npm ci` per task. It symlinks a new worktree's `node_modules` (whole, or per entry, plus
  `.vite`/`.vite-temp`) and `holder/target/debug` into an OLDER worktree's dirs.
- Chains exist: `candidate-*/node_modules -> relay-workers-integration/node_modules -> relay-workers/node_modules`.
- The deps step judges a dep dir by its own worktree's idleness, `busy_paths()` (/proc) and container binds. Reading
  through a link never touches the target. `busy_paths()` sees a RUNNING consumer, through the resolved paths of its
  open and mapped files, but not a link that is only waiting to be used.
- A manual `cleanup --deps --deps-days 3` (with the reflog signal patched out) removed `relay-workers/node_modules`.
  That left 4,793 symlinks in about 60 worktrees dangling. It was restored 15 minutes later by `npm ci` from the
  unchanged lockfile.
- `--worktrees` has the same hole: `git worktree remove` deletes a worktree other trees link into.

**Why together:** A masks B today. Fixing A alone would let the guard break consumers automatically.

**Authority:** the dev-worker infrastructure is frozen until Relay workers are validated. On 2026-11-12 the owner
asked for "the best solution to properly address these two issues". This covers the cleanup tool, plus a canary that
pauses dw3's two reclaim timers during rollout. No VM, IP, RAM, threshold or credential change.

## Approach

### A. Activity is the newest reflog ENTRY time, not the file's mtime

`reflog_time(path)` returns one of three things:
- the activity time, in Unix seconds;
- `0`: no reflog activity;
- `None`: unknown, which the caller treats as activity now (fresh).

`scan_worktree` uses it for `logs/HEAD`. The index and `HEAD` files keep using mtime: gc does not touch them, and a
checkout or commit writes them.

- **Read discipline:**
  - Open with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, then `fstat` the DESCRIPTOR and require a regular file.
    Size and fallback mtime come from that same `fstat`.
  - Read at most the last 64 KiB.
  - `ENOENT` returns 0, as today. Any other error, or anything that is not a regular file (FIFO, device, a symlink
    refused by `O_NOFOLLOW`), returns `None`.
  - Parent components are not pinned. The gitdir path comes from the tree's own `.git` file, and the result decides
    only that owner's tree.
- **Parsing:**
  - A record is `<old-oid> <new-oid> <name> <<email>> <unix-ts> <+|-hhmm>`, then optionally `\t<message>`. The tab is
    omitted when there is no message.
  - The identity part is the text before the first tab, or the whole record. It must match
    `^([0-9a-f]{40}|[0-9a-f]{64}) ([0-9a-f]{40}|[0-9a-f]{64}) .* (\d{1,12}) [+-]\d{4}$`.
  - The timestamp is the Unix-seconds field. No timezone arithmetic is applied.
  - Only complete records (ending in `\n`) count. A leading fragment is dropped when the read started mid-file.
  - A file not ending in `\n` means an append is in progress: return `None`.
- **Value:**
  - The MAXIMUM timestamp over the valid complete records in the tail, because committer dates need not be monotonic.
  - A malformed LAST record, or no complete record in the window, falls back to the descriptor's mtime (the old
    behaviour), never to an older entry.
  - A timestamp later than now plus 5 minutes is treated as now.
- **Accepted limitations** (stated in the docstring and tested):
  - **The window is bounded.** A recent entry followed by more than 64 KiB (about 400 records) of OLDER-dated entries
    is not seen. Git appends in time order, so that needs backdated identities or a clock jump.
  - **An empty file returns 0, not "now".** With default settings, expiry empties a log only when every entry is past
    `gc.reflogExpire` (90 days) or `gc.reflogExpireUnreachable` (30 days). Using the mtime there would keep A's false
    freshness for exactly the stale worktrees. An explicit aggressive expiry (`--expire=now`) of a recently active
    worktree loses this one signal; its index, `HEAD` and file mtimes still count (tested).
  - **Entry times are the committer identity's.** An owner can backdate them, in the owner's own tree only.

### B. A dir another tree links into is never removed

#### What is protected, precisely

The guard protects consumers whose dependency is expressed AS A SYMLINK on the inventoried filesystems: the link
resolves into the candidate, or into an ancestor of it inside its worktree.

It does NOT protect:
- a consumer that names the candidate by path in a file, a config or a command, directly or through an alias above
  the worktree (`~/ws -> /workspace/c4`, used later as `~/ws/repo/node_modules/x`). No link inventory can see such a
  consumer;
- a link on another filesystem mounted below a root.

A protected link was observed in a COMPLETED inventory. The walk is not a snapshot: a link created or retargeted
after its directory was visited is not in that inventory. The removal-time re-check below bounds how old the
observation can be.

What can go without protection is limited to:
- deps, which are regenerable;
- whole worktrees that are clean and either merged (equivalent to a squash merge into a remote default branch, judged
  from local remote-tracking refs) or with HEAD on a remote-tracking branch. Their source contents are recoverable from
  the forge under those assumptions; the worktree's own HEAD reflog is lost.

#### `link_index(roots, excluded, strict)`: the inventory

- **Roots: `LINK_ROOTS` (= `DEPS_ROOTS`) ∪ `--deps-roots`, always.** `disk-guard` passes `--deps-roots` with only the
  pressured filesystems (`disk-guard:321`).
  - Each root is resolved with `realpath` and anchored with `open_dir_nofollow`.
  - Tests patch `LINK_ROOTS`, so CLI tests never walk the runner's real `/home` and `/workspace`.
- **Coverage:** everything below each root on the root's filesystem, any depth, whoever owns it. That includes
  dependency dirs, tool caches, and `.git` dirs EXCEPT their `objects/` and `lfs/` (hook links like
  `.git/hooks/pre-commit -> W/node_modules/.bin/tool` are consumers). Container roots are skipped.
- **Traversal: descriptor-relative.**
  - Each dir is opened with `openat(parent_fd, name, O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)` and listed with
    `scandir(fd)`.
  - Entry types come from `d_type`.
  - The absolute source path is kept explicitly.
  - A link's text is read at listing time with `readlink(name, dir_fd=parent_fd)`.
  - An entry that fails to open with `ELOOP` or `ENOTDIR` changed type: it is re-checked through the parent fd and
    recorded if it is now a link.
  - Another device is not entered. Depth over 256 is an error.
  - Fds are closed on every path (tested).
- **Errors fail closed:**
  - `ENOENT` (vanished) is ignored.
  - Any other error, in the walk or in a link's resolution, makes the index INCOMPLETE. `strict=False` (a non-root dry
    run) only skips permission errors.
  - An incomplete index refuses every deps and worktree candidate, prints
    `link inventory incomplete (N error(s), e.g. <path>: <err>)`, and makes `cleanup` EXIT 1. `disk-guard` already
    counts a nonzero step as failed and never as EXHAUSTED (`disk-guard:357,371`), so no caller change is needed.
  - Docker categories are unaffected.

#### The resolver: `traverse(link, first_target)`

- It works component by component, like `posixpath.realpath`, with no `realpath` call. A relative target resolves
  against the RESOLVED parent, and `..` applies to the resolved path. At most 40 symlink expansions; 41 is a loop.
- It records two sets:
  - `ends`: every symlink location met, including the link itself, plus the final path. Both predicates below apply
    to these.
  - `through`: every ordinary directory a `..` stepped back out of. `consumer/x -> W/node_modules/../src` needs
    `W/node_modules` to exist. Only the into-candidate predicate applies, so recording `W` does not protect `W`'s other
    deps.
- Dangling links, loops and the expansion limit record the resolved prefix plus the unresolved rest, and count.
- Any other lookup error (`EACCES`, `EIO`, …) is an inventory error.
- It never opens a target's contents.

#### `linked_from(index, C, W, worktrees)`

`C` is the candidate. `W` is its worktree (`W == C` for a whole worktree). A link `L` counts when all three hold:
1. `L` is not under `C`;
2. any of these holds:
   - an `ends` path `T` with `under(T, C)`;
   - an `ends` path `T` with `under(C, T)` and `under(T, W)` (an ancestor inside the worktree, the
     `consumer/lib -> W` case);
   - a `through` path `T` with `under(T, C)`;
3. for a dep dir (`C != W`), `L`'s deepest enclosing worktree is not `W`. A worktree's own internal links (pnpm and
   npm workspaces) are regenerated with it. A NESTED worktree is another consumer.

`under` compares on component boundaries.

#### Using the index

- **Built lazily** by an `Inventory` per scan, only when a step has an otherwise-eligible candidate. "Linked" is
  therefore reported only for candidates that would otherwise have gone.
  - Shared by the deps and worktrees steps within one process.
  - Scheduling is monotonic. An index's OBSERVATION time is its walk's start, and its refresh is scheduled from its
    walk's COMPLETION.
- **Deps step:** `plan_deps` refuses a linked dir with `linked from N path(s), e.g. <first>`. Unlinked sibling dirs are
  still planned.
- **Worktrees step:** `plan_worktrees` keeps a linked worktree whatever its idle age, as
  `Kept worktree <path> — linked from …`. Its unlinked deps stay eligible.
- **Re-check at the destructive boundary:**
  - `remove_deps` re-checks each dir right before `remove_dep_dir`, after its re-inspection.
  - `remove_worktree` re-checks right before `git worktree remove`, after its git checks and inspection.
  - Each re-check uses an index whose walk COMPLETED at most `INDEX_MAX_AGE` (120 s) ago, rebuilding it first when it
    is older.
  - An incomplete rebuild refuses that removal and every later one in the run; the previous index is not reused.
  - Same as today, `still_planned` stays all-or-nothing for a deps action (tested).
- **Reporting:** `Linked from other trees, not removed: N dep dir(s), M worktree(s) (e.g. …)` is printed as the LAST
  line of every walk run: dry run, nothing to do, or after execution. That keeps it inside `disk-guard`'s 15 retained
  lines. A run that took an inventory also prints `Link inventory: N links (Xs)`.
- **Cost:** `disk-guard` runs the worktrees and deps steps as separate processes. Each takes a plan scan and a fresh
  scan, plus removal-time refreshes. The canary measures the whole ladder: walk count, seconds, and the ladder's total
  duration. The guard's `OnUnitInactiveSec` timer never overlaps runs, and the step timeout is 2700 s.

### Out of scope: capacity, measured afterwards

- **Growth:** worktrees created in the last 3 days held 14.0 GB: about 7.0 GB of dependency or build output and
  7.0 GB of source and evidence. These are potential amounts. The canary's manifest records what is actually eligible
  with A+B.
- **What blocks old worktrees:** among those idle 14 days or more, uncommitted changes (29), shallow repositories (24)
  and HEAD not pushed (7).
- **Follow-up:** watch `dev_worker_disk_guard_*` and `dev_worker_dir_bytes` for 7 days. If dw3 still trends under
  15 %, the levers are the owner's: the stale threshold, the agent's worktree hygiene, or a larger `/workspace`.
- **Not changed:** thresholds, the disk-guard ladder, `DISPOSABLE`, disk-guard's code.

## Critical files

- `ansible/roles/dev_worker/files/cleanup`
  - `reflog_time`, used by `scan_worktree`.
  - `link_index`, `traverse`, `owner`, `linked_from`, `Inventory`.
  - `plan_deps` and `plan_worktrees` take `linked`.
  - `remove_deps` and `remove_worktree` take a removal-time re-check.
  - `execute` passes it through.
  - `main`: the inventory roots, the exit 1 on an incomplete inventory, the last-line summary.
  - The docstring states A, B and the guarantee above.
- `scripts/tests/test_dev_worker_cleanup.py`
  - **Fixtures:** `WorktreesTest.age()` ages the reflog entries too. `DepsTest` and `WorktreesTest` patch
    `LINK_ROOTS` to `()`.
  - **Real-git gc regression:**
    - pin `gc.reflogExpire*` to `never` for the retain phase;
    - set old mtimes explicitly;
    - assert that the entries survive, the mtimes moved, and the worktrees are still planned;
    - then `--expire=now`: empty logs still planned;
    - and a recently active worktree whose log was emptied stays fresh through its file mtimes.
  - **Parser:** every case in A, including the 64 KiB-boundary limitation.
  - **Inventory, resolver, `linked_from`:**
    - blocks:
      - whole-dir, per-entry, `@scope`, deep (`.bin`, nested `node_modules`, `.venv/bin`, below `target/debug`),
        relative, two-hop, symlinked-component and `..`-after-symlink links;
      - `..` after an ordinary dir (`through`);
      - ancestor inside the worktree;
      - dangling link;
      - root-owned top dir; tool cache; `.git/hooks`;
      - a `/home` link when `--deps-roots` is narrower;
    - does not block: ancestor above the worktree; links inside the candidate or its own worktree; prefix siblings;
    - a nested worktree counts;
    - loops terminate; 40 expansions resolve, 41 count as a loop;
    - injected errors: `ENOENT` ignored; `EIO` in scandir, open or `readlink` makes the inventory incomplete;
    - a dir swapped for a symlink between listing and open is recorded as a link;
    - fds are closed after error paths;
    - an incomplete inventory refuses everything and exits 1.
  - **Execute:**
    - a link created after the scan is caught at the removal boundary once the index is older than 120 s;
    - an index older than 120 s at completion (slow walk) is rebuilt once, not before every action;
    - an incomplete refresh refuses the rest;
    - a deps action with one newly linked dir is skipped whole.
- `scripts/tests/test_dev_worker_disk_guard.py`: if not already covered, a test that a step exiting 1 is a failed
  step and not exhausted.
- `docs/runbooks/dev-workers.md`, disk section: both behaviours, the two new output lines, and the canary procedure.

## Verification

1. **Unit tests:**
   - In `python:3.12` with git and PyYAML (`pip install pyyaml`), verbose: the `test_dev_worker_*` modules, then full
     discovery. Record the python and git versions, and the names of the Linux and git tests that RAN.
   - **CI (`script-tests.yaml`, the runner's own python and git):** confirm the same in the job log. Plus the PR's
     reviewbot reviews.
2. **Canary on dw3, with the new tool side-installed.** The Ansible `cleanup` tag STARTS `dev-worker-deps-prune.timer`
   (`tasks/cleanup.yml`), so Ansible deploys only after the canary.
   1. **Pause reclaim:** `sudo systemctl stop dev-worker-disk-guard.timer dev-worker-deps-prune.timer`. Wait until
      neither service is active.
   2. **Install the candidate:** copy the new file to `/usr/local/bin/cleanup.new` (root, 0755). The deployed
      `/usr/local/bin/cleanup` is untouched.
   3. **Baseline:**
      - a lossless link census: for every symlink on `/workspace` and `/home` outside container roots, its path, its
        link text and its resolved target, NUL-separated;
      - two fixtures under `/workspace/cleanup-canary` (owned by c4): a git worktree with an aged `node_modules`
        (positive control), and a second one whose aged `node_modules` is linked from a third dir (linked control).
   4. **Manifest:** run `cleanup.new` with the guard's exact step arguments (its `--deps-roots`), its `execute`
      replaced by a dump of the full plan (paths, ids, fingerprints). That records exactly what a real run would
      attempt, plus the timing and the "Link inventory" line.
      - Expect: the positive control planned; the linked control and the known shared sources reported as linked;
        no "incomplete".
   5. **Real run:** `sudo /usr/local/sbin/disk-guard <the service's arguments> --cleanup /usr/local/bin/cleanup.new`
      (or its installed path). That is a real ladder run, with its exit classification, 15-line output and heartbeat.
      Record its duration.
   6. **Check:**
      - every link that existed before (by `lstat`) and still exists still resolves to the same target;
      - every removed path is in the manifest (a subset: execute-time skips are legitimate);
      - the positive control's `node_modules` is gone, and the linked control's is intact;
      - smoke test: `node -e "require.resolve('vitest')"` in one consuming worktree.
   7. **Stop conditions:** an unexpected removal, a newly broken link, an incomplete inventory, or a ladder over
      15 min. Then:
      - `systemctl stop` the guard service and wait until it is inactive;
      - remove `cleanup.new` and leave timers stopped;
      - repair the damage: `npm ci` or `cargo build` for deps; `git worktree add <path> <branch>` for a worktree;
      - report before resuming. Rolling back the binary alone repairs nothing.
   8. **Pass:** remove the fixtures and `cleanup.new`. Deploy with Ansible (`--tags cleanup`, which restarts the
      prune timer), start the guard timer, and confirm one scheduled guard tick ends cleanly.
3. **dev-worker-1, -2 and -4:** deploy with Ansible, then run the manifest dry run (step 2.4) on each. Their disks are
   healthy, so their timers stay on.

<!-- codex-review-status: finalized -->
