# 2026-10-09 — dev-worker disks: from firefighting to bounded growth

## Codex Review

- The measured evidence, heartbeat fix, and protection of live work provide a strong basis; the evidence below is accepted as supplied.
- P0 should include guard reliability, basic directory attribution, and native Docker cache limits before moving more data onto `/workspace`; the global `/tmp` migration needs a separate rollout gate.
- Binding `/tmp` to an already capacity-constrained `/workspace` couples system services and recovery tools to agent disk usage; boot fallback, PrivateTmp, sockets, and full-filesystem behavior need explicit tests.
- The 15 GB cache policy and age-based retention are soft targets, not guaranteed bounds; an aggregate capacity budget and a response when nothing is safely reclaimable are missing.
- Image history and automatic stack removal introduce substantial state and deletion risks; conservative ownership rules, concurrency protection, failure handling, and complete retention-cycle verification must precede enforcement.

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
   <!-- codex: Relocation transfers demand onto a filesystem already observed full; establish destination headroom and recovery behavior before migration, and retune forecasts for the combined workload. -->
2. **Every accumulator on `/workspace` is bounded continuously** (a size cap enforced every tick,
   like the #1183 archive cap), not on a calendar, and not only once the disk is already short.
   <!-- codex: These are periodic reclamation targets, not hard bounds: active files, busy deferrals, protected recent data, and age-only image retention can all exceed them indefinitely. State this limitation and define how over-budget but unreclaimable usage is surfaced. -->
3. **Unbounded agent data is attributed early**, by directory, before it threatens the disk — the
   guard cannot (and must not) delete live work, so a human has to hear about it hours earlier.
   <!-- codex: Attribution needs a named responder and an actionable escalation path, including pausing new disk-heavy jobs when safe reclaim is exhausted; otherwise the largest measured accumulator remains operationally unbounded. -->
4. **Alerts fire on trajectory**, not only on a static threshold that leaves ~3 h at peak rate.

### P0 — this week (small, high value)

**W1. Make the alerts work.** (`dev-workers-rules.yaml`, disk-guard, promtool tests)
- disk-guard keeps exporting the PREVIOUS run's `exhausted`/`failed_steps` until the current run
  finishes (heartbeat carries them forward, read back from the prom file at start); test it.
  <!-- codex: Define state transitions for successful no-pressure runs, partial failures, crashes, and reboots so previous exhaustion clears after recovery without treating interrupted work or missing state as healthy. Preserve results per filesystem rather than letting progress on one clear failure on another. -->
  <!-- codex: Use atomic publication and one coordinated writer for heartbeat and completion updates, and test missing, truncated, incompatible, and unwritable prom files. Export last-completed time separately from heartbeat so carried-forward results have a visible age. -->
- `DevWorkerDiskTimeToFull`: `predict_linear(node_filesystem_avail_bytes[1h], 4*3600) < 0` and
  avail < 30%, for 15 m, both mounts — fires ~4 h ahead at the measured peak rates.
  <!-- codex: Four hours is the projection horizon, not guaranteed warning time: the historical fit, 30% gate, evaluation cadence, and 15-minute hold all affect delivery, while sudden allocations can bypass it entirely; see [Prometheus predict_linear](https://prometheus.io/docs/prometheus/latest/querying/functions/#predict_linear). Measure actual lead time rather than using the horizon as an SLA. -->
  <!-- codex: Replay large build-then-reclaim cycles and post-prune recoveries before paging on this rule, comparing short- and long-window confirmation and warning versus paging severity. Tune against false pages without suppressing the independent critical-space alert whenever a build is active. -->
- `DevWorkerDiskFilling` (12%) stays as the last line; add `DevWorkerDiskLow` at < 15% for 30 m
  while the guard is exhausted OR deferred, so the band between the guard's trigger and the static
  alert is no longer silent.
  <!-- codex: This band remains silent when the guard is hung, missing, failing before completion, or still reclaiming with previous exhausted=0. Make sustained low-space detection independent of guard health and use exhaustion/deferral as diagnostic context or separate alerts. -->
  <!-- codex: Specify worker and mount selectors plus explicit vector matching between filesystem and guard metrics; host-level Docker deferral must not be attributed to the wrong filesystem. Avoid duplicate alerts for the proposed /tmp bind alias. -->
- `DevWorkerInodesLow` (< 10% free inodes) on both mounts.
  <!-- codex: An inode alert alone does not activate a byte-triggered reclaim ladder; define an inode-pressure response and verify that candidate deletion actually frees inodes. Test filesystems with unavailable inode statistics and choose severity using absolute headroom as well as percentages. -->
- `DevWorkerDiskGuardDeferred`: docker steps deferred for > 1 h while the docker fs is short.
  <!-- codex: Track the start of continuous eligible-but-deferred work across runs, with explicit recovery/reset rules; resetting a timestamp each tick recreates the heartbeat bug. Export the blocking reason so a false busy match can be distinguished from a real build. -->
- promtool unit tests for all disk rules (`dev-workers-rules.test.yaml`), including the
  heartbeat-reset regression.
  <!-- codex: Rule correctness is insufficient without loaded-rule and notification checks: verify deployment, Alertmanager routing/grouping, an actual test notification, and an operator runbook. Include missing/stale guard data and missing or read-only mounts rather than allowing absent series to look healthy. -->

**W2. Move agent-scale data off `/`.** (new `tasks/disk_layout.yml`, `agent-shell-env.sh.j2`)
<!-- codex: Split this workstream: verified cache redirection and per-agent TMPDIR are suitable P0 mitigations, while the global /tmp bind needs a separate migration gate. TMPDIR only helps programs that honor it, so retain root monitoring and handle remaining hard-coded /tmp writers explicitly. -->
- `/tmp` → bind mount of `/workspace/.tmp` (systemd `tmp.mount` drop-in: `What=/workspace/.tmp`,
  `Options=bind`, `RequiresMountsFor=/workspace`; if `/workspace` is missing `/tmp` stays on `/`).
  tmpfiles 8 h aging unchanged. Migration: on the next reboot (no live move of an in-use /tmp).
  <!-- codex: The claimed fallback is not established: RequiresMountsFor adds Requires and After dependencies, so a failed workspace mount can fail tmp.mount and its dependents even though the underlying /tmp directory remains; see [Ubuntu systemd.unit](https://manpages.ubuntu.com/manpages/noble/man5/systemd.unit.5.html). Define degraded-boot behavior explicitly and prevent agent services from silently writing into an unmounted /workspace directory on root. -->
  <!-- codex: Inspect the installed/generated or masked tmp.mount and provide a complete effective unit, including the appropriate type and target, rather than assuming a usable base unit exists. Provision .tmp on the verified workspace filesystem before mounting it, without creating a cycle through ordinary post-mount tmpfiles setup. -->
  <!-- codex: Require root ownership, mode 1777, and a trusted parent for the shared temporary directory, and reject a substituted symlink or mount source. Check effective bind-mount security flags explicitly; any noexec choice needs agent/build compatibility testing. -->
  <!-- codex: A full /workspace now also prevents new temporary files and filesystem socket creation for system tools, potentially obstructing recovery despite free root space. Keep guard locks/state and a tested emergency temporary location outside that failure domain, or retain a separate bounded system /tmp. -->
  <!-- codex: Ubuntu 24.04 PrivateTmp services acquire dependencies on the mounts backing /tmp and /var/tmp, so this can affect service startup and failure propagation; /var/tmp is not relocated by this proposal. Test actual private namespaces and sandbox write restrictions, as described in [Ubuntu systemd.exec](https://manpages.ubuntu.com/manpages/noble/man5/systemd.exec.5.html). -->
  <!-- codex: Test long-lived tmux and ssh-agent sockets plus active task output across the 8-hour cleanup boundary, including traversal through the .tmp alias. If sockets move to /run/user/<uid>, verify ownership and logout/linger lifetime rather than assuming the directory survives detached sessions. -->
  <!-- codex: Mounting over /tmp hides existing root-disk files instead of reclaiming them; remove eligible old contents while offline before the bind and verify released root blocks. Never mount over a live fallback /tmp after services have created sockets there, and exclude .tmp from unrelated workspace cleanup. -->
- Tool caches → `/workspace/<user>/.cache` via env for every agent entrypoint (login shells,
  tmux, systemd user units, the claude/codex launchers): `XDG_CACHE_HOME`, `UV_CACHE_DIR`,
  `npm_config_cache`, `PLAYWRIGHT_BROWSERS_PATH`, `PIP_CACHE_DIR`, `GOMODCACHE`/`GOCACHE`,
  `CARGO_HOME` stays shared in `/opt/rust` (read-mostly) but `CARGO_TARGET_DIR` is NOT set
  globally. One-time migration: move existing `~/.cache/*`, `~/.npm/_cacache` (rsync, then remove).
  Bonus: uv hardlinks into venvs on the same filesystem.
  <!-- codex: The entrypoint list misses system services using User=, cron, noninteractive SSH, sudo, and container environments; user environment.d applies to services launched by the user manager, not every process, per [Ubuntu environment.d](https://manpages.ubuntu.com/manpages/noble/man5/environment.d.5.html). Existing tmux servers, panes, user managers, and daemons also need an explicit environment-refresh or restart procedure. -->
  <!-- codex: Publish exact per-tool destination paths and check effective tool configuration, since XDG is not universal and command/project settings can override environment defaults. Include any deployed pnpm/Yarn caches, npm npx/log directories, and existing alternate cache paths without moving persistent data or credentials into an eviction tree. -->
  <!-- codex: Cross-filesystem rsync followed by removal is unsafe with concurrent writers and requires destination headroom plus an idempotent recovery path after interruption. Quiesce each tool, preserve ownership and hidden entries, retain npm's expected _cacache layout, validate the new path, and only then remove the old copy. -->
  <!-- codex: “Read-mostly” is not a permission policy: shared CARGO_HOME contains mutable registry/git data, installed executables, configuration, and potentially credentials, as documented in [Cargo Home](https://doc.rust-lang.org/cargo/guide/cargo-home.html). Define trusted writers and protect shared executables and credentials; its remaining growth on /opt also needs accounting. -->
- journald `SystemMaxUse=300M`; apt `Keep-Downloaded-Packages "false"`.
  <!-- codex: Specify the complete apt configuration scope and verify both apt and apt-get behavior; the new setting does not itself remove existing archives. Include an explicit initial cleanup and post-change size check. -->
  <!-- codex: Configure and verify journal rotation/vacuum for existing usage rather than assuming an immediate exact 300M ceiling; only archived journal files are removed to enforce limits, per [journald.conf](https://manpages.ubuntu.com/manpages/jammy/man5/journald%40.conf.5.html). Preserve enough local or forwarded history to investigate the next incident. -->
- Result: `/` holds OS + `$HOME` config + the agent CLIs (~15-20 GB), leaving > 50% free.
  <!-- codex: Treat this as a measured acceptance target, not an established result: arbitrary HOME output, agent histories, /var/tmp, non-journal logs, crash dumps, and shared Rust growth remain possible. Assign retention or monitoring to remaining material paths before concluding root only needs journald/apt caps. -->

### P1 — next two weeks (bounded growth on `/workspace`)

**W3. Continuous docker caps** (disk-guard pre-ladder "caps" phase, every tick, busy-gated):
<!-- codex: Promote a minimal build-cache limit to P0 before increasing workspace demand, preferably using native [BuildKit garbage collection](https://docs.docker.com/build/cache/garbage-collection/) for the deployed builder driver. Keep bespoke image history and stack retention separate so their complexity does not delay this high-value reclaim. -->
<!-- codex: Bound caps-phase duration and let urgent filesystem recovery bypass slow maintenance; a five-minute timer does not imply five-minute enforcement when one run takes 45 minutes. Coordinate guard, daily timers, and manual cleanup with a common lock rather than merely checking whether each individual service is already running. -->
- build cache: `buildx prune --max-used-space` at the cap (20 GB) whenever above it — the daily
  timer stays as a backstop; under pressure the ladder keeps `-af`, but at most once per hour.
  <!-- codex: Verify installed Buildx support and noninteractive behavior, then enumerate the relevant local builders and Docker contexts explicitly; prune targets the selected builder, which can differ for root and agent users, per [buildx prune](https://docs.docker.com/reference/cli/docker/buildx/prune/). Define whether 20 GB is a host aggregate or a per-builder budget. -->
  <!-- codex: An hourly full cache purge can still create a rebuild/refill loop; first reclaim toward a free-space target with warm-cache protection and hysteresis. Define the exceptional condition for a full purge and ensure the daily backstop follows the same policy. -->
- images: remove images no container (running or stopped) uses and that were not used for 7 d —
  "used" = a container created from it, tracked by the guard in a small state file, since docker
  has no last-used field; first rollout in report mode.
  <!-- codex: Five-minute polling misses containers created and removed between ticks, and Docker event replay only retains the last 256 events, so polling events is not a durable usage history; see [Docker events](https://docs.docker.com/reference/cli/docker/system/events/). Prefer conservative last-observed retention with a renewed grace period after observation gaps unless maintaining a reliable event consumer is justified. -->
  <!-- codex: Container creation excludes base-image use during builds and recent pulls/loads of old images, while locally built unpublished images may contain unrecoverable work. Define those cases explicitly and provide owner-controlled retention protection rather than treating every unreferenced image as disposable. -->
  <!-- codex: Specify a root-owned, versioned state schema keyed by daemon identity and immutable image ID, with atomic writes, locking, bounded stale-record cleanup, and first-seen grace on rollout or state loss. Clock jumps, corrupt state, and ENOSPC must defer deletion rather than make unknown images immediately eligible. -->
  <!-- codex: Recheck references immediately before non-forced removal and treat a concurrent new reference as a skip, not a reason to force deletion. A seven-day age rule is not a byte cap, so expose when protected images alone exhaust the Docker budget. -->
- stale compose stacks: a stack whose containers have all been stopped ≥ 7 d (and whose
  working_dir worktree is idle ≥ 7 d) is `compose down` (containers + its named volumes are kept
  for another 7 d as orphan volumes, then removed). Report mode first. Breaks the stack↔worktree
  mutual pin.
  <!-- codex: The retention statement is incorrect: compose down removes containers and their writable layers immediately, while named volumes remain by default; see [compose down](https://docs.docker.com/reference/cli/docker/compose/down/). Container-layer data therefore has no proposed seven-day recovery period. -->
  <!-- codex: Stopped does not establish disposability, and volumes may hold databases or other live work; restrict automatic teardown and later volume deletion to explicitly managed disposable stacks. Define continuous stopped/idle evidence, protected projects, never-started or unknown states, and dirty/untracked worktree handling before breaking the pin. -->
  <!-- codex: A stack can restart between inspection and compose down, which would then stop active work. Require lifecycle coordination or remove only the previously validated stopped container IDs without force so a concurrent restart fails safely. -->
  <!-- codex: Mutable or missing Compose files, .env files, overrides, and project names can change what down targets or prevent cleanup entirely. Avoid privileged execution against arbitrary worktree configuration and use validated ownership and object identities when selecting resources. -->
  <!-- codex: The second seven-day clock must begin when a volume becomes orphaned, not at volume creation, and must survive reboot/state loss conservatively. The existing daily and pressure volume pruners must honor the same grace and ownership exclusions or they can immediately erase the promised retention period. -->
- Layout: manage `/etc/containerd/config.toml` (root/state) in ansible and converge dw1-dw4 onto ONE
  path (requires a docker stop + move per worker; scheduled per worker).
  <!-- codex: Codifying each worker's existing configuration removes unmanaged drift without moving data; path-name convergence provides little immediate capacity benefit. Defer the move to W7 if a separate disk remains likely, avoiding two maintenance migrations. -->
  <!-- codex: Distinguish Docker data-root, the containerd instance actually used by Docker, persistent containerd root, and runtime state under /run; their storage locations are not interchangeable, as explained in [Docker daemon configuration](https://docs.docker.com/engine/daemon/). Preserve existing runtime/plugin settings and require the data filesystem before either daemon starts. -->
  <!-- codex: Stopping Docker alone may leave containerd, socket activation, or live-restore tasks using the old tree. Specify complete writer shutdown, metadata-preserving transfer or same-filesystem rename, container/volume smoke tests, and rollback before removing the old data. -->
<!-- codex: Images and build cache do not cover container writable layers, container logs, or growing active volumes. Add size attribution and appropriate native log rotation for those paths so “Docker caps” does not imply coverage the implementation lacks. -->

**W4. Cache caps** (same caps phase): `/workspace/<user>/.cache` total ≤ 15 GB per user: over the
cap, prune per tool with its native safe command first (`uv cache prune`, `npm cache verify`,
playwright `uninstall` of unreferenced revisions), then delete whole tool subdirs least-recently
modified first (caches are regenerable by definition), never one modified within 1 h.
<!-- codex: Justify 15 GB against a host budget: 15 GB times the number of users, plus Docker, worktrees, shared targets, relocated temporary data, and burst/recovery reserve must fit 125 GB. Choose per-tool targets from observed working sets and acceptable rebuild cost rather than assigning every user the same unsupported allowance. -->
<!-- codex: The cap only covers .cache, leaving existing .cache-npm and other configured destinations outside it unless W2 explicitly converges them. Derive the accounting set from effective tool paths and avoid counting the same bind or symlink target twice. -->
<!-- codex: Native maintenance does not necessarily enforce a size target: uv prune removes unused entries, while npm verify checks integrity and garbage-collects unneeded data rather than providing an LRU byte limit; see [uv cache management](https://docs.astral.sh/uv/concepts/cache/) and [npm cache](https://docs.npmjs.com/cli/v11/commands/npm-cache/). Define the measured result and safe next action when these commands reclaim too little. -->
<!-- codex: Playwright uninstall removes browsers for the current installation; it is not a command restricted to unreferenced revisions, whose garbage collection is a separate mechanism in [Playwright browser management](https://playwright.dev/docs/browsers#stale-browser-removal). Prove candidate revisions are unneeded before removing binaries that active tests or later launches require. -->
<!-- codex: Directory mtime is neither recursive modification time nor last use, and reads of old cached data do not reliably refresh it. Remove the generic whole-directory fallback for live caches, use tool-supported locking/cleanup, and respect uv's explicit warning against direct cache modification in its [cache safety documentation](https://docs.astral.sh/uv/concepts/cache/#cache-safety). -->
<!-- codex: A one-hour protection window means the cap can remain exceeded; expose protected bytes and a no-safe-candidates state instead of escalating to unsafe deletion. Add separate trigger/target thresholds and cooldowns to prevent repeated prune-and-redownload cycles. -->
<!-- codex: With uv hardlinks, deleting a cache entry may free no blocks while a venv still references it, and separate directory totals can double-count shared storage. Control recovery using actual filesystem availability and inode deltas rather than assuming reported cache bytes equal reclaimable bytes. -->
Shared Rust targets (`/workspace/<user>/.target`-style `CARGO_TARGET_DIR`s) get the same treatment
as deps: removable when untouched 3 d.
<!-- codex: A shared target can serve multiple worktrees and running executables, so one directory timestamp or one idle worktree cannot establish safety. Coordinate cleanup with all participating builds and target users, retaining it when ownership or activity is uncertain. -->

**W5. Attribution for agent data** (hourly, separate timer — not every 5 min):
<!-- codex: Promote a minimal size report plus an operator response to P0 because the largest measured unmanaged directories are not addressed by cache relocation or Docker pruning. The full per-directory Prometheus design can follow after a simple report proves useful. -->
- `du -x --max-depth=1` of `/workspace/<user>` and `$HOME` (ionice idle, 10 min budget) → textfile
  metric `dev_worker_dir_bytes{dir,git="true|false"}`, top 25 per user + an "other" bucket.
  <!-- codex: max-depth limits output, not recursive scan work; define timeout, permission-error, and concurrent-rename handling and publish completion time plus scan success. Preserve last-good data with visible staleness instead of publishing partial totals as a successful sample, and measure scan cost under representative builds. -->
  <!-- codex: Top-25 membership churn destroys continuous history for newly appearing or temporarily omitted directories, while “other” cannot identify the offender. Compute growth before truncating presentation, or retain stable accounting history, and bound cumulative label churn rather than only the current series count. -->
  <!-- codex: Recognize worktrees whose .git is a file and distinguish grouping directories such as wt/ from actual worktrees; git=true does not mean the bytes are tracked or owned. Treat this classification as an attribution hint, never as proof that a directory is safe or unsafe to delete. -->
  <!-- codex: Escape arbitrary filenames correctly in Prometheus labels and constrain label length; raw paths can also disclose private project names to monitoring users. Write the metric file atomically into a trusted directory and keep sensitive detail in an appropriately restricted report if necessary. -->
- `DevWorkerDirGrowing`: a single dir grew > 15 GB in 24 h; `DevWorkerUnownedDataLarge`: a non-git,
  non-archive, non-cache dir > 20 GB. Both name the dir in the alert — the forge archives (+50 GB in
  2-3 days) would have paged on day one.
  <!-- codex: These thresholds miss aggregate growth spread across smaller directories and slow growth that consumes the remaining headroom; add per-user/host totals and relate escalation to available space. “Day one” also depends on having a baseline and a successful scan, so test new-directory and missing-history behavior explicitly. -->
- Agent guide (managed block): name the directories an agent may use for scratch (archive, cache,
  tmp) and say that everything else in `/workspace/<user>` must be a git worktree or be deleted
  when the task ends; non-git dirs over 20 GB are reported to the operator.
  <!-- codex: Allow declared owners and lifetimes for legitimate non-git datasets instead of encouraging agents to hide them inside a repository. Define scratch retention and active-use protection too: a permitted directory name does not make its current contents disposable. -->

**W6. Smaller reclaim gaps in `cleanup`**: idle-worktree build outputs (`dist`, `.next`, `coverage`,
`test-results`, `.turbo`, `__pycache__`) join the deps step; `~/.codex/sessions` + Claude transcripts
older than 30 d (Claude: managed `cleanupPeriodDays: 30`); daily `git worktree prune` on main clones.
<!-- codex: Directory names are not proof of generated content: dist or test-results can contain tracked deliverables or unreproducible debugging evidence. Use explicit generated-output policies plus current activity checks, and do not delete tracked files or follow paths outside the validated worktree. -->
<!-- codex: Sessions and transcripts are retained user data, not ordinary caches; specify which timestamp defines age, protect active sessions, and document the loss of resume/history capability. Prefer the application's managed retention where available and verify the intended storage scope before adding a generic sweep. -->
<!-- codex: git worktree prune removes administrative records for missing worktrees rather than worktree contents, so it is hygiene with little expected disk recovery; preserve locked/offline worktrees as described in [git-worktree](https://git-scm.com/docs/git-worktree.html). Keep it from delaying higher-yield reclaim work. -->

### P2 — structural (decide after P0/P1 data)

**W7. Docker on its own disk.** scsi2 (~100 GB) for the docker/containerd root, so a docker fill
cannot block git/edits in worktrees and vice versa; `/workspace` stays for worktrees. Needs the
Proxmox local-storage headroom check on ai-node1/2 first (dev-workers live there) and a per-worker
maintenance window; tofu + ansible change. Decide with 2 weeks of W5 data: if docker is < 25% of
`/workspace` peaks, skip.
<!-- codex: W5 scans user directories, so it cannot establish Docker's share of whole-filesystem peaks or account for .tmp and other top-level service paths. Collect explicit Docker/containerd and filesystem measurements, and base isolation on burst contribution and operational impact rather than an arbitrary 25% occupancy cutoff. -->
<!-- codex: A second filesystem separates guest capacity limits but does not isolate exhaustion of the same Proxmox backing pool. Include thin-pool/snapshot headroom, host alerts, and migration overlap in the capacity check, and size 100 GB against protected live data plus reserve. -->

**W8. Guard robustness**: bound the docker busy deferral (after 60 min of deferral with the fs under
10%, run anyway); heartbeat thread during long steps; busy gate for the daily buildx/volume prune;
codex in-use check also looks at cwd/maps.
<!-- codex: Promote timely heartbeat, bounded step execution, shared cleanup coordination, and required in-use protections to P0; expanding deletion while leaving these known weaknesses until P2 reverses the dependency order. A heartbeat thread must not hide a hung step, so alert separately on last progress and last completed run. -->
<!-- codex: Elapsed deferral does not make deletion safe: do not override image, stack, or volume protection merely because an hour passed. At critical space, use operations that preserve in-use objects, escalate, and pause new disk-heavy work when safe reclaim is unavailable. -->
<!-- codex: An argv heuristic can miss Buildx, Compose, API clients, detached work, and rootless daemons while matching unrelated command text. Scope busy detection to the affected local daemon and operation, and avoid letting one build globally block unrelated safe reclaim. -->
<!-- codex: cwd/maps checks improve evidence but are not a complete activity contract; include relevant executable/open-file references and handle processes exiting or becoming inaccessible during inspection. Unknown activity should preserve candidates rather than be interpreted as idle. -->

**W9. Agents see the disk**: free space of `/` and `/workspace` in the Claude statusline
(`claude-statusline.js`) and the tmux status bar, red under 15%.

### Not doing
- Hard quotas (ext4 project quotas): invasive (fs feature flags, remount); revisit only if W5
  attribution + guide do not change behaviour within a month.
  <!-- codex: Deferring quotas is reasonable only if the plan acknowledges that soft reclamation and guidance cannot guarantee free space. Set an earlier revisit trigger for repeat exhaustion or sustained unreclaimable growth instead of waiting a month regardless of incidents. -->
- Growing disks alone: peak fills of 36 GB in 6 h would refill any reasonable size.
  <!-- codex: A measured peak burst does not establish indefinitely sustained growth; reject expansion as the sole fix, but retain targeted temporary headroom as a migration and response aid. Modest root expansion may be lower risk than rushing a system-wide /tmp change. -->
- Auto-deleting non-git agent data outside the archive/cache/tmp dirs: it is live work.

## Critical files

- `ansible/roles/dev_worker/files/disk-guard` — heartbeat carry-forward (W1), caps phase (W3/W4),
  deferral bound + heartbeat thread (W8).
- `ansible/roles/dev_worker/files/cleanup` — stale stacks, image last-use, build outputs, sessions
  (W3/W6).
  <!-- codex: Establish one deletion-safety contract across this expanding privileged tool: trusted roots, race-resistant no-follow traversal, ownership checks, and no crossing into unexpected mounts. Run native per-user maintenance with the owner's identity and trusted executable/environment rather than root executing commands resolved from agent-controlled paths. -->
- `ansible/roles/dev_worker/tasks/{disk_guard,docker,cleanup,tmp_hygiene}.yml`, new
  `tasks/disk_layout.yml` (tmp bind mount, cache env, journald/apt caps, containerd config).
- `ansible/roles/dev_worker/templates/agent-shell-env.sh.j2` (+ systemd user env) — cache env (W2).
- `ansible/roles/dev_worker/defaults/main.yml` — caps and toggles, each feature with a
  report/enforce mode.
  <!-- codex: Report mode must use the same candidate selection without invoking mutating “maintenance” commands, and report potential rather than guaranteed reclaimed bytes. Add independent disable switches so an unsafe cache or stack policy can be stopped while alerting and unrelated reclaim continue. -->
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
  <!-- codex: Couple the Python heartbeat regression to the Prometheus sample sequence so tests cannot validate an idealized exporter that the implementation does not produce. Include recovery without a ladder, simultaneous filesystem pressure, missing scrapes, metric write failure, and interrupted runs. -->
  <!-- codex: Add adversarial deletion tests for symlink/rename races, newly active containers, active cache readers/writers, unknown ownership, corrupt state, reboot, and clock changes. Test complete seven-day image and seven-plus-seven-day stack/volume lifecycles with controlled time, including every competing prune entrypoint. -->
- Canary per workstream on one worker (dw3 for `/workspace`, dw2 for `/`), report mode first where a
  feature deletes, 48 h of reports reviewed before enforce, then the rest.
  <!-- codex: Forty-eight hours of report mode cannot validate a seven- or fourteen-day policy, real reclaimed bytes, or command side effects. Exercise aged disposable fixtures and a limited enforcement canary with representative builds before fleet rollout, with explicit stop criteria for job failures or poor reclaim yield. -->
  <!-- codex: Add Ansible syntax/check and idempotence checks, installed-tool capability checks, and verification of handler/reboot ordering across role tags. Reapplying the role after a partially completed migration must converge safely without deleting either valid copy. -->
- W2 acceptance: after a reboot `/tmp` is on `/workspace` (`findmnt /tmp`), agent shells report the
  cache env (`env | grep CACHE`), new venvs hardlink (`uv` reports linked), `/` used < 50%.
  <!-- codex: On a disposable clone, test delayed/missing/read-only/full workspace, missing .tmp, reboot, and rollback, then prove login, PrivateTmp services, sockets, and guard recovery still function. Inspect effective mounts inside relevant namespaces and verify that hidden old /tmp data no longer occupies root. -->
  <!-- codex: Environment grep is insufficient and misses variables such as lowercase npm_config_cache and PLAYWRIGHT_BROWSERS_PATH; run actual tools through each supported entrypoint and inspect their effective cache paths and created files. Verify hardlink device/inode identity and root growth after representative work, including refreshed existing tmux and daemon sessions. -->
- Fleet acceptance (14 days after P0+P1): no filesystem under 5% free; every sub-15% episode was
  preceded by a TimeToFull or DirGrowing alert; guard exhausted time < 2 h/week per worker.
  <!-- codex: The “every episode preceded” criterion is impossible for abrupt allocations or missing historical data; distinguish measured forecast lead time from a bounded detection time using independent low-space and telemetry-failure alerts. Define exhaustion consistently with carried-forward results so missing or failed runs cannot improve the score. -->
  <!-- codex: Require representative workload volume and track build failures, rebuild/download cost, cleanup duration, actual reclaimed bytes/inodes, and false pages alongside free space. Fourteen days may not include the full orphan-volume lifecycle after staged rollout, so complete that verification separately rather than declaring success early. -->

<!-- codex-review-status: complete -->