# etcd leader churn on shared consumer NVMe: analysis and remediation

## Context

**Symptom.** At 2026-10-07 05:57Z, `ControlPlaneRestartWave` (critical) fired: 9 control-plane
controllers restarted once each within 10 min:

- cnpg-operator, kyverno admission/background/cleanup/reports
- csi-nfs-controller (3 sidecars), snapshot-controller

All had lost their client-go leader-election lease ("context deadline exceeded" / "leader election
lost") at 05:56:29-30Z. They recovered on their own. The underlying problem is chronic:

- `etcd_server_leader_changes_seen_total` rose by 10 in the 12 h to 06:09Z.
- Earlier 12 h windows: 4, 2, 4, 2, 0, 6.
- The cnpg-operator pod (created 2026-09-29) has restarted 50 times; the kyverno controllers 45-54
  times; csi-nfs-controller 111. Each election produces a restart round.

**Topology.** 3 Talos CPs, one per Proxmox host (ai-node1/2/3, Bosgame M5).

- Each CP VM disk (80 GB, `local-lvm` thin) lives on the host's SINGLE NVMe: Kingston
  OM8TAP42048K1 2 TB, consumer QLC, no PLP.
- The same drive also carries:
  - 2-3 Gitea Actions CI runner VMs per host (200 GB each, heavy docker build churn of
    0.6-0.9 TB/day per runner);
  - dev-worker VMs (node1);
  - the Zot registry LXC (node1);
  - a Talos agent-node VM;
  - reviewer VMs and an LLM LXC (node3).
- etcd's WAL (fdatasync per raft Ready) sits on the CP VM disk (Talos EPHEMERAL, /var).
- Current etcd timing (machine-config `controlplane.yaml.tftpl`): `heartbeat-interval: 250`,
  `election-timeout: 2500`. Raised from the 100/1000 defaults on 2026-09-29 (#951); leader changes
  dropped to ~0 for a while, then returned.
- Changing etcd flags needs one controlled reboot per CP. Talos updates EtcdSpec live but the
  process keeps its old flags until boot, and `service etcd restart` is refused. Procedure: roll one CP
  at a time with `_out/talosctl-1112.exe`, and require `talosctl etcd status` to show 3/3 in sync
  between reboots.

## Evidence

**1. Today's event was a leader-side WAL fsync stall on cp3 (ai-node3).**

- cp3 was the etcd leader (member 1fd6da7fa19ceda6). Its etcd log:
  - `slow fdatasync took 5.030716525s` at 05:56:23.79Z, so the sync started around 05:56:18.76;
  - another `slow fdatasync 1.89s` at 05:56:25.68.
- Sequence:
  - from 05:56:19 all members logged "waiting for ReadIndex response took too long";
  - at 05:56:21.77 cp2 started a pre-vote, then won term 779;
  - cp3 truncated its unstable entries and became follower.
- Transactions took 5-6 s around the election, and lease renewals with a 10 s client deadline failed.
- ai-node3 host for the minute covering the stall:
  - NVMe average write latency 84.6 ms (normally 2-8 ms) at only ~685 write IOPS and <1 MB/s reads;
  - host IO PSI 19% ("some"), meaning roughly 11 s stalled in that minute;
  - nvme writes 11-24 MB/s and discards ~0, so there was NO heavy load at the time;
  - no kernel/NVMe errors, SMART media_errors 0, error-log empty;
  - NVMe Thermal Management T1/T2 transition counts 0 on node3 (node2's drive shows T1 count 2,
    387 s total);
  - controller temperature sensors run 77-90 °C on all three hosts.
- Heavy CI writes on node3 only started at 05:57:30 (3.8-7.5k write IOPS), after the stall.
- Reading: a firmware-internal write stall (QLC SLC-cache fold / GC, possibly thermal) on the
  drive, not host-level contention.

**2. Multi-second WAL fsyncs happen on ALL three members, worst on cp1.**

WAL fsyncs >2.048 s:

| Window | cp1 | cp2 | cp3 |
|---|---|---|---|
| 1 d | 382 | 38 | 13 |
| 7 d | 1926 | 212 | 87 |

Hourly fsyncs >1.024 s since 2026-10-06 12:00Z:

- cp1: 33-135/h during busy CI hours, 1-5/h overnight;
- cp2: 0-31/h;
- cp3: 0-12/h.

Leaders over that period were cp2 and cp3. Leader changes per hour came in 1s, 2s and 3s
(midnight hour 3), each aligned with slow fsyncs on the then-leader. With pre-vote on, a slow
FOLLOWER (cp1) is not expected to disrupt; elections follow LEADER stalls.

**3. cp1 / ai-node1 contention was analysed and partly fixed on 2026-10-06.**

- Over 24 h at 1-min resolution, the 124 minutes with cp1 fsync p99 >500 ms had node1-runner writes
  of median 46 MB/s, against 12 MB/s in good minutes.
- Discard volume did not separate bad minutes from good ones.
- Per-runner 2-min write max was 169 MB/s, p99 ~92-116.
- What separates node1 from node2 (similar CI writes) is thin-pool fill: 72% vs 51%; QLC slows as
  it fills. Avg NVMe write latency was node1 11.6, node2 4.1, node3 1.0 ms; host IO PSI 22-30% vs <2%.
- Done on 2026-10-06:
  - (a) Chunked fstrim of the registry LXC mp0: 212 GB freed, node1 pool 71.6 → 59.8%, node1 IO
    PSI ~33 → ~15%.
  - (b) Flux `platform` GitRepository `spec.ignore`: kustomize-controller unpack writes on cp1's
    own disk went 2.3-4.0 GB → 54 MB per 10 min (ailab #1106).
  - (c) The forge Postgres reclaim (unrelated to etcd).
- Result: cp1 slow fsyncs fell to 43-77/h busy and 1-5/h overnight. Leader churn did NOT stop: 7
  elections between 20:18Z on 10-06 and 06:09Z on 10-07.

**4. Options measured or tried and rejected.**

- Runner PVE write caps:
  - 120 MB/s halved fleet write throughput on 09-29.
  - A 250/500 MB/s burst ceiling would never engage (max observed 169).
  - A cap low enough to matter (~30-40 MB/s) trades heavy CI throughput while the queue is already
    215 deep.
- Removing runner `discard`: runner disks are already 74-79% allocated WITH online discard, so pool
  fill (the harmful variable) would rise.
- Neither addresses device-internal stalls like today's, which happened at light load.

**5. Hardware facts.**

- Each host (dmidecode type 9) shows `M.2 Socket 3: Available`, i.e. a free second M.2 slot.
- Host IO scheduler `none`; cgroup v2 controllers include `io`, so `io.latency` and `io.max` work but
  `io.weight` needs bfq/iocost.
- 124 GB RAM per host, 84-98 GB used.
- Drive wear (SMART percentage_used) 57/54/34%, ~3.4-5.4 TB/day writes per node (2026-10-03),
  projecting ~100% wear around early 2027 on node1/node2.

## Approach

Recommended, in this order. Each step is independent and reversible.

### 1. Ride out stalls: etcd heartbeat/election 250/2500 → 500/5000 (this week)

- Edit `heartbeat-interval: "500"` and `election-timeout: "5000"` in
  `kubernetes/infra/machine-config/controlplane.yaml.tftpl`. Keep the 10× ratio; rewrite the
  rationale comment with this plan's data.
- `just plan` / apply config to all three CPs (`talosctl apply-config`, no reboot).
- Roll one controlled reboot per CP: `talosctl shutdown` + `qm start`, never `qm shutdown`.
  - Order: followers first, the current leader last (or `talosctl etcd forfeit-leadership` before
    its reboot).
  - Between reboots, `talosctl etcd status` must show 3/3 in sync and the apiserver must be healthy.
  - Window: a quiet CI period (night), because a stall on one of the two remaining members during a
    reboot loses quorum for the stall's length.
- Effect: followers wait 5-10 s (randomized) before campaigning, so a ≤5 s leader fsync stall (all of
  today's observed stalls) no longer causes an election.
- Cost: a genuinely dead leader is replaced in 5-10 s instead of 2.5-5 s. The apiserver is degraded
  for that long, which it already is during every stall today.

### 2. Make the restart waves non-events: longer leader-election leases for single-replica controllers (this week)

- cnpg-operator, kyverno controllers, csi-nfs-controller and snapshot-controller run as single
  replicas. Their lease is a liveness fence, not failover, so they gain nothing from the client-go
  default `LeaseDuration 15s / RenewDeadline 10s`.
- Raise to ~60s / 40s / 5s via each component's flags or env, where supported. Then a 5-15 s API
  stall no longer makes them exit.
- Cost: after a real crash, the replacement pod waits up to the remaining lease (≤60 s) before it
  acts.
- Scope: only components whose HelmRelease/manifest exposes the knobs; no forks.

### 3. Remove the cause: a dedicated PLP SSD per host for the CP VM disk (order now, install within weeks)

- Install a small enterprise NVMe with power-loss protection (TLC, M.2 2280, e.g. 480-960 GB) in each
  host's free M.2 Socket 3.
- Create a separate PVE storage on it (LVM-thin or ZFS). Move ONLY the CP VM disk there with
  `qm disk move <vmid> scsi0 <new-storage>` (online), one CP at a time, with etcd 3/3 between moves.
- PLP drives acknowledge flush/FUA from protected DRAM, so fdatasync becomes sub-millisecond and
  immune to QLC folding/GC. Runners and everything else stay on the QLC drive.
- This is the "real fix" the 09-29 notes already named, and the only option that removes
  device-internal stalls.
- Cost: hardware (~3 drives). Risk: physical install = one host reboot each (rolling, etcd 3/3
  between).

### 4. Keep host-level contention bounded until 3 lands: cgroup v2 `io.latency` for CP VM scopes (optional, measured)

- Set `io.latency` (target e.g. 10 ms) on the CP VMs' systemd scopes on each host. A systemd
  drop-in or hookscript re-applies it after VM restarts. The kernel then throttles sibling cgroups
  (runners) only while the CP misses its latency target.
- This addresses node1-style contention (proven correlation with runner writes) without a static
  cap. It does nothing for firmware stalls.
- Measure fsync p99 and runner throughput for 48 h, and remove it if CI throughput drops measurably.

### 5. Housekeeping

- Schedule a recurring registry-LXC trim on node1 (host `fstrim.timer` does not reach LXC mounts;
  Zot GC refills): weekly, chunked, CI-quiet hours.
- Leave the `ControlPlaneRestartWave` severity as is; it detected a real infrastructure event.

## Critical files

- `kubernetes/infra/machine-config/controlplane.yaml.tftpl`: etcd extraArgs + rationale comment
  (step 1).
- The HelmRelease values for cnpg-operator (installed by cchifor/platform, so cross-repo), kyverno,
  csi-driver-nfs and snapshot-controller: leader-election flags (step 2).
- Proxmox host config (storage.cfg, CP VM disk placement). Out-of-band today; document it in
  `docs/runbooks/ai-host-setup.md` (step 3).
- A host hookscript/systemd drop-in for io.latency plus the runbook entry (step 4).
- A host timer for the chunked LXC trim plus the runbook entry (step 5).

## Verification

- Step 1:
  - `talosctl -n <cp> get etcdspec -o yaml` shows 500/5000 on all three;
  - `etcd_server_leader_changes_seen_total` increase/24h drops to ≤1 over 3 busy days;
  - stalls up to 5 s appear as `slow fdatasync` log lines without a term change;
  - `ControlPlaneRestartWave` does not fire.
- Step 2: the controllers' restart counters stay flat across the next leader change (if any).
- Step 3:
  - `etcd_disk_wal_fsync_duration_seconds` p99 <10 ms on all members;
  - zero WAL fsyncs >1 s per day;
  - host IO PSI unchanged for runners.
- Step 4: cp1 fsync p99 during busy CI hours vs the 10-06 baseline; runner write throughput and job
  durations vs the 10-06 baseline.

<!-- codex-review-status: pending -->
