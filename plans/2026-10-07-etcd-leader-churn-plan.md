# etcd leader churn on shared consumer NVMe: analysis and remediation

## Codex Review (round 2)

- **Timeout pushback accepted; concern dropped.** The observed stall distribution supports trying
  250/5000 as a reversible bridge, with API outage duration checked alongside election counts.
- **Thick-LVM pushback accepted; concern dropped.** Native PVE thick LVM is appropriate for one
  80 GB disk without snapshot or clone requirements; thin provisioning adds no needed capability.
- **New material issues:** leadership transfer cannot select the intended successor with the stated
  Talos command; between-node gates must include CNPG recovery; two NFS controller replicas need
  enforced host separation. Corrections are annotated below.
- **Ordering endorsed:** A+D now, B this week as a bridge, C when drives arrive is the best solution
  supported by the evidence, subject to the operational corrections below.

## Review decisions (round 1: Codex + Fable)

Both reviewers independently ranked the dedicated control-plane disk first.

- **Accepted from both:**
  - The election-timeout change is a *bridge*, not a fix; it does not guarantee riding out a 5 s
    stall.
  - `io.latency` is dropped: the kernel lacks `CONFIG_BLK_CGROUP_IOLATENCY` and `qemu.slice` does not
    delegate `io`.
  - Thermal is a real factor.
  - The verification and rollback gates are tightened.
- **Accepted from Codex:**
  - Keep heartbeat 250 and raise only election-timeout. A simulation agrees: for a 5.03 s leader
    stall, P(election) is 11% at 250/5000 vs 20% at 500/5000 vs 100% at 250/2500.
  - snapshot-controller already runs 2 replicas.
  - The csi-nfs chart hard-codes its sidecar arguments.
  - Kyverno and CNPG live in cchifor/platform.
  - CP disk placement is declared in `kubernetes/infra/vms.tf` and must be reconciled after a move.
  - Verify running flags, not EtcdSpec.
  - Take an off-host etcd snapshot before any roll.
  - Reduce CI load during maintenance.
- **Accepted from Fable:**
  - Kyverno's admission webhooks are `failurePolicy: Fail` with a single replica, so a restart blocks
    API writes. Raise it to 2 replicas.
  - Use `talosctl reboot`.
  - Keep cp1 from holding leadership until the disk move.
  - Concrete PLP part numbers.
  - NVMe controller temperatures reach 92-101 °C.
- **Resolved in round 2:** Codex accepts thick LVM for the new storage and the election-timeout
  bridge. Both original concerns and their matching pushback markers have been removed.

## Context

**Symptom.** On 2026-10-07 at 05:57Z, `ControlPlaneRestartWave` (critical) fired:

