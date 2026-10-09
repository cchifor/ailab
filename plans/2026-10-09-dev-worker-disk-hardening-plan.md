# 2026-10-09 — dev-worker disks: from firefighting to bounded growth

## Context

Four disk-full incidents in nine days on the dev-workers (10-01 worktree deps, 10-06 anonymous
volumes, 10-09 agent tarball archives on dw3, 10-09 dw2 `/` at 98%). Each fix closed one
accumulator; the next one filled the disk. This plan targets the class, not the instance.

### Evidence (measured 2026-10-09; Prometheus 15 days, live survey of dw1-dw4)

| | dw1 | dw2 | dw3 | dw4 |
|---|---|---|---|---|
| `/` min free (15d) | 0.8% | 1.0% | 2.2% | 5.5% |
| `/workspace` min free (15d) | **0.0%** | 0.4% | **0.0%** (101 h < 5%) | 11.9% |
| worst `/workspace` consumption in 6 h | 36.5 GB | 36.0 GB | 36.6 GB | 24.8 GB |
| worst `/` consumption in 6 h | 9.2 GB | 13.2 GB | 8.6 GB | 4.8 GB |
| disk-guard triggered / exhausted (7d) | 0.7 h / 0 | 25.5 h / 25.5 h | 158 h / 100 h | 0.2 h / 0 |

Disks are 40 GB (`/`: OS + `/home` + `/tmp`) and 125 GB (`/workspace`: worktrees + docker/containerd).
The worst 6 h windows consumed up to ~36 GB on `/workspace` and ~13 GB on `/` — bursts, not a
sustained rate, but at that pace the 15% trigger leaves about 3 h on either disk.

