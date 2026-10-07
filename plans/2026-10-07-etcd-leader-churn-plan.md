# etcd leader churn on shared consumer NVMe: analysis and remediation

## Review record

The plan went through two rounds of independent review by Codex (plan-review profile) and Fable,
read-only against the repo, the cluster and the hosts.

- **Round 1.** Both reviewers ranked the dedicated control-plane disk first. Changes adopted:
  - the election-timeout change became a bridge, with heartbeat kept at 250;
  - `io.latency` was dropped (not compiled into the kernel);
  - thermal became a factor;
  - kyverno admission replicas go to 2;
  - per-CP placement in tofu;
  - verification and rollback gates.
- **Round 2.** Both reviewers accepted the author's two pushbacks: the 5000 ms bridge is
  net-positive, and the new storage should be thick LVM. Both endorsed the ordering. They also
  supplied the corrections folded in below:
  - leadership transfer cannot target a member, so it has to be verified;
  - `talosctl reboot` does not drain, so D comes before B and reboots use the runbook's shutdown;
  - the full post-maintenance gate (CNPG / strive-pg) applies between members;
  - the tofu apply must use `apply_mode = "no_reboot"`;
  - `vms.tf` needs per-CP datastores;
  - csi-nfs needs hostname anti-affinity;
  - the alert needs the correct metric labels;
  - C starts with node1.

## Context

**Symptom.** On 2026-10-07 at 05:57Z `ControlPlaneRestartWave` (critical) fired:

- 9 control-plane controllers lost their client-go leases at 05:56:29-30Z and restarted once each:
  cnpg-operator, the kyverno admission/background/cleanup/reports controllers, csi-nfs-controller
  and snapshot-controller.
- The problem is chronic. Lifetime restarts: cnpg-operator 50 since 09-29, kyverno 45-54,
  csi-nfs 111.
- Leader changes (each member counts the same transition): 10 in the 12 h to 06:09Z; earlier 12 h
  windows 4, 2, 4, 2, 0, 6.
- The kyverno admission webhooks are `failurePolicy: Fail` with a single replica, so each
  admission-controller restart blocks matching API writes.

**Topology.**

- 3 Talos v1.11.2 CPs (etcd 3.6.4), one per Proxmox host (Bosgame M5).
  `allowSchedulingOnControlPlanes` is true.
- Each kube-apiserver uses `--etcd-servers=https://localhost:2379`.
- Each CP disk is 80 GB on `local-lvm` thin (`Zero=zero`, `discards=passdown`) on the host's single
  NVMe: a Kingston OM8TAP42048K1 2 TB, consumer QLC, DRAM-less, no PLP.
- That drive is shared with:
  - 2-3 CI runner VMs (0.6-0.9 TB/day each);
  - dev-workers and the registry LXC (node1);
  - agent nodes, reviewer VMs and an LLM LXC.
