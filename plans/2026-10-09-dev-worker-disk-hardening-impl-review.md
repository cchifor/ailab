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
<!-- codex: round-2: The settled decision permits reclaim through a busy Docker below --critical; it does not amend W2's separate hourly full-prune limit. Critical pressure does not guarantee that cache stays empty: builds can produce new reclaimable cache between ticks while unrelated disk usage keeps free space below 5%, allowing repeated full prunes to discard that cache. The claimed absence of a refill loop is therefore not enforced. Native BuildKit GC runs periodically, so a guard tick can precede its next sweep; it is not continuous enforcement or a synchronous replacement for the planned targeted prune ([Docker GC documentation](https://docs.docker.com/build/cache/garbage-collection/)). Keep the hourly full-prune limit and use the configured target between full prunes, without changing the settled busy-Docker exception. Test repeated successful critical-pressure ticks with intervening cache growth. -->
<!-- opus-pushback: Intended, and tied to the escalated point above: below --critical the guard's job is to free space now, and once the first full prune has emptied the cache a repeat frees little and costs nothing extra (no refill loop at that fill level). Between full prunes the dangling-only prune is deliberate: W3's BuildKit GC (merged #1189) now enforces the 20 GB max-used-space continuously inside dockerd, which is the size-targeted prune the plan asked for. -->

### Cache migration deletes without the planned safety workflow
**Location:** PR #1192 / #1195 — agent-dirs-migrate and tasks/agent_dirs.yml
**Severity:** important
<!-- codex: round-2: Accepting wholesale symlinks and disposable cache contents does not resolve the concurrent-use race. ln -sT fixes link placement only when something recreates the source directory after its rename. A tool can instead start after the busy survey but before mv, open the existing cache, and remain active while mv, ln -sT, and rm -rf all succeed. That run reports "moved", not a busy warning, while removing an active uv environment or Playwright installation. Both the success and link-failure branches still delete the renamed tree. Repopulating caches on a later invocation cannot protect the current process, and the reviewed placement deviation does not establish quiescence. Prevent new cache users throughout the survey and switch, or use a migration that preserves active users' access; add a deterministic regression test starting a cache user after the initial busy check. -->
<!-- opus-pushback: Partly accepted (358d9c88: `ln -sT` closes the rename-to-link race — a tool recreating ~/.cache in that window no longer gets the link put inside it, the run reports and the next one retries). The rest is a documented, reviewed deviation (#1192/#1195, both bots approved): symlinking ~/.cache wholesale covers every entrypoint, which tool-native per-cache cleaning cannot; caches repopulate by design; the per-run busy skip with a visible per-user warning is the report step, and dw3/dw4 were indeed deferred by it. -->

### Agent placement does not cover the promised entrypoints and stores
**Location:** PR #1192 — agent_dirs.yml, profile.d-02-dev-worker-agent-tmp.sh.j2, and claude-job@.service.j2
**Severity:** important
<!-- opus-pushback: The agent entrypoints are covered: tmux/ttyd panes (interactive, via ~/.bashrc), login shells and codex's `bash -lc` (profile.d), Claude's Bash tool (inherits the pane's env), claude-job (unit Environment). Non-interactive `ssh host cmd` is not an agent entrypoint here, and no agent runs as a systemd user service. The ~/.cache symlink covers every tool's default cache location (uv, pip, npm, Playwright, go-build) without per-tool variables; GOMODCACHE and pnpm stores were not observed consumers in the survey — W5's attribution will name them (e.g. ~/.local) if they grow. -->

### The agent-directory toggle does not disable installed cleanup
**Location:** PR #1192 — tasks/main.yml and tasks/agent_dirs.yml
**Severity:** important
Resolved in 358d9c88: with the toggle false, the profile.d hook, its .bashrc line and the tmpfiles aging rule are removed; ~/.cache and ~/.npm symlinks are documented to stay (they point at real cache data).

### Deferral lifetime does not represent blocked Docker reclaim
**Location:** PR #1188 / #1190 — next_state, guard deferral tracking, and DevWorkerDiskGuardDeferred
**Severity:** important
<!-- opus-pushback: Clearing deferred_since on a completed run that did not defer is correct: such a run either had no pressure (docker reclaim was not needed) or ran its docker steps. The guard only defers while the docker filesystem is short, so the exported deferred_since already implies current docker-fs pressure; an alert for a blocked reclaimer across pressure/recovery cycles would page on situations that resolved themselves. -->

### W2’s timeout and progress work remains incomplete
**Location:** PR #1190 — disk-guard ladder, run_with_heartbeat, and ReliabilityTest
**Severity:** important
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
<!-- opus-pushback: The bridge is tested from both sides: the Python tests assert the exact series each heartbeat and completion exports (carried exhausted/failed_steps, state_known, state_write_failed across runs), and the promtool fixtures replay those series shapes, including a ladder longer than one scrape, recovery, and a gappy series. Generating fixtures from a running exporter is test infrastructure worth a follow-up, not a defect in this change. -->

### Runbook presents desired fleet state as completed rollout
**Location:** docs/runbooks/dev-workers.md:19
**Severity:** nit
<!-- opus-pushback: The runbook documents the configured state; transient per-host rollout state (dw1's root disk pending the balloon-memory decision, dw3/dw4 cache moves waiting for idle) is tracked here and in the PRs, and would be stale in the runbook within a day. -->
