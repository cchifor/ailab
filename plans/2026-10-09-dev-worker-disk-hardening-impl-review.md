# Implementation review — dev-worker-disk-hardening — round 1

<!-- codex-impl-review-status: pending -->

## Summary

- W1’s persisted results, W3’s native GC configuration, and W5’s attribution are substantial improvements. W2 and W4 still diverge materially from the finalized plan.
- The highest-risk gap is the guard’s retained permission to prune through busy Docker work below 5% free. W4 also substitutes immediate generic cache deletion for the planned report-first, tool-native migration.
- Rollout remains partial: W4 cache moves completed on dw1/dw2 and correctly deferred on busy dw3/dw4; W6 reached dw2–dw4, with dw1 deferred for unrelated balloon-memory drift.
- W7–W9 are not implemented in these PRs; W10–W12 remain later or conditional work. Those are outstanding plan items, consistent with reviewing P0 first.
- This review uses the supplied diffs and rollout facts. Local commands failed at sandbox startup, so tests could not be rerun; rollout alone does not establish the planned behavioral and observation-window acceptance checks.

## Findings

### Critical pressure still overrides Docker protection
**Location:** ansible/roles/dev_worker/files/disk-guard:331
**Severity:** blocker
<!-- codex: The busy gate still falls through when the Docker filesystem is below --critical, allowing prune operations during the builds/pulls whose leases those operations can invalidate; W2 explicitly required continued protection and escalation at critical space. Remove the override, update the alert/runbook language promising pruning below 5%, and test that every Docker reclaim step remains deferred under critical pressure while Docker is busy. -->

### Full-cache pruning still bypasses its hourly limit
**Location:** PR #1189 / #1190 — BuildKit GC policy and disk-guard full_prune_due/ladder
**Severity:** important
<!-- codex: full_prune_due returns true unconditionally below --critical, so sustained critical pressure can discard all warm cache every tick despite W2’s hourly limit; the interim dangling-only prune is also a documented substitution for, rather than an implementation of, the planned size-targeted prune. Apply the hourly limit at every pressure level, use the configured --max-used-space target between full prunes, and test repeated successful ticks below 5% free. -->

### Cache migration deletes without the planned safety workflow
**Location:** PR #1192 / #1195 — agent-dirs-migrate and tasks/agent_dirs.yml
**Severity:** important
<!-- codex: The migration immediately renames and recursively deletes whole ~/.cache and ~/.npm trees, with no report mode or tool-native cleaning, and its process/container surveys do not prevent a new cache writer from starting before the rename/removal. Restore the planned report-first cleaning workflow and coordinate a quiescent migration window that prevents new writers during the switch, with a regression test for a tool starting after the initial busy check. -->

### Agent placement does not cover the promised entrypoints and stores
**Location:** PR #1192 — agent_dirs.yml, profile.d-02-dev-worker-agent-tmp.sh.j2, and claude-job@.service.j2
**Severity:** important
<!-- codex: TMPDIR is wired through profile.d, an appended .bashrc line, and one job unit, leaving non-interactive SSH shells that return early from .bashrc and other systemd user services uncovered; symlinking ~/.cache and ~/.npm also supplies none of the promised explicit per-tool cache settings, including GOMODCACHE and the pnpm store. Complete the launcher, SSH, tmux, and user-service environment wiring and verify actual tool-created files and uv hardlinks through each entrypoint, rather than only sourcing the hook in a test shell. -->

### The agent-directory toggle does not disable installed cleanup
**Location:** PR #1192 — tasks/main.yml and tasks/agent_dirs.yml
**Severity:** important
<!-- codex: Setting dev_worker_agent_dirs_on_workspace=false merely skips the task import, leaving the installed profile hook, .bashrc source line, and eight-hour tmpfiles deletion policy active after rollout. Implement an explicit off path that disables the managed hooks and aging policy, document the treatment of existing cache symlinks, and test an enabled-to-disabled transition. -->

