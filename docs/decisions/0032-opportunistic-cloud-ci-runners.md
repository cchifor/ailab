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
  (`Conflicts=shutdown.target` was missing), and `cluster-power.sh` gave guests a 120 s shutdown
  attempt followed by an unconditional host poweroff (effectively ~5 min with `pve-guests`' default,
  not the 720 s a draining runner needs).

## Amendment 2026-09-28 — the Homepage OFF drains at the forge, then powers off

**Why.** The first real OFF with the runners live (2026-09-24 23:54) cancelled a job on cloud-ci-3
exactly 10 min into act_runner's drain. The in-guest layer (decision 3) caps every drain at
`shutdown_timeout`, while the fleet's job durations have a p99 of ~13 min (`gatekeeper`) and a max
of 50 min; a longer cap inside a host shutdown would still be invisible, uncancellable and bounded
by `poweroff.target`'s 30-min job timeout.

**Decision.** The OFF button no longer shuts the hosts down on confirm; it SCHEDULES the power-off
(`kubernetes/apps/apps/cloud-power/`, plan `plans/2026-09-28-cloud-power-scheduled-drain-plan.md`):

1. Pause every enabled `cloud-ci-*` runner in Gitea (`PATCH /orgs/cchifor/actions/runners/{id}
   {"disabled": true}`, Gitea 1.26): `PickTask` hands a disabled runner nothing, a running task
   carries on. The ailab pool keeps taking jobs.
2. Wait until no cloud runner has a job in flight (org jobs `status=in_progress` by `runner_name`,
   unioned with the runner `busy` flag) on two polls 20 s apart. No drain cap below the job timeout.
3. Shut the nodes down as before (`pve-guests` + the RTC hook), then re-enable each paused runner
   once the hosts have been unreachable for 5 min straight AND Gitea reports it offline, so the pool
   is whole at the next wake with no morning step.
