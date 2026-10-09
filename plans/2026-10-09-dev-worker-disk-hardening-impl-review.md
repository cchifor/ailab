# Implementation review — dev-worker-disk-hardening — rounds 1-2

<!-- codex-impl-review-status: finalized -->

## Findings

### Critical pressure still overrides Docker protection
**Location:** ansible/roles/dev_worker/files/disk-guard:331
**Severity:** blocker
Settled by the operator (2026-10-09): keep pruning through a busy docker below --critical; the plan's W2 sentence is amended to match (no code change).

### Full-cache pruning still bypasses its hourly limit
**Location:** PR #1189 / #1190 — BuildKit GC policy and disk-guard full_prune_due/ladder
**Severity:** important
Resolved in 46651cff (round 2 accepted): the all-cache `-af` runs at most hourly under --critical too; the ticks in between prune to the BuildKit GC target (`--max-used-space`/`--min-free-space`/`--reserved-space` from `dev_worker_buildkit_gc_*`), as W2 specified — at --critical that leaves only the reserved, most recently used cache. Test: repeated critical ticks within the hour, then due again.

### Cache migration deletes without the planned safety workflow
**Location:** PR #1192 / #1195 — agent-dirs-migrate and tasks/agent_dirs.yml
**Severity:** important
Resolved in 46651cff (round 2 accepted): the busy check runs again right before each switch, and the renamed tree is removed only once no process references it ("kept …"; a later run reports "removed …"). Deterministic tests start a cache user during the copy (stub `cp`) and between rename and link (stub `ln`); each fails with its half of the fix removed.

### Agent placement does not cover the promised entrypoints and stores
**Location:** PR #1192 — agent_dirs.yml, profile.d-02-dev-worker-agent-tmp.sh.j2, and claude-job@.service.j2
**Severity:** important
Dropped by codex in round 2 (the implementation's reasoning was accepted).

### The agent-directory toggle does not disable installed cleanup
**Location:** PR #1192 — tasks/main.yml and tasks/agent_dirs.yml
**Severity:** important
Resolved in 358d9c88: with the toggle false, the profile.d hook, its .bashrc line and the tmpfiles aging rule are removed; ~/.cache and ~/.npm symlinks are documented to stay (they point at real cache data).

### Deferral lifetime does not represent blocked Docker reclaim
**Location:** PR #1188 / #1190 — next_state, guard deferral tracking, and DevWorkerDiskGuardDeferred
**Severity:** important
Dropped by codex in round 2 (the implementation's reasoning was accepted).

### W2’s timeout and progress work remains incomplete
**Location:** PR #1190 — disk-guard ladder, run_with_heartbeat, and ReliabilityTest
**Severity:** important
Dropped by codex in round 2 (the implementation's reasoning was accepted).

### Grouping directories escape the unowned-data alert
**Location:** PR #1193 — disk-report kind_of/render and DevWorkerUnownedDataLarge
**Severity:** important
Resolved in 358d9c88: non-git children of a kind=worktrees directory are exported as kind=other `group/child` entries over --big-gb, so UnownedDataLarge sees them; test with a 25 GB `wt/dump` beside a checkout.

### Missing report roots are counted as a complete scan
**Location:** PR #1193 — disk-report scan_all/build and report freshness alerts
**Severity:** important
Resolved in 358d9c88: a missing workspace root while /workspace is not mounted is a failed scan (None; narrowed in c3c22bc9 after reviewer-claude: a user with no workspace dir on a mounted disk is simply not scanned): it keeps its last values, the run is incomplete, complete_at does not advance.

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
Dropped by codex in round 2 (the implementation's reasoning was accepted).

### Runbook presents desired fleet state as completed rollout
**Location:** docs/runbooks/dev-workers.md:19
**Severity:** nit
Dropped by codex in round 2 (the implementation's reasoning was accepted).