What filled them, by category (today's survey):
- **Agent-made non-git data in `/workspace/<user>`** (nothing owns it, nothing bounds it):
  dw3 `forge-work` 32 GB + archives 50 GB (moved to NAS today), dw2 `forge-maintenance` 17.8 GB,
  dw4 `.private` 23 GB, dw1 `wt/` 14 GB (a directory of worktrees, not itself one). dw4 has 52
  non-git top-level dirs of 96.
- **Tool caches, mostly on the 40 GB `/`**: `~/.cache/uv` 1.1-4.3 GB, ms-playwright 0.7-1.4 GB,
  `~/.npm` 2.2-2.3 GB; agent-redirected ones on `/workspace`: dw2 `/workspace/c4/.cache` 6.8 GB +
  `.cache-npm` 5.6 GB, dw4 shared Rust target `/workspace/c4/.target` 12.9 GB.
- **`/tmp` on `/`**: dw2 9.7 GB (pytest temp 3.1 GB, node compile cache 0.7 GB), dw3 3.2 GB
  (`/tmp/claude-1000` task output 2.4 GB) — all younger than the 8 h aging.
- **Docker, reclaimable but only touched under pressure**: dw1 26.4 GB unused images (388 images,
  0 containers); dw2 29 GB build cache 12 h after the daily 20 GB cap freed 15 GB, plus 18 GB
  unused images; named volumes of stopped stacks 5.7-6.2 GB on dw2-dw4.
- **Never pruned by anything**: codex daemon releases (fixed in #1183, live 10-09), `~/.codex`
  sessions, journald (dw4 1.0 GB, no cap), apt cache.

### Tooling review (files/cleanup 1.6k lines, files/disk-guard, timers, alerts)
- **Alerting is broken where it matters.** Every disk-guard run's first heartbeat writes
  `exhausted=0`/`failed_steps=0`, so a ladder that outlasts one 30 s scrape resets the 15 m `for:`
  every 5 min: dw3 was exhausted ~100 h in 7 days and the alert recorded 33 firing samples, vs
  1287 on dw2 whose ladder takes seconds. `DevWorkerDiskFilling` fires at 12% free, below the
  guard's 15% trigger. No time-to-full alert, no inode alert, no promtool tests for disk rules.
- **`/` has almost no reclaim.** Under `/` pressure the ladder can only prune codex releases and
  worktree deps under `/home`; `/tmp` < 8 h, `~/.cache`, `~/.npm`, `~/.codex/sessions`, journald and
  non-git `$HOME` data are untouched. No TMPDIR/cache redirection exists; the uv cache on `/` with
  venvs on `/workspace` cannot hardlink, so every venv is a full copy.
- **Bounded only on a calendar or under pressure**: build cache (daily cap), images (pressure
  only, by creation date), worktree deps. Compose stacks are never removed (`--keep-stacks`), and a
  stopped stack pins its worktree — mutual pinning only a human breaks.
- **Placement drift**: dw1/dw2 run docker on `/workspace/containerd-root`, dw3/dw4 on
  `/workspace/containerd`; no containerd config is managed by ansible.
- **Guard edge cases**: docker steps defer indefinitely on an argv heuristic (no deferral metric);
  `builder prune -af` re-runs every 5 min under sustained pressure, discarding all warm cache; the
  heartbeat is only written between steps (steps may run 45 min vs the 30 min Stale threshold); the
  daily buildx/volume prune has no busy gate and no shared lock with the guard.

## Approach

Principles:
1. **Detect independently of the reclaimer.** Low space must page whether or not the guard is
   healthy; the guard's own state is diagnostic context.
2. **Reclaim is soft; say so.** Every cap here is a periodic target, not a guarantee: active,
   protected or young data can hold a disk over it. Each mechanism exports what it could NOT reclaim
   (protected bytes, no-safe-candidate state) so "over budget and stuck" is visible, and a named
   responder (the operator, via ntfy) gets an alert that names the directory.
3. **Prefer native, bounded mechanisms** (BuildKit GC, tool-native cache cleanup, app-native
   retention) over bespoke state databases and generic deletion of live data.
4. **Agent-scaled temp and caches leave the OS disk via the agents' own environment**, not by
   re-plumbing system `/tmp`, so a full `/workspace` cannot also break system services and the
   recovery tooling.
5. **Nothing new deletes without a report mode, an independent off switch, and a canary that
   lives through the policy's full time window.**

### P0 — this week

**W1. Alerts that work** (`dev-workers-rules.yaml` + `.test.yaml`, disk-guard)
- `DevWorkerDiskLow`: < 15% free for 30 m on either mount, from node_exporter alone — fires
  whether the guard is healthy, hung, missing or failing. `DevWorkerDiskFilling` (12%) stays as the
  critical tier. Exhausted/Deferred/Failing become diagnostic warnings joined to the filesystem by
  `instance` + `mountpoint` (guard metrics gain a `mountpoint` label where they are per-filesystem).
- disk-guard result state: the heartbeat carries the last COMPLETED run's `exhausted` /
  `failed_steps` per filesystem until the current run completes; a completed healthy run clears it;
  missing/unparseable state is exported as unknown (`state_known 0`), never as healthy. New
  `last_completed_timestamp_seconds` beside the heartbeat, and a `last_progress` timestamp so a hung
  step is distinguishable from a slow one. Atomic writes stay (tmp + rename, one flocked writer).
- `DevWorkerDiskTimeToFull` as a WARNING (not a page): `predict_linear` over 1 h, 4 h horizon, gated
  on < 30% free; tuned by replaying the 15 days of history (count fires vs real < 5% episodes, and
  fires during build-then-reclaim cycles) before it is enabled. Abrupt allocations are covered by
  DiskLow/DiskFilling, not by the forecast.
- `DevWorkerInodesLow` (< 10% free inodes or < 200k free) as a warning with a runbook response
  (find the inode-heavy dir; the byte ladder does not trigger on inodes).
- `DevWorkerDiskGuardDeferred`: deferral start is tracked across runs (persisted, reset only when a
  docker step actually runs), exported with the blocking reason; alert at > 1 h while the docker fs
  is short.
- promtool tests for every disk rule driven by the sample sequence the Python heartbeat actually
  produces (including a ladder longer than one scrape, recovery without a ladder, both filesystems
  short, missing scrapes). Deployment check: rules loaded in Prometheus, one test notification
  through Alertmanager/ntfy.

**W2. Guard reliability before more reclaim** (disk-guard, cleanup, docker.yml)
- Per-step time budgets well under the Stale window, and a heartbeat thread that updates
  `last_progress` only while the step's process is alive (a hung step still alerts via
  last_completed).
- One shared lock for every reclaim entrypoint (disk-guard, deps-prune timer, buildx/volume timer,
  manual `cleanup`); the daily docker prunes take the same busy gate as the guard.
- Under sustained pressure, the full `builder prune -af` runs at most once per hour; other ticks
  reclaim toward the target with `--max-used-space`.
- Elapsed deferral never overrides a protection. At critical space with docker busy, the guard
  escalates (alert) instead of deleting.

**W3. Native BuildKit GC** (`templates/daemon.json.j2`)
- Enable the daemon's built-in builder GC (`"builder": {"gc": {"enabled": true,
  "defaultKeepStorage": "20GB"}}`) so the build cache is bounded continuously by docker itself.
  Verify the builder in use is the default `docker` driver for both root and the agent user (no
  `docker-container` builders, which keep their own cache); keep the daily prune as a backstop.

**W4. Agent temp and caches off `/` — via the agents' environment** (`agent-shell-env.sh.j2`,
claude/codex launch env, tmux, `environment.d` for user services)
- `TMPDIR=/workspace/<user>/.tmp` for agent sessions (a per-user directory, mode 0700, aged 8 h
  by a tmpfiles rule like `/tmp`). Covers pytest, node, Python `tempfile`, and anything honouring TMPDIR;
  hard-coded `/tmp` writers stay on `/` and stay monitored. System `/tmp` is not touched.
- Cache env with explicit per-tool paths under `/workspace/<user>/.cache/`: `UV_CACHE_DIR`,
  `npm_config_cache`, `PIP_CACHE_DIR`, `PLAYWRIGHT_BROWSERS_PATH`, `GOMODCACHE`/`GOCACHE`, pnpm
  store; `XDG_CACHE_HOME` as the default for the rest. `CARGO_HOME`/`CARGO_TARGET_DIR` unchanged.
  Entrypoints: login + non-interactive SSH shells, tmux server env, the claude/codex launchers,
  systemd user units; documented gaps: cron, `sudo`, containers (they keep their own).
- No migration copy: new caches start empty; the old `~/.cache/*` / `~/.npm` are removed by the
  tool's own clean command once no process of that tool runs (one-shot task, report first).
- Verification is by running the tools through each entrypoint and checking where they actually
  write (effective config, created files, uv hardlink = same inode), not by grepping env.

**W5. Minimal attribution with a responder** (hourly timer, separate from the guard)
- `du -x --max-depth=2` of `/workspace/<user>` and `$HOME`, ionice idle, 10 min budget; results to a
  root-owned report file (full list, with scan completeness and duration) and to metrics: per-user
  totals, top-10 directories (paths relative to the user root, label-escaped, length-capped), and
  `scan_complete` + `last_scan_timestamp`. Partial scans publish last-good data marked stale.
- Growth is computed from the report history file (24 h ago vs now, per directory) BEFORE
  truncating to the top-10, so a new or fast-growing dir is caught even when it is not yet large.
- `DevWorkerDirGrowing` (a dir +15 GB in 24 h) and `DevWorkerUnownedDataLarge` (non-git
  directory, not archive/cache/tmp, > 20 GB) — both name the directory, route to the operator (ntfy),
  with the runbook action: ask the agent's owner, move to the NAS (runbook procedure), or archive.
  `git` classification is a hint only (a `.git` file = linked worktree; a grouping dir like `wt/` is
  neither), never a deletion criterion.
- Agent guide: name the scratch places (`/workspace/archive`, the cache dir, the tmp dir) and say
  other non-git data needs a stated owner and lifetime; large non-git dirs are reported to the
  operator.

### P1 — next two weeks

**W6. Root-disk headroom (decision).** Grow `/` from 40 to 60 GB (`dev_worker_rootfs_gb`, online
virtual-disk resize + `growpart`/`resize2fs`, per worker) as a cheap margin for what W4 cannot
move (hard-coded `/tmp` writers, agent CLIs, `$HOME` data). Requires the ai-node1/2 local-storage
headroom check first.

**W7. Docker hygiene, conservative**
- Images not referenced by any container (running or stopped) and created > 7 d ago: report daily,
  enforce after the report has run a full 7-day cycle on a canary. Locally built images whose name
  carries a protected prefix/label are never removed. No last-used state database.
- Compose stacks: a daily report of stopped stacks idle > 7 d (size of containers + named volumes,
  owner, worktree). Automatic `down` only for projects explicitly labelled disposable
  (`dev-worker.disposable=true`), removing only the validated stopped container IDs (no force), with
  their volumes' grace starting when they become orphaned. Everything else is report + alert.
- Docker space attribution: `docker system df -v` totals by type (images, containers' writable
  layers, volumes, build cache) as metrics, so docker's share of a peak is measured.
- Codify each worker's current containerd/docker placement in ansible as-is (no path convergence;
  that waits for W10).

**W8. Cache hygiene, tool-native only**
- Daily `uv cache prune` and npm `cache verify` per user, as that user; Playwright's own
  stale-browser removal. Per-tool cache sizes as metrics. Under `/workspace` or `/` pressure, the
  guard may run `uv cache clean` / `npm cache clean --force` only when no process of that tool runs.
  No generic whole-directory deletion of live caches; shared Rust targets are reported, not deleted.

**W9. Smaller gaps**: journald `SystemMaxUse=300M` + an initial `--vacuum-size`; apt
`Keep-Downloaded-Packages "false"` + initial `apt-get clean`; Claude transcript retention via the
managed `cleanupPeriodDays` (after confirming its scope); `~/.codex/sessions` reported, not deleted.

### P2 — after two weeks of W5/W7 data

**W10. Docker on its own disk** if the W7 attribution shows docker driving `/workspace` bursts:
scsi2 for the docker/containerd root, sized from measured peaks plus reserve; requires the Proxmox
thin-pool/snapshot headroom check (a second guest disk does not isolate a full backing pool) and a
per-worker maintenance window with a full writer shutdown and rollback.

**W11. Agents see the disk**: `/` and `/workspace` free space in the Claude statusline and the
tmux status bar.

**W12. Quotas revisit trigger**: if two exhaustion incidents happen within 30 days of P0+P1, revisit
ext4 project quotas for `/workspace/<user>` instead of waiting a fixed month.

### Not doing
- **Bind-mounting system `/tmp` onto `/workspace`**: couples system services, PrivateTmp units,
  sockets and the recovery tooling to the agents' busiest disk, and `RequiresMountsFor` would fail
  `tmp.mount` with a missing `/workspace` rather than fall back. W4 (agent TMPDIR) + W6 cover it.
- **An image last-used database, auto-down of unlabelled stacks, generic LRU deletion of live
  caches, elapsed-time overrides of protections**: risk out of proportion to the reclaim.
- **Growing disks as the only fix** (W6 is headroom on top of W1-W5, not a substitute).

## Critical files

- `ansible/roles/dev_worker/files/disk-guard` — result-state carry-forward, last_completed /
  last_progress, step budgets + heartbeat thread, shared lock, deferral tracking (W1, W2).
- `ansible/roles/dev_worker/files/cleanup` — shared lock, stack/image reports, opt-in disposable
  stacks (W2, W7).
- `ansible/roles/dev_worker/templates/daemon.json.j2` — BuildKit GC (W3).
- `ansible/roles/dev_worker/templates/agent-shell-env.sh.j2`, claude/codex launch env, tmux
  config, new `environment.d` drop-in — TMPDIR + cache env (W4).
- New `files/disk-report` + `tasks/disk_report.yml` (hourly attribution, W5).
- `ansible/roles/dev_worker/tasks/{disk_guard,docker,cleanup,tmp_hygiene}.yml`, `defaults/main.yml`
  — timers, toggles (every deleting feature: `report | enforce | off`).
- `kubernetes/infra/dev-workers/variables.tf` — `dev_worker_rootfs_gb` (W6, if decided).
- `kubernetes/apps/infrastructure/monitoring/dev-workers-rules.yaml` + `.test.yaml` (W1, W5).
- `scripts/tests/test_dev_worker_disk_guard.py`, `test_dev_worker_cleanup.py`, new
  `test_dev_worker_disk_report.py`.
- `docs/runbooks/dev-workers.md` § Disk full — alerts → actions, the NAS move procedure.

## Verification

- Unit: guard result-state transitions (healthy clears, missing = unknown, interrupted run),
  heartbeat thread vs hung step, shared lock contention, deferral persistence; report scan
  completeness/staleness, growth before truncation, label escaping; promtool tests fed the guard's
  real sample sequence.
- Ansible: `--check --diff` on a canary, a second run is idempotent (changed=0), tag-limited runs
  (`-t disk_guard`, `-t docker`, …) converge alone.
- W1 acceptance: replaying the 15-day history, DiskLow would have fired on every < 15% episode
  (dw1-dw4) and Exhausted would have stayed firing through dw3's 10-09 episode; a test
  notification reaches ntfy.
- W3 acceptance: build cache stays ≤ ~20 GB through a full day of agent builds on dw2.
- W4 acceptance: per entrypoint, pytest/uv/npm/playwright write under `/workspace/<user>/{.tmp,.cache}`;
  a new venv's files share inodes with the uv cache; `/` growth over 48 h of normal work < 1 GB.
- Deleting features (W7, W8 pressure cleans): report mode for the full policy window (7 d + grace)
  on a canary with aged disposable fixtures, then enforce on the canary with stop criteria (any job
  failure attributable to a removal, or reclaim yield below the report's estimate by > 50%).
- Fleet acceptance, 14 days after P0: every < 15% episode paged DiskLow within 30 m; no filesystem
  under 5% free; measured TimeToFull lead time reported (not promised); guard exhausted time per
  worker trends down. Track build failures, re-download cost and reclaim duration alongside.

## Review disposition (codex round 1, 80 findings)
All accepted; none pushed back. Themes and where they landed:
- Re-prioritization (guard reliability, native build-cache GC and minimal attribution into P0;
  deletion features after reliability): W1-W5 order.
- System `/tmp` bind mount (boot ordering, RequiresMountsFor failure, PrivateTmp, sockets, recovery
  domain, hidden root data): dropped → Not doing; replaced by agent TMPDIR (W4) + root headroom (W6).
- Cache env gaps (entrypoints, effective paths, lowercase npm var, migration safety, CARGO_HOME
  permissions): W4 explicit per-tool paths, no copy migration, CARGO unchanged.
- Cache caps (native commands are not byte caps, uv forbids direct cache edits, mtime ≠ use,
  hardlink accounting, per-user budget unjustified): generic cap dropped → W8 tool-native only.
- Image last-used state, stale-stack auto-down (down removes writable layers immediately, restart
  races, mutable compose files, volume grace from orphaning): W7 report-first, opt-in disposable
  label, no state DB.
- Alerts (DiskLow must not depend on guard health, mount-label joins, predict_linear is a horizon
  not a lead time, inode response, deferral persistence, deployment + notification checks): W1.
- Attribution (scan completeness, label churn/escaping/privacy, growth before truncation, git
  classification is a hint, aggregate growth): W5.
- containerd convergence vs separate disk (do not migrate twice; thin-pool headroom): W7 codify
  as-is, W10.
- Verification (48 h < policy windows, idempotence, real tool behaviour over env grep,
  unachievable "every episode preceded"): Verification section.

<!-- codex-review-status: complete -->
