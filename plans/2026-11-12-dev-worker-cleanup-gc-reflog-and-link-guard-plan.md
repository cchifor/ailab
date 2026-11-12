# Dev-worker cleanup: gc-proof idle detection and a cross-tree link guard

## Codex Review

- Shipping A and B together is justified. Reusing the existing activity checks and keeping thresholds unchanged keeps the change focused.
- The proposed link inventory has deletion-safety gaps: pressure-filtered roots, skipped directories, shallow dependency scans, and links to ancestors of a candidate.
- Root-level reads and symlink resolution need explicit race protection, bounded resolution, and refusal to delete when the inventory is incomplete.
- The execution race is understated: the inventory can age during scanning and earlier actions, and atomic dependency-directory renaming does not protect symlink consumers.
- Before rollout, add real linked-worktree expiry/gc verification, update timestamp fixtures, test disk-guard’s separate invocations, and define a controlled canary and rollback.

## Context

On 2026-11-12, `DevWorkerDiskFilling` (critical) fired for 7 h on dev-worker-3 (192.168.0.10). `/workspace` was at
94 %, and `dev-worker-disk-guard` reported EXHAUSTED on every 5-minute tick: each step of its ladder ran and none
reclaimed anything. The investigation found two defects in `ansible/roles/dev_worker/files/cleanup`. They have to
ship together.