- CP disks use `cache=none`, `aio=io_uring`, `iothread=1`.
- About 85% of device FLUSHes come from non-CP guests.
- etcd timing today: `heartbeat-interval 250` / `election-timeout 2500` (`controlplane.yaml.tftpl:104-105`,
  from #951 on 09-29).
- etcd flag changes need a reboot per CP: EtcdSpec updates live, but `service etcd restart` is refused.

## Evidence

All times UTC. etcd instances 192.168.0.41/42/43:2381 = cp1/2/3; hosts 192.168.0.2/3/4.

**1. Trigger: a device-wide write freeze on ai-node3 while cp3 led.**

- cp3 logged `slow fdatasync 5.03s` (done 05:56:23.79) and `1.89s`.
- cp2 pre-voted at 05:56:21.77, about 3.0 s in, and won term 779.
- Host-side:
  - nvme average write latency 84.6 ms;
  - `io_time_weighted` 58 s/s;
  - writes 0.6-24 MB/s, discard 0;
  - memory/CPU PSI about 0, steal under 1%, peer RTT p99 6 ms;
  - no kernel or NVMe errors.
- The mechanism (QLC SLC fold / GC and/or controller thermal throttling) is a hypothesis; the
  shared-device freeze is established.

**2. Thermal.**

- NVMe sensor-1 (`temp2`) 24 h maxima: 92 / 101 / 94 °C (82-89 °C at rest).
- node2: 2 T1 throttle transitions (387 s).
- node1/node2 Warning Temperature Time: 7 / 5 min.

**3. Stall tail (WAL fsync count, 7 d):**

| | cp1 | cp2 | cp3 |
|---|---|---|---|
| > 1.024 s | 5455 | 808 | 375 |
| > 2.048 s | 1914 | 211 | 86 |
| > 4.096 s | 595 | 34 | 15 |
| > 8.192 s | 126 | 4 | 1 |

- Leaders were cp2 or cp3 throughout. cp1 is unsafe as leader.
- Failed proposals per member in 24 h: 549 / 83 / 154.

**4. ai-node1 contention (partly fixed on 10-06).**

- cp1's bad minutes track node1 runner writes (median 46 vs 12 MB/s) and node1 pool fill.
- Done:
  - registry-LXC trim: pool 71.6 → 59.8%, IO PSI about 33 → 15%;
  - Flux `platform` artifact trim (#1106).
- 7 leader changes followed anyway.

**5. Rejected.**

- Runner write caps: halved throughput at 120 MB/s, idle at 250+.
- Removing runner `discard`: refills the pool.
- `io.latency`: `CONFIG_BLK_CGROUP_IOLATENCY` is not set, and `qemu.slice` delegates no `io`.

**6. Hardware.**

- Every host shows a free M-key slot (`M.2 Socket 3: Available`); the Bosgame M5 has dual M.2 2280
  PCIe 4.0 x4. The physical fit is still to be confirmed.
- Wear: 57/54/34%. RAM: 124 GB per host.

## Approach

Order: A and D now → B once D is live → C when drives arrive (node1 first). E runs alongside.

### A. Now: procure the fix

- Order 4× M.2 2280 enterprise NVMe with full PLP, 480 GB class: Kingston DC2000B 480 GB (preferred:
  low power, heatsink) or Micron 7450 PRO 480 GB. One is a spare, for qualifying first.
- Order thermal pads / M.2 heatsinks for the existing Kingston drives too.

### D. Now, before B: shrink the restart blast radius

- cchifor/platform `deploy/components/kyverno/helmrelease.yaml` (chart 3.3.6):
  - `admissionController.replicas: 2` (the chart auto-renders a PDB `minAvailable: 1`);
  - `admissionController.container.extraArgs.leaderElectionRetryPeriod: 10s` (kyverno v1.13: lease
    60 s / renew 50 s).
- csi-driver-nfs 4.13.2 `controller.replicas: 2` only together with REQUIRED hostname pod
  anti-affinity:
  - it uses hostNetwork, and its liveness probe binds localhost:29652;
  - the chart renders `controller.affinity` only when it has `nodeSelectorTerms`, so use a combined
    affinity or a Flux postRenderer patch, then verify the rendered manifest and 2 Ready pods on
    different hosts;
  - otherwise skip it (low value).
- CNPG `--leader-lease-duration` / `--leader-renew-deadline`: platform repo, optional.
- snapshot-controller: none (already 2 replicas).

### B. After D is live: etcd election-timeout 2500 → 5000, heartbeat 250 (bridge until C)

**Change.**

- Edit `controlplane.yaml.tftpl:104-105` and rewrite its rationale comment with this plan's data.
- In the tofu `talos_machine_configuration_apply.cp` resource set `apply_mode = "no_reboot"`. It
  defaults to `auto` and fans out to all three CPs at once; any unrelated non-immediate drift would
  otherwise reboot all three together.
- Review `just plan` for unrelated drift before applying.

**Expected effect.**

- Leader stalls under 5 s no longer elect: deterministic below 5.0 s, about 11% at 5.03 s, rising
  through 10 s.
- Expect roughly 1-3 elections/day until C, not 0.
- A dead leader is detected in 5-10 s.

**Procedure** (night, one CP at a time, all Talos commands via `kubernetes/infra/_out/talosctl-1112.exe`):

1. Off-host `talosctl etcd snapshot`. Save the machine configs. Baseline the CNPG and strive-pg
   health.
2. Reboot order: cp1, then the current follower, then the current leader.
3. Before each reboot whose surviving pair includes cp1, gracefully stop node1's runner daemons
   (drain after the in-flight job).
4. Before rebooting the leader:
   - send `talosctl etcd forfeit-leadership` to the current leader only (Talos picks the first
     non-self member by ID: cp3→cp2, cp2→cp3);
   - query all three members and require them to agree that a caught-up cp2/cp3 other than the
     target leads;
   - if not, stop. etcd's own graceful stop would hand off to the longest-connected peer (cp1).
5. Reboot via the runbook's graceful path: `talosctl shutdown -n <cp>` (cordon + drain), then
   `qm start`. Do NOT use `talosctl reboot`, which does not drain on 1.11.
6. Gate before the next member (from `docs/runbooks/node-maintenance.md`):
   - etcd: 3/3 reachable, one agreed leader and term, applied index caught up, no alarms;
   - 10 min of successful API writes;
   - nodes Ready and uncordoned;
   - CNPG healthy, and strive-pg back to 3/3 on separate CPs with both replicas streaming;
   - `talosctl -n <cp> processes | grep etcd` shows `--election-timeout=5000`.
7. Abort on a renewed stall or a failed gate.

**After the roll and rollback.**

- After the roll, the leader must not be cp1.
- Rollback: the same procedure with 2500.

### C. When the drives arrive: CP disk on a dedicated PLP drive (node1 first)

**Install** (host order node1 → node2 → node3; one host at a time):

- Drain the Talos guests per node-maintenance.md and stop the host's other guests.
- Install the drive and thermal pads on both drives. Confirm the boot order and the new device's
  stable ID.
- Gate as in B.6, plus Proxmox healthy.

**Storage.**

- PVE **LVM (thick)**, with the same storage ID and VG name on all three hosts (`nodes` restricted).
- One 80 GB CP disk, no snapshots needed: no thin metadata or zeroing on the flush path.

**Tofu.**

- `kubernetes/infra/vms.tf` today uses the global `var.vm_datastore` for every CP disk. Add a per-CP
  datastore (`control_planes` field) as a code change.
- bpg's behaviour on a `datastore_id` change (move vs replace) is unverified. Refresh, and NEVER apply
  a plan that shows a CP disk move or replace; adopt the hand move instead until `just plan` is clean.

**Move.**

- `qm disk move <vmid> scsi0 <storage> --bwlimit <KiB/s>`, keeping the source (`--delete 0`).
- Do it with that CP as a follower, at night, with CI drained on that host (and node1 runners
  stopped whenever cp1 is one of the two survivors). Monitor quorum throughout.
- Verify scsi0 and boot. Reclaim the old volume only after validation.
- If the drives land within about a week of B, fold B's reboots into C's host-down windows.

**Gate per host.**

- WAL p99 under 10 ms, and zero fsyncs over 1.024 s/day over several busy CI days.
- Drive temperature in range.

### E. Alongside: housekeeping and detection

- Weekly chunked registry-LXC trim on node1 from a host timer, in CI-quiet hours, watching WAL
  latency.
- Alert `node_hwmon_temp_celsius{job="proxmox-node",chip="nvme_nvme0",sensor="temp2"} > 95` for
  15 m.
  - `job` filter: the LLM LXC exports the same hwmon.
  - 95, not 90: the at-rest temperature is 82-89.
- Alert `etcd_server_is_leader{instance="192.168.0.41:2381"} == 1` for 15 m (warning: forfeit), until
  C is done on node1.
- Confirm WAL/backend-commit latency and no-leader alerts exist. Keep `ControlPlaneRestartWave`.

### Deferred

- Runner `cache=unsafe`/`writeback`: a 48 h trial on node3, after C.
- Moving ci-runner-9 to node3: after C.
- NVMe APST tuning: low prior.

## Critical files

- `kubernetes/infra/machine-config/controlplane.yaml.tftpl` and the tofu `talos_machine_configuration_apply.cp`
  (`apply_mode`) (B).
- `kubernetes/infra/vms.tf` (per-CP datastore) plus state (C).
- `docs/runbooks/node-maintenance.md`, `docs/runbooks/ai-host-setup.md` (B, C, E).
- cchifor/platform `deploy/components/kyverno/helmrelease.yaml` (D).
- `kubernetes/apps/infrastructure/storage/csi-driver-nfs.yaml` (D).
- `kubernetes/apps/infrastructure/monitoring/` rules, and a host trim timer (E).

## Verification

- **Baseline first.** Per member, 3 busy days before and after each change:
  - WAL and backend p99, plus counts over 1.024/4.096 s;
  - leader changes, with planned transfers excluded;
  - failed proposals;
  - API write latency and errors;
  - controller restarts and pod replacements;
  - runner job durations.
- **D:**
  - 2 Ready admission-controller pods on different nodes, and the PDB present;
  - no admission failures while one replica restarts;
  - kyverno restarts stop clustering with elections.
- **B:**
  - `talosctl processes` shows 5000 on all members;
  - no term change for stalls under 5 s;
  - 3 or fewer leader changes per day;
  - API outage durations not longer;
  - the leader is not cp1.
- **C:**
  - `qm config` shows scsi0 on the PLP storage, and the tofu plan is clean;
  - per-member WAL p99 under 10 ms and zero fsyncs over 1.024 s/day over several busy days;
  - temperatures in range.
- **E:** the timer ran, the pool reclaimed space without WAL spikes, and the alerts evaluate.

<!-- codex-review-status: finalized -->
