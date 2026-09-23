# ADR 0032 — Opportunistic Gitea CI runners on the cloudlab cluster, with drain and bounded auto-rerun

**Status:** ACCEPTED (2026-09-23), operator-directed: *"run multiple runners on cloud machines
(cloud1, cloud2, cloud3) to address the bottleneck on PR validation during the day … the cloud
runners should automatically subscribe and unsubscribe … if the cloud machines are turned off and
there are cloud runners in progress, the validation tasks should be retried by ailab runners."*
**Relates to:** ADR 0013 (the runner VMs), ADR 0017 (Gitea is the forge; CI on Gitea Actions),
cloudlab ADR 0001 (day-only operation). Plan: `plans/2026-09-23-cloud-ci-runners-plan.md` (the
2026-09-15 draft's §3 refutations are what the watchdog's gates answer).

## Context

The pool is wait-bound: 8 always-on `ci-runner-N` VMs serve every `cchifor` repo through one label,
capacity 1 each; queue wait measured 2026-09-15 at median 226 s / p90 1030 s while jobs themselves
run median 14 s / p90 31 s. The ailab hosts have no RAM for more runners. The cloudlab cluster is
idle on CPU/RAM during the day and powered off by a human at night (RTC wake 08:00).

Gitea 1.26 never re-queues a task whose runner vanished: the zombie reaper fails it after 10 min,
and a graceful `act_runner` stop with no drain cancels the job at once. So "opportunistic" needs
two things the estate did not have: a drain that every shutdown path honours, and a bounded rerun
for the residual.

## Decision

1. **QEMU VMs, the ailab contract verbatim** (8 vCPU, 24 GiB / 10 GiB floor, 200 GB, same image
   from the shared QNAP export, same roles). Not LXC: host-mode CI Docker is root-equivalent, and an
   LXC escape lands on a host serving models from 4× RTX 3090. Phase 1 = 4 VMs (2 on cloud1, 2 on
   cloud3); a 5th on cloud3 is gated on a measured page-cache headroom for llama-swap; cloud2 is gated
   on its BIOS SVM actually being on (probed OFF on 2026-09-23 despite an attempted change).
2. **Same label (`self-hosted-hv:host`), no re-registration at runtime.** Registration is persistent
   and Gitea only dispatches to a polling runner, so join/leave is free. The label rename planned on
   2026-09-15 is separate work.
3. **Drain in three layers**: the role's `shutdown_timeout`/`KillMode=mixed` (ailab#745), the VM's
   `startup: down=720` (honoured by `pve-guests` stopall on the OFF-button/PVE-UI/`poweroff` path),
   and cloudlab's `cluster-power.sh down` running the drain detached on the host.
4. **A bounded auto-rerun watchdog for the residual**, shadow mode first. It re-runs a run's failed
   jobs (`rerun-failed-jobs`, which also restores cancelled dependents such as a gate job) only when
   every failed job in the run ran on a `cloud-ci-*` runner whose loss the watchdog itself observed
   within minutes of the failure, the run is done and `failure` (never `cancelled`), its sha is still
   the current head of its open PR or branch, and no newer or live run exists for the same workflow
   there. One rerun per run within a 90-day tombstone retention, caps per scan and per day, a
   ConfigMap kill switch, and a post-check that detects (and once compensates) a collateral cancel
   from the seconds-wide check→POST race.
5. **Ownership by estate boundary**: cloudlab owns the VMs and the power scripts; ailab owns the
   ansible group/playbook, IPAM, monitoring and the watchdog.

## Consequences

- The pool grows 8 → 12 during the day with no change to any workflow or org variable.
- `cloud-ci-*` are `offline` for hours every day. Every place that treats "offline" as a fault
  excludes them: the preflight script (`--include-cloud` lists, never gates), the `ci-runner-node`
  scrape (they have their own job), and the rules (host-gated joins, proven to fire in promtool).
- A `cloud-ci-*` registration must not be deleted while a job of theirs may be inside the watchdog's
  lookback (the API resolves the runner name from the live row; unknown runners fail closed).
- Stated limits of the rerun: latency ≈ 10 min zombie timeout + ≤ 60 s for hard power loss; a
  genuine failure inside the 3-minute loss-correlation window is re-run once; the rerun lands on
  any online labelled runner (ailab at full power-off; another cloud runner if only one host died,
  which is acceptable — it is up); the race is shrunk and compensated, not eliminated.
- Two live bugs fixed on the way (cloudlab): the RTC wake hook never ran at a real shutdown
  (`Conflicts=shutdown.target` was missing), and `cluster-power.sh` hard-killed guests at 120 s.