**A. `git gc` makes every worktree look active.**
- `scan_worktree` counts the mtime of each worktree's HEAD reflog (`<gitdir>/logs/HEAD`) as activity.
- `git gc --auto` runs `git reflog expire --all`, which rewrites the reflog of EVERY linked worktree of the repository
  at once. That gives each of them a new mtime, even when no entry was dropped.
  <!-- codex: Qualify this as an auto-gc that actually performs housekeeping: `--auto` may exit without doing anything, while `reflog expire --all` normally includes linked worktrees unless restricted with `--single-worktree`. See the [gc documentation](https://git-scm.com/docs/git-gc/2.43.0) and [reflog documentation](https://git-scm.com/docs/git-reflog/2.39.0). -->
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
    <!-- codex: The tab/message is optional in valid reflogs; parse the identity suffix without requiring a message or interpreting message text as metadata, and use Unix seconds without applying the timezone offset again. Git's [reflog writer](https://github.com/git/git/blob/v2.43.0/refs/files-backend.c) explicitly omits the tab when there is no message. -->
  - It reads at most the last 4 KiB of a REGULAR file, with the same open discipline as `read_line`: lstat
    regular-file check, `O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`, because root reads user-writable trees.
    <!-- codex: Copying `read_line` is insufficient: its lstat/open race permits replacement after the type check, and O_NOFOLLOW protects only the final component of the user-controlled gitdir/logs path. Pin parent directories without following symlinks and fstat the opened descriptor before seeking or reading; obtain fallback metadata from that same descriptor. -->
  - It takes the last line whose identity part ends in `<digits> <+|-hhmm>`.
    <!-- codex: A malformed or partially appended final entry must not cause selection of an older valid entry and premature deletion. Discard a leading fragment when the tail starts mid-record, require a complete final record, and conservatively handle oversized records, concurrent append/truncation/replacement, and invalid numeric values. -->
- **Callers:** `scan_worktree` uses `reflog_time` for `logs/HEAD` instead of `mtime`. The index and HEAD files keep
  using mtime: gc does not touch them, and a checkout or commit writes them.
- **Fallbacks:** no parseable entry falls back to the file's mtime (conservative: the old behaviour). A future
  timestamp counts as activity (fresh).
  <!-- codex: Expiry can remove every entry, leaving an empty reflog whose mtime subsequent expiry refreshes, so this fallback preserves the original false-freshness problem for some old worktrees. Explicitly test and document that limitation rather than promise universal gc-proof detection; Git's [expiry implementation](https://github.com/git/git/blob/v2.43.0/refs/files-backend.c) commits the rewritten log even when empty. -->
  <!-- codex: Distinguish a genuinely absent reflog from an unreadable or changing one: the existing mtime helper returns None on every OSError, which scan_worktree converts to zero. An I/O or permission failure must make activity unknown/fresh, not silently remove this activity signal. -->
- **Why this signal:** `reflog expire` keeps each surviving entry's own timestamp, so the last entry's time is the last
  HEAD movement in that worktree, and gc cannot change it.
  <!-- codex: This is the last surviving recorded timestamp, not a guaranteed wall-clock activity time: expiry can remove entries, and committer dates or clock changes can make timestamps nonmonotonic. State that assumption and test a recent entry followed by an older timestamp so newer activity visible in the bounded tail is not silently discarded. -->
- **Scope:** this changes `scan_worktree` only. Every caller inherits it: `plan_deps`, `remove_deps`'s re-judgement,
  `judge_worktree` and `remove_worktree`.

### B. A dir another tree links into is never removed

- **`link_index(roots, excluded)`, the symlink inventory:**
  - It walks the same trees as `find_worktrees`: regular-user top dirs under each root, same filesystem, no
    `SKIP_NAMES`, no container roots (`container_roots()`).
    <!-- codex: Blocking integration gap: disk-guard.guard passes --deps-roots containing only filesystems still under pressure, so a /home consumer of a /workspace dependency can disappear from this inventory. Separate consumer-search roots from deletion roots, preserve pressure filtering for deletion, and cover custom roots in the caller contract and integration tests. -->
    <!-- codex: Discovery exclusions do not establish where consumers may live: links under .local, .cache, .codex, nested mounts, or beyond find_worktrees' depth-six limit can still reference a deletion candidate. Specify whether the inventory inherits that depth limit and either cover these sources or explicitly constrain the supported sharing layout; the unconditional “never removed” claim is currently unsupported. -->
    <!-- codex: A path-based scandir walk can follow a directory swapped for a symlink after its no-follow stat, escaping the intended tree as root. Use descriptor-relative traversal or an equivalent confinement mechanism, and define how target resolution outside trusted roots is handled without opening target contents or traversing arbitrary privileged trees. -->
    <!-- codex: Define an explicit incomplete-inventory result for scandir, stat, and readlink failures instead of copying find_worktrees' skip-on-error behavior. Missing sources must prevent affected deletions, with a visible failure when coverage cannot be established, rather than look like an empty successful inventory. -->
  - It records every symlink it meets. Inside a dep dir (`dep_kind`) it lists only the direct children, and descends
    one more level only into npm `@scope` dirs. That is where per-entry links live, and it keeps the walk off the
    millions of files inside packages.
    <!-- codex: This catches the incident layout but misses legitimate consumers such as node_modules/.bin/tool, nested node_modules or .pnpm links, .venv/bin links, and links below a real target/debug directory. Add representative regressions and resolve this coverage tradeoff before treating the shallow walk as a general deletion guard. -->
  - For each link it records its targets: every hop, made absolute against the `realpath` of the link's own directory
    and normalised (followed while the hop is itself a symlink, at most 40), plus `realpath(link)`.
    <!-- codex: Do not collapse “..” before resolving preceding symlink components: lexical normalization can change the destination. Resolution also needs to retain intermediate component symlinks, since following only whole-path symlinks plus the final realpath can miss an intermediate worktree whose removal breaks the chain. -->
    <!-- codex: The 40-hop limit does not bound a separate unrestricted realpath call, and default non-strict realpath can return a partially resolved path after errors. Define bounded component resolution and explicit treatment of cycles, excessive chains, inaccessible components, and dangling tails; unresolved must not mean unreferenced. -->
  - Recording the hops catches chains through an intermediate link. Recording the realpath catches symlinked path
    components.
- **`linked_from(index, path)`:** the links that are not under `path` and have a target under `path`, including
  `path` itself. A dangling link whose target lies under `path` counts too (conservative). A link inside the candidate
  that points into the candidate does not count.
  <!-- codex: A link to an ancestor also exposes the candidate: consumer/repo -> source-worktree allows consumer/repo/node_modules to use a dependency that this predicate would delete. Conservatively protect descendants of linked directory targets as well, with component-boundary tests to avoid confusing sibling prefixes. -->
- **Built lazily, once per `scan()`:** only when the deps or worktrees step has at least one candidate. `main()` scans
  twice (the plan, then the fresh re-scan right before `execute`), so the index is at most one scan old at execute
  time. The deps and worktrees steps share one index.
  <!-- codex: One scan is not a bounded age: links can appear after their source directory was visited while the inventory or later Git inspections are still running, before execute even starts. Track inventory age from scan start and define when execution must refresh or skip stale coverage, including delays from earlier actions. -->
- **Deps step:** `plan_deps` refuses a linked dep dir with
  `Not touching <dir>: linked from N path(s), e.g. <first>`. The worktree's unlinked dep dirs are still planned.
- **Worktrees step:** `plan_worktrees` keeps a linked worktree. The reason is `linked from N path(s), e.g. <first>`,
  listed in the "Kept" output once the worktree is idle past `--worktree-days`.
  <!-- codex: Linked merged candidates idle between the guard's one-day merged threshold and its thirty-day stale threshold will be retained silently under the existing reporting rule. Provide a concise refusal summary or diagnostic path, especially because disk-guard.run_step retains only the last fifteen output lines. -->
- **Execute:** `still_planned` already drops a deps action whose dirs the fresh scan no longer plans, and a worktree
  action that is no longer in the fresh plan. Neither `remove_*` function rebuilds the index.
  <!-- codex: still_planned is all-or-nothing for a deps action: if just one of its directories gains a link, every directory in that original action is skipped. This is safe but differs from retaining execution of unlinked siblings; specify and test the intended behavior while preserving inode fingerprints if actions are narrowed. -->
  - Residual race: a link created between the fresh scan and the removal. The removal renames the dir aside
    atomically, so the window is the execution of the items before it. Accepted; documented in the code.
    <!-- codex: Atomic rename immediately breaks a newly created consumer link; it does not protect that consumer, and remove_worktree does not rename at all—it calls git worktree remove. The window also includes scanning and the current action's re-inspection, while the shared cleanup lock excludes other cleaners but not link creators, so revise the justification and test link creation during execution. -->
- **Measured cost on dw3 (prototype, read-only):** 3.03 M entries, 23.7 k links, 29 s on a cold cache and 6 s on a
  warm one. It found all 4,793 links from the incident (0 missed).
  - The cost is paid only by runs that have candidates. When every candidate is linked, a guard tick under pressure
    pays one walk per tick.
    <!-- codex: disk-guard runs worktrees and deps as separate cleanup processes, so both reaching candidates costs two inventories even if all are retained, and execution can require four across their initial/fresh scans. Benchmark the complete ladder, including candidate-by-link comparisons and memory, and account for the 2700-second step timeouts and OnUnitInactiveSec scheduling, which delays the next tick until the current run finishes. -->
- **Why not detect consumers some other way:** a symlink is a name, not a reference. Nothing at the target records
  who points at it, and no /proc or mount signal shows it. An inventory is the only sound check.
  <!-- codex: busy_paths can expose resolved targets of open or mapped files, but cannot prove the absence of future users; an inventory complements that check. Its guarantee still depends on complete declared coverage or an enforced sharing contract, so describe the bounded protection precisely without expanding this fix into a persistent dependency graph. -->

### Out of scope: capacity, measured afterwards

- **Growth:** worktrees created in the last 3 days held 14.0 GB. About 7.0 GB was dependency or build output, which A
  lets the deps step reclaim after 3 idle days. The other 7.0 GB was source and agent evidence, which goes only when
  the worktree is merged (1 idle day) or stale (30 days with HEAD pushed).
  <!-- codex: These are potential amounts, not expected reclaim after A+B: incoming links, busy processes, and the existing safety checks can retain them indefinitely. Recompute the eligible bytes with both changes enabled before using these figures to assess whether the fix relieves pressure. -->
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
  <!-- codex: Existing WorktreesTest.age only changes filesystem mtimes; its real Git reflog entries remain recent, so many existing stale/merged tests will stop producing candidates. Update fixtures to control reflog timestamps as well, preserving separate tests for recent Git activity. -->
  <!-- codex: Inventory fixtures need an explicit ownership seam: WorktreesTest patches MIN_UID to zero, but find_worktrees has an independent min_uid=1000 default and temporary directories may be root-owned in the proposed container. Ensure tests actually inventory their consumer links rather than silently exercising an empty index. -->
  - A: a fresh reflog mtime with an old last entry is idle; a recent last entry is fresh; an unparseable reflog falls
    back to the mtime; a future timestamp is fresh.
    <!-- codex: Add focused cases for the parser and read failures identified above, including an old valid entry followed by malformed/partial data, a final record over 4 KiB, missing/empty logs, FIFO/symlink substitution, ancestor substitution, and concurrent replacement. Assert conservative activity classification and bounded reads, not merely successful parsing. -->
  - B:
    - whole-dir link, per-entry link, `@scope/pkg` link, relative link and a two-hop chain → refused;
    - an unlinked sibling dep dir in the same worktree is still planned;
    - a link inside the candidate pointing into itself does not block;
    - a dangling link into the candidate blocks;
    - a worktree linked into is kept by the worktrees step;
    - the fresh re-scan drops a dir linked after the first scan (the execute path).
    <!-- codex: Also exercise main/execute with combined flags: a worktree retained because of an incoming link must remain eligible for cleanup of its unrelated dependencies, and a link introduced before a later action must follow the documented freshness policy. Cover separate disk-guard worktree/deps invocations and pressure-filtered roots in scripts/tests/test_dev_worker_disk_guard.py if the caller contract changes. -->
- `docs/runbooks/dev-workers.md`, disk section: one paragraph on both behaviours and the "linked from" message.

## Verification

1. Unit tests: `python3 -m unittest discover -s scripts/tests -p "test_dev_worker_*.py"` in a Linux container with
   git (`python:3.12` image; mirrors `script-tests.yaml`). All existing tests stay green.
   <!-- codex: Add a real-Git regression with a main clone and at least two linked worktrees: explicitly expire all reflogs with no entries eligible, run gc, and verify both preserved entry times and resulting cleanup decisions, including the empty-log case. Do not rely on git gc --auto alone, because it may perform no housekeeping. -->
   <!-- codex: script-tests.yaml actually runs every test_*.py with the self-hosted runner's Python, without a Python 3.12 container or explicit Git installation check. Record Python/Git versions and verify the Linux/Git suites ran rather than reporting green after skipUnless skipped them. -->
2. CI: script unit tests, plus the PR's reviewbot reviews.
3. Rollout to dw3 first: `ansible-playbook dev-workers.yml -l dev-worker-3 --tags cleanup` from WSL with
   `ANSIBLE_CONFIG` set.
   <!-- codex: Installing cleanup makes the new deletion policy immediately available to the active guard and daily prune timer, before this dry run. Stage the candidate executable for inspection or temporarily quiesce both reclaim entrypoints and wait for any active run, then enable the canary deliberately after reviewing its plan. -->
   - Dry run: `sudo cleanup --dry-run --no-docker --deps --worktrees --deps-days 3 --worktree-days 30 --merged-days 1`.
     Expect `relay-workers/node_modules`-style sources reported as "linked from …", and a timing in the measured range.
     <!-- codex: Also dry-run the exact separate guard commands with its actual --deps-roots subset; this combined command uses both default roots and shares an index, concealing the key integration and timing differences. Inspect positive controls too: an old unlinked dependency and an eligible unlinked worktree should still be planned. -->
   - Real run of the guard's own step (`systemctl start dev-worker-disk-guard` when under 15 %, or the cleanup step by
     hand), with a dangling-link census (`find /workspace/c4 /home/c4 -xdev -xtype l`) before and after. Pass: no new
     dangling links.
     <!-- codex: Compare filename-safe sets of previously resolving links and their targets, not just dangling-link counts, across every supported user's consumer roots; deleting a consumer or retargeting a link can hide breakage from this census. Include a smoke test through representative Node/Rust consumer links and verify that the intended unlinked candidates were reclaimed. -->
     <!-- codex: Define canary stop conditions and keep the previous executable available: any unexpected deletion, new broken consumer, incomplete inventory, or unacceptable scan latency should halt further rollout. Rolling back the script cannot restore deleted worktrees, so record planned paths and recovery requirements before the first destructive run. -->
4. Then roll out to dev-worker-1, -2 and -4 the same way (same tool, no drift between workers). Run a dry run on each.

<!-- codex-review-status: complete -->