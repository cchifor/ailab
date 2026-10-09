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
Peak fill rates are ~6 GB/h on `/workspace` and ~2 GB/h on `/`: from the 15% trigger to full is
~3 h on either disk at peak.

What filled them, by category (today's survey):
- **Agent-made non-git data in `/workspace/<user>`** (nothing owns it, nothing bounds it):
  dw3 `forge-work` 32 GB + archives 50 GB (moved to NAS today), dw2 `forge-maintenance` 17.8 GB,
  dw4 `.private` 23 GB, dw1 `wt/` 14 GB. dw4 has 52 non-git top-level dirs of 96.
- **Tool caches, mostly on the 40 GB `/`**: `~/.cache/uv` 1.1-4.3 GB, ms-playwright 0.7-1.4 GB,
  `~/.npm` 2.2-2.3 GB; agent-redirected ones on `/workspace`: dw2 `/workspace/c4/.cache` 6.8 GB +
  `.cache-npm` 5.6 GB, dw4 shared Rust target `/workspace/c4/.target` 12.9 GB.
- **`/tmp` on `/`**: dw2 9.7 GB (pytest temp 3.1 GB, node compile cache 0.7 GB), dw3 3.2 GB
  (`/tmp/claude-1000` task output 2.4 GB) — all younger than the 8 h aging.
- **Docker, reclaimable but only touched under pressure**: dw1 26.4 GB unused images (388 images,
  0 containers); dw2 29 GB build cache 12 h after the daily 20 GB cap freed 15 GB, plus 18 GB
  unused images; named volumes of stopped stacks 5.7-6.2 GB on dw2-dw4.
- **Never pruned by anything**: codex daemon releases (3.4-4.5 GB each, fixed in #1183),
  `~/.codex` sessions, journald (dw4 1.0 GB, no cap), apt cache.

### Tooling review (files/cleanup 1.6k lines, files/disk-guard, timers, alerts)
- **Alerting is broken where it matters.** Every disk-guard run's first heartbeat writes
  `exhausted=0`/`failed_steps=0` (disk-guard `guard()` → `progress(m)` on trigger), so a ladder that
  takes longer than one 30 s scrape resets the 15 m `for:` every 5 min: dw3 was exhausted ~100 h in
  7 days and the alert recorded 33 firing samples, vs 1287 on dw2 whose ladder is seconds long.
  `DevWorkerDiskFilling` fires at 12% free, BELOW the guard's 15% trigger. No time-to-full
  (`predict_linear`) alert, no inode alert, no promtool tests for any disk rule.
- **`/` has almost no reclaim.** Under `/` pressure the ladder can only prune codex releases and
  worktree deps under `/home`; `/tmp` < 8 h, `~/.cache`, `~/.npm`, `~/.codex/sessions`, journald and
  non-git `$HOME` data are untouched. No TMPDIR/XDG_CACHE_HOME redirection exists; the uv cache on
  `/` with venvs on `/workspace` cannot hardlink, so every venv is a full copy.
- **Everything bounded is bounded on a calendar or under pressure only**: build cache (daily cap),
  images (pressure only, by creation date not last use), worktree deps (daily 14 d / pressure 3 d).
  Compose stacks are never removed automatically (`--keep-stacks`), and a stopped stack pins its
  worktree (cleanup counts a stopped container's compose working_dir as in use) — mutual pinning
  only a human breaks.
- **Placement drift**: dw1/dw2 run docker on `/workspace/containerd-root`, dw3/dw4 on
  `/workspace/containerd`; no containerd config is managed by ansible (hand-config).
- **Guard edge cases**: docker steps defer indefinitely while any `docker … build|pull` argv exists
  (no deferral bound, no deferral alert); `builder prune -af` re-runs every 5 min under sustained
  pressure and discards all warm cache; heartbeat only between steps (steps may run 45 min vs the
  30 min Stale threshold); the daily buildx/volume prune has no busy gate.

## Approach

Principles:
1. **`/` is for the OS.** Nothing that scales with agent work lives there: `/tmp`, tool caches and
   agent scratch move to `/workspace`. `/` then only needs journald/apt caps.
2. **Every accumulator on `/workspace` is bounded continuously** (a size cap enforced every tick,
   like the #1183 archive cap), not on a calendar, and not only once the disk is already short.
3. **Unbounded agent data is attributed early**, by directory, before it threatens the disk — the
   guard cannot (and must not) delete live work, so a human has to hear about it hours earlier.
4. **Alerts fire on trajectory**, not only on a static threshold that leaves ~3 h at peak rate.

### P0 — this week (small, high value)

**W1. Make the alerts work.** (`dev-workers-rules.yaml`, disk-guard, promtool tests)
- disk-guard keeps exporting the PREVIOUS run's `exhausted`/`failed_steps` until the current run
  finishes (heartbeat carries them forward, read back from the prom file at start); test it.
- `DevWorkerDiskTimeToFull`: `predict_linear(node_filesystem_avail_bytes[1h], 4*3600) < 0` and
  avail < 30%, for 15 m, both mounts — fires ~4 h ahead at the measured peak rates.
- `DevWorkerDiskFilling` (12%) stays as the last line; add `DevWorkerDiskLow` at < 15% for 30 m
  while the guard is exhausted OR deferred, so the band between the guard's trigger and the static
  alert is no longer silent.
- `DevWorkerInodesLow` (< 10% free inodes) on both mounts.
- `DevWorkerDiskGuardDeferred`: docker steps deferred for > 1 h while the docker fs is short.
- promtool unit tests for all disk rules (`dev-workers-rules.test.yaml`), including the
  heartbeat-reset regression.

**W2. Move agent-scale data off `/`.** (new `tasks/disk_layout.yml`, `agent-shell-env.sh.j2`)
- `/tmp` → bind mount of `/workspace/.tmp` (systemd `tmp.mount` drop-in: `What=/workspace/.tmp`,
  `Options=bind`, `RequiresMountsFor=/workspace`; if `/workspace` is missing `/tmp` stays on `/`).
  tmpfiles 8 h aging unchanged. Migration: on the next reboot (no live move of an in-use /tmp).
- Tool caches → `/workspace/<user>/.cache` via env for every agent entrypoint (login shells,
  tmux, systemd user units, the claude/codex launchers): `XDG_CACHE_HOME`, `UV_CACHE_DIR`,
  `npm_config_cache`, `PLAYWRIGHT_BROWSERS_PATH`, `PIP_CACHE_DIR`, `GOMODCACHE`/`GOCACHE`,
  `CARGO_HOME` stays shared in `/opt/rust` (read-mostly) but `CARGO_TARGET_DIR` is NOT set
  globally. One-time migration: move existing `~/.cache/*`, `~/.npm/_cacache` (rsync, then remove).
  Bonus: uv hardlinks into venvs on the same filesystem.
- journald `SystemMaxUse=300M`; apt `Keep-Downloaded-Packages "false"`.
- Result: `/` holds OS + `$HOME` config + the agent CLIs (~15-20 GB), leaving > 50% free.

### P1 — next two weeks (bounded growth on `/workspace`)

**W3. Continuous docker caps** (disk-guard pre-ladder "caps" phase, every tick, busy-gated):
- build cache: `buildx prune --max-used-space` at the cap (20 GB) whenever above it — the daily
  timer stays as a backstop; under pressure the ladder keeps `-af`, but at most once per hour.
- images: remove images no container (running or stopped) uses and that were not used for 7 d —
  "used" = a container created from it, tracked by the guard in a small state file, since docker
  has no last-used field; first rollout in report mode.
- stale compose stacks: a stack whose containers have all been stopped ≥ 7 d (and whose
  working_dir worktree is idle ≥ 7 d) is `compose down` (containers + its named volumes are kept
  for another 7 d as orphan volumes, then removed). Report mode first. Breaks the stack↔worktree
  mutual pin.
- Layout: manage `/etc/containerd/config.toml` (root/state) in ansible and converge dw1-dw4 onto ONE
  path (requires a docker stop + move per worker; scheduled per worker).

**W4. Cache caps** (same caps phase): `/workspace/<user>/.cache` total ≤ 15 GB per user: over the
cap, prune per tool with its native safe command first (`uv cache prune`, `npm cache verify`,
playwright `uninstall` of unreferenced revisions), then delete whole tool subdirs least-recently
modified first (caches are regenerable by definition), never one modified within 1 h.
Shared Rust targets (`/workspace/<user>/.target`-style `CARGO_TARGET_DIR`s) get the same treatment
as deps: removable when untouched 3 d.

**W5. Attribution for agent data** (hourly, separate timer — not every 5 min):
- `du -x --max-depth=1` of `/workspace/<user>` and `$HOME` (ionice idle, 10 min budget) → textfile
  metric `dev_worker_dir_bytes{dir,git="true|false"}`, top 25 per user + an "other" bucket.
- `DevWorkerDirGrowing`: a single dir grew > 15 GB in 24 h; `DevWorkerUnownedDataLarge`: a non-git,
  non-archive, non-cache dir > 20 GB. Both name the dir in the alert — the forge archives (+50 GB in
  2-3 days) would have paged on day one.
- Agent guide (managed block): name the directories an agent may use for scratch (archive, cache,
  tmp) and say that everything else in `/workspace/<user>` must be a git worktree or be deleted
  when the task ends; non-git dirs over 20 GB are reported to the operator.

**W6. Smaller reclaim gaps in `cleanup`**: idle-worktree build outputs (`dist`, `.next`, `coverage`,
`test-results`, `.turbo`, `__pycache__`) join the deps step; `~/.codex/sessions` + Claude transcripts
older than 30 d (Claude: managed `cleanupPeriodDays: 30`); daily `git worktree prune` on main clones.

### P2 — structural (decide after P0/P1 data)

**W7. Docker on its own disk.** scsi2 (~100 GB) for the docker/containerd root, so a docker fill
cannot block git/edits in worktrees and vice versa; `/workspace` stays for worktrees. Needs the
Proxmox local-storage headroom check on ai-node1/2 first (dev-workers live there) and a per-worker
maintenance window; tofu + ansible change. Decide with 2 weeks of W5 data: if docker is < 25% of
`/workspace` peaks, skip.

**W8. Guard robustness**: bound the docker busy deferral (after 60 min of deferral with the fs under
10%, run anyway); heartbeat thread during long steps; busy gate for the daily buildx/volume prune;
codex in-use check also looks at cwd/maps.

**W9. Agents see the disk**: free space of `/` and `/workspace` in the Claude statusline
(`claude-statusline.js`) and the tmux status bar, red under 15%.

### Not doing
- Hard quotas (ext4 project quotas): invasive (fs feature flags, remount); revisit only if W5
  attribution + guide do not change behaviour within a month.
- Growing disks alone: peak fills of 36 GB in 6 h would refill any reasonable size.
- Auto-deleting non-git agent data outside the archive/cache/tmp dirs: it is live work.

## Critical files

- `ansible/roles/dev_worker/files/disk-guard` — heartbeat carry-forward (W1), caps phase (W3/W4),
  deferral bound + heartbeat thread (W8).
- `ansible/roles/dev_worker/files/cleanup` — stale stacks, image last-use, build outputs, sessions
  (W3/W6).
- `ansible/roles/dev_worker/tasks/{disk_guard,docker,cleanup,tmp_hygiene}.yml`, new
  `tasks/disk_layout.yml` (tmp bind mount, cache env, journald/apt caps, containerd config).
- `ansible/roles/dev_worker/templates/agent-shell-env.sh.j2` (+ systemd user env) — cache env (W2).
- `ansible/roles/dev_worker/defaults/main.yml` — caps and toggles, each feature with a
  report/enforce mode.
- `kubernetes/apps/infrastructure/monitoring/dev-workers-rules.yaml` + new
  `dev-workers-rules.test.yaml` cases — W1/W5 alerts.
- `scripts/tests/test_dev_worker_disk_guard.py`, `test_dev_worker_cleanup.py` — new behaviour,
  plus `main()` and the docker-loader parsing that are untested today.
- `docs/runbooks/dev-workers.md` § Disk full — rewritten around the new layout and alerts.
- Out of git today, to codify: `/etc/containerd/config.toml` on dw1-dw4.

## Verification

- Unit: pytest for every new guard/cleanup path (carry-forward heartbeat, caps phase with fake
  measures, cache cap order, stale-stack selection, image last-use state); promtool `test rules` for
  every disk alert, including "a ladder longer than one scrape does not reset Exhausted".
- Canary per workstream on one worker (dw3 for `/workspace`, dw2 for `/`), report mode first where a
  feature deletes, 48 h of reports reviewed before enforce, then the rest.
- W2 acceptance: after a reboot `/tmp` is on `/workspace` (`findmnt /tmp`), agent shells report the
  cache env (`env | grep CACHE`), new venvs hardlink (`uv` reports linked), `/` used < 50%.
- Fleet acceptance (14 days after P0+P1): no filesystem under 5% free; every sub-15% episode was
  preceded by a TimeToFull or DirGrowing alert; guard exhausted time < 2 h/week per worker.

<!-- codex-review-status: pending -->
