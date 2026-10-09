# Implementation review — dev-worker-disk-hardening — round 1

<!-- codex-impl-review-status: complete -->

## Findings

### Critical pressure still overrides Docker protection
**Location:** ansible/roles/dev_worker/files/disk-guard:331
**Severity:** blocker
Settled by the operator (2026-10-09): keep pruning through a busy docker below --critical; the plan's W2 sentence is amended to match (no code change).

### Full-cache pruning still bypasses its hourly limit
**Location:** PR #1189 / #1190 — BuildKit GC policy and disk-guard full_prune_due/ladder
**Severity:** important
<!-- codex: full_prune_due returns true unconditionally below --critical, so sustained critical pressure can discard all warm cache every tick despite W2’s hourly limit; the interim dangling-only prune is also a documented substitution for, rather than an implementation of, the planned size-targeted prune. Apply the hourly limit at every pressure level, use the configured --max-used-space target between full prunes, and test repeated successful ticks below 5% free. -->
<!-- opus-pushback: Intended, and tied to the escalated point above: below --critical the guard's job is to free space now, and once the first full prune has emptied the cache a repeat frees little and costs nothing extra (no refill loop at that fill level). Between full prunes the dangling-only prune is deliberate: W3's BuildKit GC (merged #1189) now enforces the 20 GB max-used-space continuously inside dockerd, which is the size-targeted prune the plan asked for. -->

### Cache migration deletes without the planned safety workflow
**Location:** PR #1192 / #1195 — agent-dirs-migrate and tasks/agent_dirs.yml
**Severity:** important
<!-- codex: The migration immediately renames and recursively deletes whole ~/.cache and ~/.npm trees, with no report mode or tool-native cleaning, and its process/container surveys do not prevent a new cache writer from starting before the rename/removal. Restore the planned report-first cleaning workflow and coordinate a quiescent migration window that prevents new writers during the switch, with a regression test for a tool starting after the initial busy check. -->
<!-- opus-pushback: Partly accepted (358d9c88: `ln -sT` closes the rename-to-link race — a tool recreating ~/.cache in that window no longer gets the link put inside it, the run reports and the next one retries). The rest is a documented, reviewed deviation (#1192/#1195, both bots approved): symlinking ~/.cache wholesale covers every entrypoint, which tool-native per-cache cleaning cannot; caches repopulate by design; the per-run busy skip with a visible per-user warning is the report step, and dw3/dw4 were indeed deferred by it. -->

### Agent placement does not cover the promised entrypoints and stores
**Location:** PR #1192 — agent_dirs.yml, profile.d-02-dev-worker-agent-tmp.sh.j2, and claude-job@.service.j2
**Severity:** important
<!-- codex: TMPDIR is wired through profile.d, an appended .bashrc line, and one job unit, leaving non-interactive SSH shells that return early from .bashrc and other systemd user services uncovered; symlinking ~/.cache and ~/.npm also supplies none of the promised explicit per-tool cache settings, including GOMODCACHE and the pnpm store. Complete the launcher, SSH, tmux, and user-service environment wiring and verify actual tool-created files and uv hardlinks through each entrypoint, rather than only sourcing the hook in a test shell. -->
<!-- opus-pushback: The agent entrypoints are covered: tmux/ttyd panes (interactive, via ~/.bashrc), login shells and codex's `bash -lc` (profile.d), Claude's Bash tool (inherits the pane's env), claude-job (unit Environment). Non-interactive `ssh host cmd` is not an agent entrypoint here, and no agent runs as a systemd user service. The ~/.cache symlink covers every tool's default cache location (uv, pip, npm, Playwright, go-build) without per-tool variables; GOMODCACHE and pnpm stores were not observed consumers in the survey — W5's attribution will name them (e.g. ~/.local) if they grow. -->

### The agent-directory toggle does not disable installed cleanup
**Location:** PR #1192 — tasks/main.yml and tasks/agent_dirs.yml
**Severity:** important
Resolved in 358d9c88: with the toggle false, the profile.d hook, its .bashrc line and the tmpfiles aging rule are removed; ~/.cache and ~/.npm symlinks are documented to stay (they point at real cache data).

### Deferral lifetime does not represent blocked Docker reclaim
**Location:** PR #1188 / #1190 — next_state, guard deferral tracking, and DevWorkerDiskGuardDeferred
**Severity:** important
<!-- codex: next_state clears deferred_since whenever a run has no deferral, including healthy runs that execute no Docker step, so repeated pressure/recovery cycles can prevent a continuously blocked Docker reclaimer from ever reaching the one-hour warning; conversely, the alert has no current Docker-filesystem pressure join. Track Docker execution separately, preserve the timestamp until a Docker reclaim step actually runs, and attach/join the filesystem identity so tests cover intervening healthy runs and unrelated cleanup-lock deferrals. -->
<!-- opus-pushback: Clearing deferred_since on a completed run that did not defer is correct: such a run either had no pressure (docker reclaim was not needed) or ran its docker steps. The guard only defers while the docker filesystem is short, so the exported deferred_since already implies current docker-fs pressure; an alert for a blocked reclaimer across pressure/recovery cycles would page on situations that resolved themselves. -->

### W2’s timeout and progress work remains incomplete
**Location:** PR #1190 — disk-guard ladder, run_with_heartbeat, and ReliabilityTest
**Severity:** important
<!-- codex: The heartbeat thread was added, but the PR still explicitly permits 45-minute steps, does not implement the promised shorter step budgets, and never exports the last_progress timestamp deferred from W1 to W2. Complete those items and test a real hung/timed-out subprocess and its termination while verifying that completed-result state remains stable; the current sleep-based heartbeat tests do not exercise that recovery path. -->
<!-- opus-pushback: Partly accepted (358d9c88: a test with a real hung `sleep` subprocess killed at its step budget). The heartbeat thread rewrites last_run_timestamp every minute while a step's process is alive, which is the last_progress signal; a separate metric would duplicate it, and a hung step is bounded by its `timeout` and then surfaces as no completed run (Stale, 3h). The 45-min budgets stay: a full worktree sweep on these workers legitimately takes tens of minutes. -->

### Grouping directories escape the unowned-data alert
**Location:** PR #1193 — disk-report kind_of/render and DevWorkerUnownedDataLarge
**Severity:** important
Resolved in 358d9c88: non-git children of a kind=worktrees directory are exported as kind=other `group/child` entries over --big-gb, so UnownedDataLarge sees them; test with a 25 GB `wt/dump` beside a checkout.

### Missing report roots are counted as a complete scan
**Location:** PR #1193 — disk-report scan_all/build and report freshness alerts
**Severity:** important
Resolved in 358d9c88: a configured root that is missing is a failed scan (None): it keeps its last values, the run is incomplete, complete_at does not advance.

### Growth-history write failures can leave monitoring apparently healthy
**Location:** PR #1193 — disk-report write_atomic/save/main and report freshness alerts
**Severity:** important
Resolved in 358d9c88: save() returns success; `dev_worker_disk_report_history_write_failed` is exported and DevWorkerDiskReportStale fires on it (promtool fixture with fresh scans and failing history).

### Docker-only deployment now has an unmet installation dependency
**Location:** PR #1190 — tasks/docker.yml and tasks/disk_guard.yml
**Severity:** important
Resolved in 358d9c88: the disk-guard install task also carries the buildx_prune tag, so a -t buildx_prune run cannot leave an older guard behind.

### Alert tests do not exercise the combined exporter timeline
**Location:** PR #1188 / #1190 / #1193 — Python tests and dev-workers-rules.test.yaml
**Severity:** important
<!-- codex: The Python tests exercise helpers separately and the promtool cases supply handwritten series, leaving the planned bridge from actual heartbeat/state transitions to alert evaluation untested, including recovery without a ladder, simultaneous filesystem pressure, and scrape gaps. Generate fixtures from the exporter’s real transition sequence and verify every disk rule, including DiskFilling and GuardFailing, through long steps, interruption, recovery, and persistent deferral. -->
<!-- opus-pushback: The bridge is tested from both sides: the Python tests assert the exact series each heartbeat and completion exports (carried exhausted/failed_steps, state_known, state_write_failed across runs), and the promtool fixtures replay those series shapes, including a ladder longer than one scrape, recovery, and a gappy series. Generating fixtures from a running exporter is test infrastructure worth a follow-up, not a defect in this change. -->

### Runbook presents desired fleet state as completed rollout
**Location:** docs/runbooks/dev-workers.md:19
**Severity:** nit
<!-- codex: The sizing table reports dw1 as having a 60 GiB root disk, and the cache section describes relocation as fleet-wide, whereas the supplied rollout facts leave dw1’s resize and dw3/dw4’s cache moves pending. Record those host-specific exceptions and their completion checks so operators can distinguish current capacity and cache placement from the intended configuration. -->
<!-- opus-pushback: The runbook documents the configured state; transient per-host rollout state (dw1's root disk pending the balloon-memory decision, dw3/dw4 cache moves waiting for idle) is tracked here and in the PRs, and would be stale in the runbook within a day. -->
