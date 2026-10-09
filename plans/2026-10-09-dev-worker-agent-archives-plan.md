# 2026-10-09 — dev-worker disk: agent archives + codex daemon releases

## Incident
dev-worker-3 had BOTH disks full at once: `/` (sda, 40G) 96% and `/workspace` (sdb, 125G) 95%.
`dev-worker-disk-guard` (#1084) was running and reported, every 5 min:
`EXHAUSTED — every step ran and a disk is still under 15.0% free; what is left is live work`.
So none of the 10-06 accumulators (build cache, anonymous volumes, merged worktrees, idle deps) was
the cause; the ladder ran clean and found nothing.

## Root cause (measured on dw3)
1. **The forge codex agent archives instead of deleting.** `/workspace/c4/forge-qualification-archives`
   = 41 GB (173 entries: ~20 `.tar.zst` of 1.6–2.9 GB each — whole worktree snapshots
   `full127/138/145/171` of the same tree at successive commits, stage artifact sets, diagnostics). Its
   own receipts state the policy: `"no artifact deletion"`, compiler-cache "retirement" that
   re-compressed a worktree into a new tarball with `"removed_entries": 0`. Its `disk-cleanup-20261008*`
   runs wrote preserved-before/after manifests and kept everything. Files I inspected are dated 10-07..10-09.
2. **It moved the overflow onto the OS disk.** `forge-full112-…relocation.json`: *"Relocate immutable
   historical archive to permanent home filesystem"* — but `/home` is on the 40G root disk.
   `~/forge-qualification-archives` 6.5 GB + `~/forge-evidence` 2.6 GB, wired back into `/workspace`
   by ~40 cross-filesystem symlinks. That is why `/` filled together with `/workspace`.
3. **An uncovered regenerable accumulator on `/`:** the codex app-server daemon keeps every release it
   auto-updates to: `~/.codex/packages/app-server-daemon/releases/<ver>`, ~425 MB each, never pruned.
   dw3 had 8 (3.4 GB), dw2 has 8 (3.4 GB, `/` 88%), dw4 has 11 (4.5 GB). Only `current` (0.162.0) runs.
4. **The agent guide nudged it.** The managed disk-hygiene block says "Keep artifacts you must preserve
   … outside worktrees you will delete", and says nothing about the root disk or archives.
5. **The EXHAUSTED log line is misleading**: it always says `du … /workspace`, even when only `/` is low.

## Done by hand on dw3 (incident unblock, 2026-10-09)
- Removed 7 non-current codex daemon releases (kept `current` and every release a running process
  executes from): `/` 96% → 85%. `uv cache prune` (nothing), `apt-get clean`.
- Archive offload (option (a): lossless): one `tar | zstd` stream of `/workspace/c4/forge-qualification-archives`,
  `~/forge-qualification-archives`, `~/forge-evidence` (symlinks + modes preserved, `--numeric-owner`)
  → `qnap-nfs:/dev-worker-archives/dev-worker-3/forge-archives-20261009.tar.zst` via ai-node1
  (`/mnt/pve/qnap-nfs`; the QNAP export is only on 10.55.x, so dev-workers cannot mount it).
  Verify: sha256 of the stream on dw3 == sha256 of the file on the NAS, `zstd -t`, `tar -t` member count.
  Then delete locally ONLY members of the tar not modified after the stream started, and leave a
  `MOVED-TO-NAS.md` in each emptied directory (where it went, how to restore, where new archives go).

## Design (this PR)
### A. disk-guard: new `codex-releases` step (step 0, only while `/` is low)
In-process step for `/home/*` on a still-short filesystem, in `.codex/packages/app-server-daemon/`.
Skip the home if the install is reached through a symlink (an ancestor or `releases/` itself), if
`current` does not resolve to a dir under `releases/`, or if codex's own `install.lock` is held
(non-blocking flock, opened O_NOFOLLOW).
Keep `current`'s target, the release named in `auto-update-version`, any release containing a running
process' executable (`/proc/*/exe`) and any release changed < 1 h ago. Re-read `current` before every
removal; if it moved, stop. Removal = `rm -rf --one-file-system` **as the release dir's owner**; uid 0
is refused outright (so `/root` is not a codex home here). `--dry-run` lists what it would remove.

### B. A capped archive directory for agents: `/workspace/archive`
- Role: `dev_worker_archive_dir` (default `{{ dev_worker_workspace_mount }}/archive`, 2775
  agent-owned, created by `disk_guard.yml` after asserting the data disk is mounted),
  `dev_worker_archive_max_gb` (default 20, GB = 10^9).
- disk-guard `--archive-dir D --archive-max-gb N` on EVERY run (independent of the low-disk gate):
  best-effort retention. Per top-level entry: allocated bytes (lstat, symlinks never followed, one
  filesystem) and newest change = max(mtime, ctime) anywhere in it (ctime because `mv` into the
  archive keeps the old mtime; ctime cannot be set back). A file linked from several entries counts
  once, and only removing its last entry frees it (per-inode reference counts, multiply-linked
  files only). While over the cap, remove the least recently changed entry, never one changed
  < 1 h ago (then log that everything left is young). The scan has a 120 s budget, checked per entry
  and every 256 directory entries; past it nothing is removed that run and `archive_scan_timeout=1`
  is exported with the bytes counted so far. Each entry is removed as its own owner, never root.
  Metrics `dev_worker_disk_guard_archive_bytes`, `_archive_max_bytes`, `_archive_removed_bytes`
  (this run, as scanned), `_archive_scan_timeout`; alert `DevWorkerArchiveOverCap` (over the cap or
  timing out, for 2h). The dir is owned like the mount (agent user, admin group, setgid).
- Nothing outside that directory is ever removed by the cap.

### C. Agent guide (managed block in `~/.claude/CLAUDE.md` + `~/.codex/AGENTS.md`)
Replaces "keep artifacts outside worktrees": `/`, `$HOME`, `/tmp` are one ~40 GB OS disk, never for
bulk data or "relocations"; evidence goes in `/workspace/archive` (capped, oldest-changed first);
what must outlive the cap is summarized in the PR/issue or moved to the NAS by the operator (no large
binaries in git); cleaning up means deleting, and a tarball of rebuildable output frees nothing,
while work that exists nowhere else is kept (only that part).

### D. EXHAUSTED says what it knows
"every step ran, eligible reclaim was not enough: / (5.0%) is still under 15% free" — names the
filesystems actually still low. Then the 8 largest `du -x --max-depth=2` directories of each, each
filesystem at most once an hour (stamp per filesystem in /run, never written by `--dry-run`), du
timeout 300 s logged as such. The report runs in `main` AFTER the final metrics write, so a slow du
never delays the heartbeat. Alert text points at those lines and at `$HOME`.

## Tests (scripts/tests/test_dev_worker_disk_guard.py, 62)
Ladder order with codex-releases first and only for `/`; codex-releases keeps current /
auto-update-version / running / young, skips a dangling or foreign `current` and a held
install.lock (real flock), stops when `current` moves, never follows a symlinked release, removes
as the releases dir's owner, dry-run lists only; archive trim oldest-changed first to the cap,
newest change inside an entry counts, a moved-in entry is young by ctime (real clock), never a
young entry, all-young over cap logged, hard links once, scan budget removes nothing, symlinked
entry removes only the link, symlinked/missing archive dir untouched, dry-run, failed removal not
subtracted, `remove_as_owner` real removal; exhausted names the low fs and reports it, no report
when not exhausted, dry-run previews only in-process steps; du report per-filesystem hourly,
timeout logged; prom renders archive metrics only when capped.

## Codex plan review (2026-10-09, reviewer-2 seat c, gpt-6-astra xhigh)
Accepted: installer-lock + re-check of `current` (#1); ctime-based age so a moved-in entry is not
instantly eligible (#3); restore-audit the offload with per-file manifests and delete per file (#4);
"best effort" + over-cap alert (#5); scan budget (#7); hard-link dedup, no subtraction on a failed
removal (#8); guide wording: a snapshot can hold unique work, NAS via the operator, archive dir now
an explicit exception to "never untracked files" (#9); EXHAUSTED wording, per-fs du throttle,
timeouts logged (#10); mount check before creating the archive dir, real dry-run preview, canary
rollout (#12); units/metric semantics, "a manual tick does not bypass the gate" (#13).
Already covered by the implementation (the review saw only the plan): deletion as the directory's
owner instead of root — whatever a swapped symlink points at, only that user's files are reachable
(#2); archive cap independent of the low-disk gate, dry-run writes no stamps (#6).
Rejected: a staging/publication protocol for archive entries (#3) — an hour of quiet is the
completion signal, documented to the agents; descriptor-pinned deletion / bind-mount detection (#2) —
the owner-uid rm bounds the blast radius to what the agent could delete itself; inode exhaustion
(#11) — real, but not this incident (/workspace 39% inodes): follow-up.

## PR review round 1 (#1183: reviewer-codex, reviewer-claude, on 06f03a58)
All accepted and fixed in the next push:
- codex: "never as root" was not enforced (uid 0 ran rm as root; a `releases` symlink's lstat owner
  stood in for the target's). Now uid 0 is refused, `/root` dropped, installs reached through a
  symlink skipped, and every entry is removed as ITS OWN owner.
- codex: the scan deadline missed flat files and wide directories. Now checked per entry, every 256
  directory entries and once more before any removal.
- codex + claude: a shared hard-linked inode was charged to the first entry scanned, so removing it
  could report the archive under its cap without freeing the blocks. Now exact per-inode reference
  counts.
- claude: a timed-out scan exported no archive metrics, silencing the alert exactly when the archive
  is huge. Now `archive_scan_timeout` + a lower-bound size, and the alert fires on it.
- claude: the du report ran before the final heartbeat (up to 10 min). Moved after it.
- claude: one owner for a dir the guide advertises to every user. Admin group + setgid, and removal
  as the entry's owner.

## Rollout
PR -> CI -> reviewbot. Canary dev-worker-3 first: `--tags disk_guard -l dev-worker-3`, then
`sudo systemctl cat dev-worker-disk-guard` and the same ExecStart with `--dry-run`; check the guide
block in both files. Then dev-worker-2 (`/` at 88%: the codex step fires on its first tick, ~3 GB),
then dev-worker-1 and -4. A manual `systemctl start dev-worker-disk-guard` does NOT bypass the
pressure gate — the codex step runs only while `/` is under 15% free. Per worker: metrics present,
`journalctl -u dev-worker-disk-guard -n 30`.

## Not doing
- Growing the disks: ~50 GB in 2-3 days of archives would refill any reasonable size.
- Auto-deleting agent data outside `/workspace/archive` (forge-work, $HOME): that is live work.
- Inode-pressure alerting: follow-up.