4. Anything that does not happen in time (3 h 15 min drain, a host still up 40 min after the
   shutdown — past `poweroff.target`'s 30-min forced power-off — or no node sent the request) leaves
   the schedule `stalled`: hosts on, runners paused, the page says why; never a forced power-off and
   never a replayed shutdown. CANCEL (or ON) hands the runners back once no host can still be
   mid-shutdown.

The schedule is persisted in the runtime `cloud-power-state` ConfigMap, so a pod restart resumes
it. Decision 3's in-guest drain, `startup: down=720` and `cluster-power.sh` remain the backstop
for the paths that do not go through the button (PVE UI, `poweroff`, the CLI); moving
`cluster-power.sh down` onto the same forge-level drain is a cloudlab follow-up.

**Consequences.** cloud-power now holds an org-owner Gitea PAT (`write:organization`; no narrower
scope exists for runner pause) and a ServiceAccount limited to its own state ConfigMap. The watchdog
(decision 4) should see no loss-correlated failures from a button OFF; it still covers hard power
loss and the non-button paths.

## Amendment 2026-10-04 — cloud2 joins; 12 active cloud runners, placed on measured load

**Why.** Measured over 2026-09-27 → 10-04 (Prometheus at 5 min; the Gitea DB). On the busy days
(Mon 09-28, Tue 09-29, Sat 10-03) jobs used 255–270 runner-hours of the ~328 the pool offers (8 × 24 h
plus 7 cloud runners × ~19.5 h). Jobs with no `needs:` waited p50 4–13 min and p90 12–37 min to start.
On quiet days the p50 was under 30 s. The gap between one task and the next on a runner is 1 s at
p50, so the waiting comes from capacity, not dispatch overhead. `platform` is 91 % of runner-hours.
Each cloud runner VM uses p99 4.4 of 8 vCPU and at most 10.7 GiB of its 24 GiB (14 d at 30 s). cloud2
(Threadripper 3990X, 128 threads, 125 GiB) ran no runners: its BIOS had SVM disabled and locked
(`VM_CR` = 0x18). A site visit on 2026-10-04 enabled it (Gigabyte TRX40 AORUS MASTER, Tweaker →
Advanced CPU Settings → SVM Mode).

**Decision.**
1. **cloud2: cloud-ci-9/10/11** (6109–6111; `.13`/`.44`/`.50`), at the same size as the rest. On paper
   cloud2 is now full: 48 GiB for cloud-exec-2 + 3 × 24 + 5 = 125 GiB. Measured MemAvailable was
   108 GiB at its lowest, which leaves ≥ ~36 GiB with all three at ceiling. Two disks go on `local-lvm`
   (Samsung 970 EVO Plus) and one on `local-nvme`. That pool fills its Kingston NV2 first, so the
   write load is split across two drives.
2. **cloud3: cloud-ci-12/13** (6112/6113; `.7`/`.12`) as its sixth and seventh. Measurement supersedes
   the earlier "a sixth is CPU-bound" note. Over the week the host's CPU was 37 % busy at p99,
   measured as 1 − idle (which counts iowait). Doing real work (user + system), it used 15 of 64
   threads at p99, about 24 % (a separate p99). CPU pressure was 1.6 % at p99, and steal was about
   zero. MemAvailable was 91 GiB
   at its lowest, which leaves ≥ ~43 GiB after two more at ceiling. Both disks go on `local-nvme`.
   On cloud3's boot NVMe, where cloud-ci-3/4/5 live, guest write latency is 21–27 ms at p99. On
   `local-nvme` (cloud-ci-7/8) it is 7–8 ms.
3. The cloud pool grows from 7 to **12 active runners** (cloud-ci-6 stays quarantined), so the
   daytime pool is 8 + 12 = 20. Expected busy-day utilisation drops from ~82 % to ~61 %.

**Consequences.**
- **No static addresses left.** The ailab IPAM block has none free. The next one needs a release or
  a router DHCP-pool shrink.
- **Keep-or-revert gates**, checked after two busy days:
  - host CPU p99 < 50 %, measured as 1 − idle (the conservative one of the two figures above);
  - host MemAvailable never below 30 GiB;
  - in-guest write latency p99 < 15 ms;
  - cloud-llm-3 serving unaffected.

  Rolling back one runner is `started = false` in cloudlab `runner_nodes`.
- **Host I/O pressure is not a usable gate here.** On cloud1/cloud3 the host's I/O PSI reads ~90 %
  at p90 while the disks are ~6 % busy at p50 and no task is blocked on I/O. Judge disks by
  in-guest latency instead.
- **Watch NVMe wear.** cloud3's boot NV3 wrote 14.5 TB that week. Check SMART `percentage_used`.

## Amendment 2026-10-12: cloud3 paused (bad DRAM); cloud1 grows to five smaller runners

**Why.** On 2026-10-12 every cloud3 guest was corrupting data in memory.
- Random segfaults hit the cloud3 runners (cloud-ci-3: 59 in 8.8 h), and none hit the cloud1/2 runners
  (~200 h of uptime).
- Files in the CI caches went corrupt. Twice that day a root filesystem died: cloud-ci-5/7 in the
  morning, and cloud-ci-7 again at ~22:00 after it had been returned to service. It then failed 39
  of 42 jobs.
- memtester inside cloud-ci-7 found a page that does not hold writes (byte lane 4). It was retired
  at every boot (cloudlab `host/retired-pages.tsv`), but cloud3 kept crashing, so the fault is wider
  than that page.
- cloud1 has the #848 fault: a host scan found 8 bad pages (byte lane 5), now also retired.

The operator also asked for three more runners on cloud1/cloud2. Each runner VM costs its host about
its full allocation: 23-24 GiB RSS per 24 GiB runner, because the guest page cache stays resident.
cloud2's 7-day MemAvailable low was 35.2 GiB, which is no room above this ADR's 30 GiB gate.
cloud1's was 72.6 GiB.

**Decision.**
1. **All seven cloud3 runners (cloud-ci-3/4/5/7/8/12/13) are PAUSED in Gitea** until cloud3's DIMMs
   are tested or replaced.
   - The pause persists across the nightly power cycle, because the cloud-power drain never
     re-enables a runner that was already disabled.
   - Re-enable with `PATCH /api/v1/orgs/cchifor/actions/runners/<id> {"disabled": false}` (org-owner
     token).
2. **cloud1 runs five runners.** cloud-ci-1/2 stay at 24 GiB / 8 vCPU. cloud-ci-6 (.29, rebuilt on a
   fresh disk), cloud-ci-14 (.37) and cloud-ci-15 (.39) run at **12 GiB / 6 vCPU**, the size of the
   smaller ailab runners, which run the same jobs under the same 10 GiB job cap.
   - Expected cloud1 low: 72.6 - 3 x 12 = **~36.6 GiB**, which passes the 30 GiB gate by ~6 GiB.
   - That assumes a 12 GiB guest costs its host ~its full allocation, as the 24 GiB ones were
     measured to. It is not yet measured at 12 GiB; the two-day gate below checks it.
   - vCPUs: 2 x 8 + 3 x 6 = **34 on 32 host threads**, a slight oversubscription.
     - Measured runner CPU is ~4.4 vCPU at p99 (this ADR), so five runners need ~22 threads at
       their p99.
     - The 10 GiB job cap keeps the jobs themselves bounded.
     - Hence the 75 % CPU gate and the job-duration check below.
   - Disks go on `local-nvme`.
   - Each new VM's RAM is scanned before it takes a job, and it gets a short canary (segfaults, EXT4
     errors, failure rate) before it counts as in service.
3. **The daytime pool:** 8 ailab + 3 cloud2 + 5 cloud1 = **16** while cloud3 is paused (23 once it
   returns).

**Gates (cloud1), after two busy days:**
- host MemAvailable never below 30 GiB;
- host CPU p99 < **75 %**, amended from 50 % for five runners on 32 threads; judge job durations
  alongside it;
- in-guest write latency p99 < 15 ms;
- no segfault or EXT4 error on the cloud1 runners.

Roll back one runner with `started = false`.
