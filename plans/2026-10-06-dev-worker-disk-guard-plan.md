# 2026-10-06 — dev-worker disk: pressure guard + safe routine reclaim

## Incident
dev-worker-3 `/workspace` (scsi1, 125G; worktrees + docker/containerd data-root) reached 100% on
2026-10-06 five days after the 10-01 cleanup (#1005). The codex agent could not start any command
(sandbox ENOSPC) mid-plan. Hand-unblocked: `docker builder prune -af` (13.5 GB) + anonymous-volume
prune (12.1 GB) → 22 GB free. #1083 adds the anonymous-volume prune to the daily docker unit.

## Root cause (measured)
1. **Every reclaim is calendar-driven, none is pressure-driven.** Daily timers (03:00 buildx cap,
   03:30 deps/worktree prune) cannot keep up with intra-day churn: dw3 created 180 anonymous postgres
   volumes on 10-04 alone. Between runs nothing reacts to the disk filling. The CI runners already
   solved this class with disk-gated reclaim (`gitea-runner-cleanup.sh` §3b/3c); dev-workers did not.
2. **Uncovered accumulators.** Anonymous volumes: nothing reaped them (240 / 12.1 GB on dw3, 85 / 9 GB
   on dw4) — fixed by #1083. Build cache: daily `--max-used-space 20GB` reclaimed 0 B on dw3 every day
   (13.5 GB < cap) — the cap is a size target, not a disk target. Unused images: 9.2 GB reclaimable on
   dw3, nothing automatic.
3. **Worktree prune still report-only** (`dev_worker_worktree_prune_mode: report` since #1029, 10-02):
   dw3 40 merged/stale worktrees = 9.8 GB, dw4 51 = 6.2 GB, dw1 33 = 1.2 GB waiting. Reports have
   run clean on all 4 workers for 4 days (no errors, counts consistent).
4. **No agent guidance**: agents `docker rm` throwaway containers without `-v`.
5. **Alerting**: `DevWorkerDiskFilling` (<12% for 15m) is a static threshold; a fast fill (tens of
   GB in hours) goes from warning to 100% before anyone acts, and there is no signal that automatic
   reclaim has run out of options.

## Design
Principle: **routine = zero-risk categories on a calendar; pressure = escalating ladder of
regenerable things, only while the disk actually needs it; exhaustion = page a human.**

### A. `dev-worker-disk-guard` (new) — systemd timer every 5 min, root, Nice/idle IO
`files/disk-guard` (stdlib Python, tested in `scripts/tests/test_dev_worker_disk_guard.py`).
- Reads free% of `/` and of the filesystem holding the docker data-root (`/workspace`).
- **Trigger**: any watched fs below `low` (default 15% free). **Target**: stop as soon as every
  watched fs is above `target` (default 25% free). Hysteresis prevents flapping.
- Ladder, one step at a time, re-measuring between steps, docker steps only if the docker fs is low:
  1. `docker builder prune -af` — all unused build cache (in-use records are kept by buildkit).
  2. `docker volume prune -f --filter label=com.docker.volume.anonymous`.
  3. `cleanup --y --keep-stacks --image-days 3 --stopped-hours 24` — stopped non-stack containers
     >24h, images unused >3d, anon volumes. Never compose stacks, never named volumes.
  4. `cleanup --y --no-docker --worktrees --merged-days 1 --worktree-days 30` — only when
     worktree_prune_mode == remove.
  5. `cleanup --y --no-docker --deps --deps-days 3` — node_modules/.venv/target of worktrees idle 3d.
  Each step runs under `timeout`; a failing step logs and the ladder continues.
- Single-instance flock (`/run/lock/dev-worker-disk-guard.lock`); `cleanup` keeps its own lock —
  if the daily run holds it the guard step logs "busy" and retries next tick.
- Writes `dev_worker_disk_guard.prom` (textfile collector dir `/var/lib/prometheus/node-exporter`,
  already active on the workers): last_run_timestamp, triggered, last_step, reclaimed_bytes,
  exhausted (1 = ran every step and still below `low`).
- Every run logs a one-line summary to the journal; removals logged via cleanup's own table.

### B. Routine (daily) stays conservative
- docker-buildx-prune unit: buildx cap + anonymous volumes (#1083). Images/containers NOT routine
  (a locally built, unpushed image may be wanted) — only under pressure.
- Flip `dev_worker_worktree_prune_mode: remove` (4 days of clean fleet reports).

### C. Prevention — agent guidance
Managed block (`blockinfile`, HTML-comment markers) in each user's `~/.claude/CLAUDE.md` and
`~/.codex/AGENTS.md`: `docker run --rm` for throwaway containers, `docker compose down -v` for
throwaway stacks, `docker system df` / `cleanup --dry-run` when the disk is low, a disk guard exists
and what it removes, keep artifacts you need outside worktrees you intend to delete.

### D. Alerting (`dev-workers-rules.yaml`)
- `DevWorkerDiskGuardExhausted` — exhausted == 1 for 15m (critical-ish: live work fills the disk;
  needs a human or more capacity).
- `DevWorkerDiskGuardStale` — guard last_run older than 30m on a worker that is up.
- Update `DevWorkerDiskFilling` description to mention the guard.

### Out of scope
- Disk capacity growth (125G scsi1) — revisit only if the guard reports exhaustion.
- dw1 build cache 33.9 GB with only 2.2 GB reclaimable (shared with images) — the guard's image
  step frees those records under pressure; separate investigation otherwise.
- Fixing the specific test loop that leaked postgres volumes (agent-owned code).

## Rollout
`ansible-playbook dev-workers.yml --tags cleanup,buildx_prune,disk_guard,agent_docs` on all 4;
`systemctl list-timers dev-worker-disk-guard*`; `disk-guard --dry-run` on each; check the .prom file
and the Prometheus series; Flux applies the rule change on merge.

## Codex plan review (2026-10-06) — disposition
- **Accepted — docker prunes during a live pull/build.** Verified in `gitea-runner-cleanup.sh`
  (busy-gate comment): ANY docker prune triggers a containerd GC pass that reaps an in-flight pull's
  lease; measured on the runners 52.6% vs 19.0% job failure. → Busy gate: docker steps (build cache,
  anon volumes, cleanup's docker pass) are re-checked before each step against running docker clients
  (`build|bake|pull|push|load|import|run|create|up`) and deferred to the next tick unless the docker fs
  is under `--critical` (5%). Deferring is not "exhausted". The daily 03:00 prunes keep this race at the
  quietest hour (unchanged).
- **Rejected — "remove images unused >3d is unsafe; use 24h".** Backwards: a 24h threshold removes
  MORE. The real hazard (GC vs in-flight pull) is covered by the busy gate; 3 days only under pressure.
- **Rejected — "4 days of reports doesn't validate removal".** Removal is `git worktree remove` without
  `--force` as the owner, after a fresh re-judge (`remove_worktree`); git itself refuses a dirty tree.
  The first real removal is run by hand on dev-worker-3 during rollout and its output checked.
- **Rejected / already covered**: container-before-image order (cleanup's plan already does that);
  textfile write at full disk (the textfile dir is on `/`, the write is atomic, and a failed write is
  exactly what `DevWorkerDiskGuardStale` reports); hysteresis unspecified (it is: 15% trigger, 25% stop);
  5-min interval (a no-op tick is two statvfs calls and prints nothing).