- 9 control-plane controllers lost their client-go leases ("context deadline exceeded" / "leader
  election lost") at 05:56:29-30Z and restarted once each: cnpg-operator, the kyverno
  admission/background/cleanup/reports controllers, csi-nfs-controller and snapshot-controller.
- They recovered on their own.
- The problem is chronic. Lifetime restarts: cnpg-operator 50 since 2026-09-29, kyverno 45-54,
  csi-nfs-controller 111.
- Leader changes, per member (each member counts the same transition):
  - 10 in the 12 h to 06:09Z;
  - 4, 2, 4, 2, 0 and 6 in the earlier 12 h windows.
- Restarts happen when API calls stall for longer than the clients' renew deadline, which elections
  and long leader stalls both cause.

**Topology.**

- 3 Talos CPs, one per Proxmox host (ai-node1/2/3, Bosgame M5). `allowSchedulingOnControlPlanes` is
  true, so the CPs also run app pods.
- Each CP VM disk is 80 GB on `local-lvm` thin, on the host's single NVMe: a Kingston OM8TAP42048K1
  2 TB, consumer QLC, DRAM-less, no PLP.
- The same drive carries:
  - 2-3 CI runner VMs (200 GB each, docker-build churn of 0.6-0.9 TB/day each);
  - dev-workers and the Zot registry LXC (node1);
  - a Talos agent-node VM;
  - reviewer VMs and an LLM LXC (node3).
- The CP disk config is right for etcd: `cache=none`, `aio=io_uring`, `iothread=1`, guest write-cache
  on, so flushes propagate.
- About 85% of the device's FLUSH commands come from non-CP guests: host 202/146/126 flush/s vs
  21-25/s per CP.
- etcd's WAL sits on the CP VM disk (Talos EPHEMERAL).
- Timing today is `heartbeat-interval 250` / `election-timeout 2500`
  (`kubernetes/infra/machine-config/controlplane.yaml.tftpl:104-105`), raised from 100/1000 on 09-29
  (#951).
- etcd flag changes need one controlled reboot per CP: Talos updates EtcdSpec live but the process
  keeps its old flags, and `service etcd restart` is refused.

## Evidence

All times UTC. Queries use etcd histograms on instances 192.168.0.41/42/43:2381 (cp1/2/3) and
node-exporter on hosts 192.168.0.2/3/4.

**1. Today's trigger: a device-wide write freeze on ai-node3 while cp3 was leader.**

- cp3 logged `slow fdatasync 5.03s` (completed 05:56:23.79) and `1.89s` (05:56:25.68).
- cp2 pre-voted at 05:56:21.77, about 3.0 s in and inside the current 2.5-5 s window, and won term
  779.
- On the host:
  - nvme average write latency 84.6 ms;
  - `io_time_weighted` 58 s/s, i.e. the whole device frozen about 5 s;
  - only 0.6-24 MB/s of writes, discard 0;
  - memory PSI 0, swap 0, CPU PSI about 0, cp3 steal under 1%, peer RTT p99 6 ms;
  - no kernel or NVMe errors.
- The exact firmware mechanism is a hypothesis: QLC SLC-cache fold / GC and/or controller-die
  thermal throttling. A shared-device tail-latency freeze is established.

**2. Thermal.**

- NVMe sensor-1 24 h maxima: node1 92 °C, node2 101 °C, node3 94 °C (87-89 °C during the event).
- node2 SMART: T1 throttle transitions 2, 387 s total.
- node1/node2 "Warning Temperature Time" 7/5 min.

**3. Stall-length distribution (WAL fsync, 7 d, count above threshold):**

| | cp1 | cp2 | cp3 |
|---|---|---|---|
| > 1.024 s | 5455 | 808 | 375 |
| > 2.048 s | 1914 | 211 | 86 |
| > 4.096 s | 595 | 34 | 15 |
| > 8.192 s | 126 | 4 | 1 |

- Leaders in the last 36 h were always cp2 or cp3.
- cp1 is a dangerous leader candidate, with 126 stalls over 8 s in a week.

**4. ai-node1 contention (analysed and partly fixed on 10-06).**

- cp1's bad minutes correlate with node1 runner writes: median 46 vs 12 MB/s.
- node1 had the fullest thin pool (72% vs 51/34%) and the highest avg write latency
  (11.6 vs 4.1/1.0 ms). Pool allocation is only a proxy for the SSD's real NAND occupancy.
- Done:
  - chunked registry-LXC trim: pool 71.6 → 59.8%, node1 IO PSI about 33 → 15%;
  - Flux `platform` artifact trim (#1106): cp1 own-disk writes 2.3-4.0 GB → 54 MB per 10 min.
- cp1's slow fsyncs fell, but there were 7 leader changes between 20:18Z and 06:09Z.

**5. Rejected levers.**

- Runner write caps: 120 MB/s halved fleet throughput on 09-29, and higher caps barely engage.
- Removing runner `discard`: refills the pool.
- `io.latency`: not compiled in (`# CONFIG_BLK_CGROUP_IOLATENCY is not set`, kernel 7.0.2-6-pve).
  dm-thin worker IO is unattributable anyway.
- None of these touch device-internal freezes.

**6. Hardware.**

- All three hosts show a free second M-key slot (`M.2 Socket 3: Available`). The Bosgame M5 is
  documented with dual M.2 2280 PCIe 4.0 x4 behind the bottom panel.
- Physical length, keying and clearance are still to be confirmed on the first host opened.
- Drive wear (SMART percentage_used): 57/54/34%.
- RAM: 124 GB per host, 84-98 GB used.

## Approach

Ordered by value; A and D run in parallel.

### A. Now: procure and qualify the dedicated CP disk (the fix)

- Order 3× M.2 2280 enterprise NVMe with full power-loss protection, 480 GB class:
  - Kingston DC2000B 480 GB (preferred: low power, heatsink; matters at these temperatures);
  - or Micron 7450 PRO 480 GB.
- Buy one extra to qualify first.
- Inspect cooling while each host is open: thermal pad or heatsink on BOTH drives, and check chassis
  airflow.

### B. This week: election-timeout 2500 → 5000, heartbeat stays 250 (bridge until C)

- Edit `controlplane.yaml.tftpl:104-105` and rewrite the rationale comment with this plan's data.
  Ratio 20× is fine.
- Expected effect:
  - leader stalls under about 5 s (cp2: 211 events/7 d over 2 s vs 34 over 4 s) stop causing
    elections;
  - stalls of 5-10 s cause an election with rising probability;
  - expect roughly 1-3 elections/day until C, not 0.
- Cost: a dead leader is detected in 5-10 s instead of 2.5-5 s.
- Procedure (night window, one CP at a time):
  1. Take an off-host etcd snapshot (`talosctl etcd snapshot`). Save the machine configs. Confirm
     CNPG replicas are streaming.
  2. Apply the config to all three in no-reboot mode. Review `just plan` for unrelated drift first;
     do not use a broad apply.
  3. Reboot order: **cp1 first**, so cp2+cp3, the healthiest pair, carry quorum. Then the current
     follower. The current leader goes last, after `talosctl etcd forfeit-leadership` to a caught-up
     member other than cp1.
     <!-- codex: round-2: This command cannot select the intended successor: Talos v1.11.2 forfeit-leadership takes no destination and its implementation selects the first other member, which can be cp1. See the [CLI source](https://github.com/siderolabs/talos/blob/v1.11.2/cmd/talosctl/cmd/talos/etcd.go) and [transfer implementation](https://github.com/siderolabs/talos/blob/v1.11.2/internal/pkg/etcd/etcd.go). Use the pinned kubernetes/infra/_out/talosctl-1112.exe for all Talos commands here, as node-maintenance.md requires; the system client is v1.6.2. Address the forfeit request only to the current leader, then query all three CP IPs and require agreement that a caught-up cp2/cp3 other than the reboot target is leader BEFORE proceeding. If that condition is not met, stop and establish a verified targeted transfer procedure; the after-roll cp1 check is too late to enforce this maintenance invariant. -->
  4. For the two reboots that leave cp1 in quorum, first stop node1's runner daemons (graceful drain,
     after the in-flight job). Every cp1 stall is a commit stall while only two members are up.
  5. Reboot with `talosctl reboot -n <cp>` (drain per node-maintenance.md).
  6. Gate between members:
     - all 3 members reachable, one agreed leader and term, applied index caught up, no alarms;
     - 10 min of repeated successful API writes;
     - the etcd startup log (process args) shows `election-timeout=5000`.
     <!-- codex: round-2: Include the full post-maintenance gate from docs/runbooks/node-maintenance.md before every subsequent CP reboot in B and host shutdown in C: nodes Ready and uncordoned, CNPG healthy, and strive-pg back at 3/3 on separate CPs with both replicas streaming. The runbook documents required hostname anti-affinity and slow Trident reattachment, so a database instance can remain Pending after etcd and API writes have recovered. Checking replication only before the roll and waiting ten minutes does not establish recovery; a second drain could leave only one database instance. -->
  7. Abort on a renewed stall or a failed gate, and recover before touching the next member.
- After the roll: if cp1 is leader, forfeit.
- Rollback: the same procedure back to 2500.

### C. When the drives arrive: move each CP VM disk onto its own PLP drive

- Install one host at a time: drain its guests per node-maintenance.md; physical install + thermal
  pads; confirm boot order and the new device's stable identity.
- Create a PVE **LVM (thick)** storage on the new drive, one VG per host.
- Move the disk: `qm disk move <vmid> scsi0 <new-storage> --bwlimit <measured>`, keeping the source
  disk, while that CP is a follower, at night with CI drained on that host. Monitor quorum throughout.
- Then:
  - verify scsi0 points at the new volume and the guest boots from it;
  - reconcile `kubernetes/infra/vms.tf` (per-CP datastore) until `just plan` shows no drift;
  - only then reclaim the old volume.
- App pods on the CPs move with the disk (images, logs, emptyDirs). That is fine at 480 GB, and
  still far less churn than the runners.
- Gate per host: that member's WAL fsync p99 under 10 ms, and zero fsyncs over 1.024 s for several
  busy CI days.

### D. This week, cheap: shrink the restart blast radius

- cchifor/platform `deploy/components/kyverno/helmrelease.yaml`:
  - `admissionController.replicas: 2`, so the `failurePolicy: Fail` webhooks keep a serving replica
    through a restart, plus a PDB;
  - optionally `--leaderElectionRetryPeriod=10s` (lease 60 s, renew 50 s).
- csi-driver-nfs (`kubernetes/apps/infrastructure/storage/csi-driver-nfs.yaml`):
  `controller.replicas: 2`, if the chart exposes it. Lease flags would need a postRenderer patch;
  skip those.
  <!-- codex: round-2: The pinned chart v4.13.2 exposes controller.replicas, but scaling it alone leaves a placement hazard: the controller uses hostNetwork and its liveness-probe binds localhost:29652, with no default pod anti-affinity. Colocated replicas can therefore hit a port collision and fail to become fully Ready. Require placement on different hostnames and verify two fully Ready controller pods. Check the rendered manifest: this chart emits controller.affinity only when it contains nodeSelectorTerms, so a podAntiAffinity-only value is silently ignored; use a combined affinity configuration that renders correctly or a postRenderer placement patch. See the [controller template](https://github.com/kubernetes-csi/csi-driver-nfs/blob/v4.13.2/charts/v4.13.2/csi-driver-nfs/templates/csi-nfs-controller.yaml) and [chart defaults](https://github.com/kubernetes-csi/csi-driver-nfs/blob/v4.13.2/charts/v4.13.2/csi-driver-nfs/values.yaml). -->
- CNPG operator lease flags (`--leader-lease-duration`/`--leader-renew-deadline`): platform repo,
  optional.
- snapshot-controller: no change (already 2 replicas).

### E. This week: housekeeping and detection

- Weekly chunked trim of the registry LXC (node1) from a host timer, in CI-quiet hours, watching WAL
  latency.
- Alerts:
  - NVMe sensor-1 over 90 °C for 10 min (`node_hwmon_temp_celsius`);
  - `etcd_server_is_leader{instance="192.168.0.41:2381"} == 1` for 15 m (warning: forfeit), until C
    is done;
  - confirm WAL/backend-commit latency and no-leader alerts exist.
- Keep `ControlPlaneRestartWave` as is.

### Deferred / not recommended now

- Runner disks `cache=unsafe` or `writeback`. This removes about 85% of device flushes. Revisit after
  C, as a measured 48 h trial on node3; runner FS corruption on a host crash is acceptable because
  runners can be rebuilt.
- Moving ci-runner-9 to node3: not before C, because node3 hosts the healthiest leader candidate.
- NVMe APST `nvme_core.default_ps_max_latency_us=0`: low prior.

## Critical files

- `kubernetes/infra/machine-config/controlplane.yaml.tftpl` (B).
- `kubernetes/infra/vms.tf` plus state, for the per-CP datastore (C).
- `docs/runbooks/node-maintenance.md`, `docs/runbooks/ai-host-setup.md` (B, C, E procedures).
- cchifor/platform `deploy/components/kyverno/helmrelease.yaml` (D).
- `kubernetes/apps/infrastructure/storage/csi-driver-nfs.yaml` (D).
- Monitoring rules under `kubernetes/apps/infrastructure/monitoring/` (E).
- A host timer for the trim (E).

## Verification

- **Baseline first.** Per member, for 3 busy days before and after each change separately:
  - WAL and backend-commit p99, plus counts over 1.024/4.096 s;
  - leader changes, with planned transfers excluded;
  - API write latency and errors;
  - controller restarts and pod replacements;
  - runner job durations.
- **B:**
  - the etcd startup args show 5000 on all members;
  - no term change accompanies stalls under 5 s;
  - leader changes trend to 3/day or fewer;
  - API outage durations do not get longer.
- **C:**
  - `qm config` shows scsi0 on the new storage, and the tofu plan is clean;
  - per-member WAL p99 under 10 ms and zero fsyncs over 1.024 s/day over several busy days;
  - drive temperature in range.
- **D:**
  - 2 Ready admission-controller pods;
  - the Lease `renewTime` advances;
  - no admission failures during a controlled restart of one replica.
- **E:**
  - the timer ran;
  - thin-pool space was reclaimed with no trim-correlated WAL spikes;
  - the alerts evaluate.

<!-- codex-review-status: complete -->