### Deferral lifetime does not represent blocked Docker reclaim
**Location:** PR #1188 / #1190 — next_state, guard deferral tracking, and DevWorkerDiskGuardDeferred
**Severity:** important
<!-- codex: next_state clears deferred_since whenever a run has no deferral, including healthy runs that execute no Docker step, so repeated pressure/recovery cycles can prevent a continuously blocked Docker reclaimer from ever reaching the one-hour warning; conversely, the alert has no current Docker-filesystem pressure join. Track Docker execution separately, preserve the timestamp until a Docker reclaim step actually runs, and attach/join the filesystem identity so tests cover intervening healthy runs and unrelated cleanup-lock deferrals. -->

### W2’s timeout and progress work remains incomplete
**Location:** PR #1190 — disk-guard ladder, run_with_heartbeat, and ReliabilityTest
**Severity:** important
<!-- codex: The heartbeat thread was added, but the PR still explicitly permits 45-minute steps, does not implement the promised shorter step budgets, and never exports the last_progress timestamp deferred from W1 to W2. Complete those items and test a real hung/timed-out subprocess and its termination while verifying that completed-result state remains stable; the current sleep-based heartbeat tests do not exercise that recovery path. -->

### Grouping directories escape the unowned-data alert
**Location:** PR #1193 — disk-report kind_of/render and DevWorkerUnownedDataLarge
**Severity:** important
<!-- codex: Any top-level directory containing a Git child becomes kind=worktrees, which both the large-entry export exception and UnownedDataLarge exclude, so a parent containing substantial non-git data beside one checkout can remain permanently silent once its growth stops. Preserve worktrees as a classification hint while covering non-git grouping directories under the size policy, and test a parent over 20 GB that falls outside the top ten. -->

### Missing report roots are counted as a complete scan
**Location:** PR #1193 — disk-report scan_all/build and report freshness alerts
**Severity:** important
<!-- codex: scan_all silently omits configured roots for which isdir is false, and build considers the remaining roots—even an empty scan—complete, so an unavailable workspace can disappear from attribution while last_complete keeps advancing and suppresses Stale/Missing. Treat unavailable configured roots as failed scans, verify the workspace mount, and retain their last-good values until those roots can be scanned again. -->

### Growth-history write failures can leave monitoring apparently healthy
**Location:** PR #1193 — disk-report write_atomic/save/main and report freshness alerts
**Severity:** important
<!-- codex: write_atomic logs persistence failures without returning failure to the caller, allowing fresh complete-scan metrics to be published even when growth snapshots are not being saved; if history writes keep failing while textfile writes succeed, DirGrowing loses its baseline without Stale or Missing detecting the loss. Propagate persistence status and expose an alerted history-write failure or growth-readiness signal, with a test that keeps metrics writable while snapshot writes fail. -->

### Docker-only deployment now has an unmet installation dependency
**Location:** PR #1190 — tasks/docker.yml and tasks/disk_guard.yml
**Severity:** important
<!-- codex: The daily Docker unit now requires disk-guard’s new --run-when-docker-idle mode, but that executable is installed through the separate disk_guard task set, so a docker-tag deployment onto a missing or older installation can install a timer that subsequently fails. Make the required executable/version part of the Docker deployment dependency and test tag-limited convergence from both absent and older guard installations. -->

### Alert tests do not exercise the combined exporter timeline
**Location:** PR #1188 / #1190 / #1193 — Python tests and dev-workers-rules.test.yaml
**Severity:** important
<!-- codex: The Python tests exercise helpers separately and the promtool cases supply handwritten series, leaving the planned bridge from actual heartbeat/state transitions to alert evaluation untested, including recovery without a ladder, simultaneous filesystem pressure, and scrape gaps. Generate fixtures from the exporter’s real transition sequence and verify every disk rule, including DiskFilling and GuardFailing, through long steps, interruption, recovery, and persistent deferral. -->

### Runbook presents desired fleet state as completed rollout
**Location:** docs/runbooks/dev-workers.md:19
**Severity:** nit
<!-- codex: The sizing table reports dw1 as having a 60 GiB root disk, and the cache section describes relocation as fleet-wide, whereas the supplied rollout facts leave dw1’s resize and dw3/dw4’s cache moves pending. Record those host-specific exceptions and their completion checks so operators can distinguish current capacity and cache placement from the intended configuration. -->