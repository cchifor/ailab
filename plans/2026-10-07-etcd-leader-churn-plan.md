# etcd leader churn on shared consumer NVMe: analysis and remediation

## Codex Review

- The WAL stalls and shared-storage topology strongly justify isolating control-plane storage; preserving discard and retaining the infrastructure alert are sensible.
- A 5000 ms election timeout does **not** guarantee surviving a 5 s leader stall. Longer timeouts can prolong API outages, and longer controller leases suppress symptoms without restoring service.
- Step 2 incorrectly treats every controller as a singleton: snapshot-controller has two replicas. CNPG and Kyverno require cross-repo work, and the pinned NFS chart does not expose the proposed lease settings.
- Missing safeguards include runtime flag verification, explicit rollback, stronger maintenance gates, hardware compatibility checks, and reconciliation of disk placement with OpenTofu.
- **Best solution:** prioritize step 3 and start compatibility checks/procurement now; bring step 5 and temporary CI load reduction forward. Use supported step 2 changes selectively if needed, consider step 1 only as a measured bridge, and skip step 4 unless a small experiment demonstrates worthwhile protection.

## Context

**Symptom.** At 2026-10-07 05:57Z, `ControlPlaneRestartWave` (critical) fired: 9 control-plane
controllers restarted once each within 10 min:

- cnpg-operator, kyverno admission/background/cleanup/reports
- csi-nfs-controller (3 sidecars), snapshot-controller

All had lost their client-go leader-election lease ("context deadline exceeded" / "leader election
lost") at 05:56:29-30Z. They recovered on their own. The underlying problem is chronic:

- `etcd_server_leader_changes_seen_total` rose by 10 in the 12 h to 06:09Z.
<!-- codex: This counter is per member and counts observed leader changes, not uniquely identified cluster elections; retain the per-member query and corroborate terms/logs rather than summing the same transition across three members. -->
- Earlier 12 h windows: 4, 2, 4, 2, 0, 6.
- The cnpg-operator pod (created 2026-09-29) has restarted 50 times; the kyverno controllers 45-54
  times; csi-nfs-controller 111. Each election produces a restart round.
<!-- codex: “Each election” is too strong: a brief election need not exhaust renewal retries, and an API/storage stall can cause lease loss without an election. Correlate individual restart reasons and renewal failures with the outage timeline. -->

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
<!-- codex: This matches the documented behavior of this Talos deployment; retain the controlled reboot requirement and version-matched client rather than substituting an unsupported service restart. Resolve `_out` relative to `kubernetes/infra` and verify the running server versions before the roll. -->

## Evidence

<!-- codex: The incident measurements below are reported evidence, not independently reproduced by this repository review; attach the relevant log excerpts and PromQL queries with UTC windows, member labels, and scrape resolution so the diagnosis and baseline are reproducible. -->

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
<!-- codex: One-minute throughput averages cannot exclude short bursts, queueing, flush serialization, or delayed effects of earlier writes; low completed throughput can itself result from a stalled device. IO PSI “some” measures time with at least one task stalled, not a continuous whole-host freeze. -->
  - no kernel/NVMe errors, SMART media_errors 0, error-log empty;
  - NVMe Thermal Management T1/T2 transition counts 0 on node3 (node2's drive shows T1 count 2,
    387 s total);
  - controller temperature sensors run 77-90 °C on all three hosts.
<!-- codex: Zero T1/T2 transitions do not exclude every firmware thermal-throttling mechanism; identify the sensors and vendor thresholds, then inspect cooling and airflow now. These temperatures warrant investigation before adding another heat source to each enclosure. -->
- Heavy CI writes on node3 only started at 05:57:30 (3.8-7.5k write IOPS), after the stall.
- Reading: a firmware-internal write stall (QLC SLC-cache fold / GC, possibly thermal) on the
  drive, not host-level contention.
<!-- codex: Internal GC/cache folding is plausible, but the evidence does not isolate it from host scheduling, memory pressure, device-mapper, or QEMU delays. Describe shared-storage tail latency as established and the precise mechanism as a hypothesis; isolation remains justified without proving that mechanism. -->

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
<!-- codex: Pre-vote avoids raising the term without prospective quorum support, while check-quorum's recent-leader protection helps reject disruptive votes; a lone stalled follower normally cannot displace a healthy leader/majority. It still reduces redundancy, and stalls on both followers can prevent commits or make the leader step down; see the [Raft implementation](https://github.com/etcd-io/raft/blob/v3.6.0/raft.go). -->

**3. cp1 / ai-node1 contention was analysed and partly fixed on 2026-10-06.**

- Over 24 h at 1-min resolution, the 124 minutes with cp1 fsync p99 >500 ms had node1-runner writes
  of median 46 MB/s, against 12 MB/s in good minutes.
- Discard volume did not separate bad minutes from good ones.
- Per-runner 2-min write max was 169 MB/s, p99 ~92-116.
- What separates node1 from node2 (similar CI writes) is thin-pool fill: 72% vs 51%; QLC slows as
  it fills. Avg NVMe write latency was node1 11.6, node2 4.1, node3 1.0 ms; host IO PSI 22-30% vs <2%.
<!-- codex: Thin-pool allocation is not the SSD's physical NAND occupancy or spare-space budget, and this cross-host comparison does not establish fill as the sole cause. Verify discard pass-through, thin-pool data and metadata usage, and other host workloads before attributing the difference entirely to QLC fullness. -->
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
<!-- codex: A two-minute maximum cannot prove a subsecond burst cap would never engage; measure at the limiter's timescale and consider aggregate traffic from all runners. The previous throughput regression supports caution, not this categorical conclusion. -->
  - A cap low enough to matter (~30-40 MB/s) trades heavy CI throughput while the queue is already
    215 deep.
- Removing runner `discard`: runner disks are already 74-79% allocated WITH online discard, so pool
  fill (the harmful variable) would rise.
- Neither addresses device-internal stalls like today's, which happened at light load.
<!-- codex: Reduced writes can lessen later GC/cache-folding pressure even when a stall occurs during low foreground traffic. A missing immediate option is temporarily reducing concurrent disk-heavy CI jobs or pausing admission during maintenance; accept a bounded queue penalty to protect quorum instead of rejecting all load reduction. -->

**5. Hardware facts.**

- Each host (dmidecode type 9) shows `M.2 Socket 3: Available`, i.e. a free second M.2 slot.
<!-- codex: “Socket 3” identifies an M.2 socket class, not a third slot, and SMBIOS “Available” is not proof of an accessible second connector. Verify the board/manual and physical slot, NVMe wiring, supported length, component clearance, power, and cooling before purchasing. -->
- Host IO scheduler `none`; cgroup v2 controllers include `io`, so `io.latency` and `io.max` work but
  `io.weight` needs bfq/iocost.
<!-- codex: `none` does not inherently prevent io.latency or io.max, but an available io controller does not prove CONFIG_BLK_CGROUP_IOLATENCY or usable latency controls on this kernel. Check the actual interface files, ancestor controller enablement, and QEMU I/O attribution through LVM-thin; scheduler weights alone are ineffective with none and no active iocost policy. -->
- 124 GB RAM per host, 84-98 GB used.
- Drive wear (SMART percentage_used) 57/54/34%, ~3.4-5.4 TB/day writes per node (2026-10-03),
  projecting ~100% wear around early 2027 on node1/node2.
<!-- codex: SMART percentage_used estimates consumed endurance, not a predictable failure date; extrapolate from measured wear trends rather than host bytes alone. Plan replacement of the original host/runner drives separately, since adding a CP SSD leaves host boot and other dependencies on those drives. -->

## Approach

Recommended, in this order. Each step is independent and reversible.
<!-- codex: The steps interact: increasing election time can lengthen lease-renewal outages, and both reboots and disk copying require reduced contention and healthy survivors. Start step 3 now, bring step 5 and maintenance load shedding forward, use step 2 selectively before extending outage tolerance in step 1, and leave step 4 as an optional experiment rather than a prerequisite. -->

### 1. Ride out stalls: etcd heartbeat/election 250/2500 → 500/5000 (this week)

- Edit `heartbeat-interval: "500"` and `election-timeout: "5000"` in
  `kubernetes/infra/machine-config/controlplane.yaml.tftpl`. Keep the 10× ratio; rewrite the
  rationale comment with this plan's data.
<!-- codex: An exact 10× heartbeat/election ratio is not required; retaining heartbeat 250 ms while trialling election 5000 ms avoids unnecessarily slowing heartbeat cadence. Treat this as a temporary experiment based on outage duration and acceptable failover latency, consistent with [etcd timing guidance](https://etcd.io/docs/v3.5/tuning/). -->
- `just plan` / apply config to all three CPs (`talosctl apply-config`, no reboot).
<!-- codex: Review the generated configuration and full OpenTofu plan for unrelated VM changes, and explicitly use a no-reboot apply mode when applying manually. `talos.tf` has no rolling-health orchestration and its configuration resources depend on all CP VMs, so an unrestricted apply or resource target must not substitute for a reviewed maintenance sequence. -->
- Roll one controlled reboot per CP: `talosctl shutdown` + `qm start`, never `qm shutdown`.
<!-- codex: Before the first reboot, verify a recent usable etcd snapshot outside these hosts, save current configurations, and follow node-maintenance.md for drain/PDB, CNPG replication, volume attachments, and capacity gates. These CPs also run application workloads, so etcd health alone does not make a second drain safe. -->
  - Order: followers first, the current leader last (or `talosctl etcd forfeit-leadership` before
    its reboot).
<!-- codex: Select the first follower by survivor health, not role alone: if cp1 remains the worst member, rebooting cp2 could leave a fragile cp1/cp3 quorum. Recheck leadership before every operation and confirm a successful transfer to a caught-up, healthy member before stopping the leader; mixed timeout settings during the roll still permit old-timeout elections. -->
  - Between reboots, `talosctl etcd status` must show 3/3 in sync and the apiserver must be healthy.
<!-- codex: Require all three members reachable, one agreed leader/term, applied-index lag recovered, no etcd alarms, and repeated successful API writes plus readiness checks over a defined observation window. Abort on renewed stalls or failed gates and recover the current node before touching another. -->
  - Window: a quiet CI period (night), because a stall on one of the two remaining members during a
    reboot loses quorum for the stall's length.
<!-- codex: A quiet clock period is insufficient given the overnight stalls; actively drain or pause heavy CI admission across all three hosts and avoid concurrent trims, backups, or migrations. Loss of durable quorum progress can outlast the stall because of election, catch-up, and retry delays, and longer timeouts cannot restore a missing majority. -->
- Effect: followers wait 5-10 s (randomized) before campaigning, so a ≤5 s leader fsync stall (all of
  today's observed stalls) no longer causes an election.
<!-- codex: This guarantee is false: at 500/5000 the randomized threshold is 10–19 ticks, nominally 5–9.5 s since the last accepted leader message, not since fdatasync began. The measured 5.0307 s stall already exceeds the minimum, and elapsed heartbeat time, scheduling, or successive stalls can still allow pre-vote and election; see the [timeout calculation](https://github.com/etcd-io/raft/blob/v3.6.0/raft.go#L1934-L1943). -->
- Cost: a genuinely dead leader is replaced in 5-10 s instead of 2.5-5 s. The apiserver is degraded
  for that long, which it already is during every stall today.
<!-- codex: Those ranges approximate campaign initiation, not guaranteed replacement or API recovery; voting, persistence, and backlog add time. Step 1 is ineffective at fixing slow commits and can be harmful when it delays replacing a stalled leader that two healthy followers could otherwise replace sooner. -->
<!-- codex: Define rollback before rollout: restore 250/2500 in the source configuration and perform the same guarded one-member-at-a-time reboot sequence if API outage duration worsens. Reverting Git alone does not change running flags, and a separate timeout reboot campaign may be unnecessary if hardware installation is imminent. -->

### 2. Make the restart waves non-events: longer leader-election leases for single-replica controllers (this week)

- cnpg-operator, kyverno controllers, csi-nfs-controller and snapshot-controller run as single
  replicas. Their lease is a liveness fence, not failover, so they gain nothing from the client-go
  default `LeaseDuration 15s / RenewDeadline 10s`.
<!-- codex: snapshot-controller is explicitly replicas: 2 in kubernetes/apps/infrastructure/storage/snapshot-controller/setup-snapshot-controller.yaml, so this step delays actual standby takeover. Verify live replicas and rollout strategy for every other component; even a nominal singleton can overlap with its replacement during a rollout. -->
<!-- codex: A coordination.k8s.io Lease is persisted API data, not an etcd TTL lease revoked by a Raft election or expired automatically by the apiserver; clients interpret it and stop leading when renewal fails. RenewDeadline governs the renewal retry budget, LeaseDuration governs competitors' takeover eligibility, and [client-go explicitly does not guarantee fencing](https://github.com/kubernetes/client-go/blob/v0.34.0/tools/leaderelection/leaderelection.go). -->
- Raise to ~60s / 40s / 5s via each component's flags or env, where supported. Then a 5-15 s API
  stall no longer makes them exit.
<!-- codex: Inventory each deployed version's actual defaults and supported arguments rather than assuming universal client-go defaults; snapshot-controller v8.6.0 already uses a 5 s retry period. Validate LeaseDuration > RenewDeadline > jitter-adjusted RetryPeriod, and check request timeouts and liveness probes, which can still cause exits despite a longer renewal budget. -->
- Cost: after a real crash, the replacement pod waits up to the remaining lease (≤60 s) before it
  acts.
<!-- codex: This is only the lease-related delay: observation timing, retry jitter, scheduling, API recovery, and startup add time, while a successful graceful release can shorten it. Longer leases can harm CNPG failover and other controller recovery objectives; choose per-component budgets rather than a blanket 60/40/5 policy. -->
- Scope: only components whose HelmRelease/manifest exposes the knobs; no forks.
<!-- codex: The pinned csi-driver-nfs 4.13.2 chart hard-codes all three sidecars' argument lists and exposes no lease-timing values, so inventing Helm values is ineffective; see its [controller template](https://github.com/kubernetes-csi/csi-driver-nfs/blob/master/charts/v4.13.2/csi-driver-nfs/templates/csi-nfs-controller.yaml). Skip it under this scope, or explicitly justify a small Flux post-render patch targeting named containers, which does not require a chart fork. -->
<!-- codex: snapshot-controller is a plain Deployment whose supported flags are --leader-election-lease-duration, --leader-election-renew-deadline, and --leader-election-retry-period; verify rendered arguments and both replicas if changed. The [v8.6.0 implementation](https://github.com/kubernetes-csi/external-snapshotter/blob/v8.6.0/cmd/snapshot-controller/main.go) exposes all three. -->

### 3. Remove the cause: a dedicated PLP SSD per host for the CP VM disk (order now, install within weeks)

- Install a small enterprise NVMe with power-loss protection (TLC, M.2 2280, e.g. 480-960 GB) in each
  host's free M.2 Socket 3.
<!-- codex: This is the highest-priority durable remediation; qualify an exact SKU for full data-in-flight PLP, sustained synchronous-write tail latency, endurance, fit, and thermals before ordering all three. If compatible PLP storage cannot fit, use suitable separate hardware for the CPs rather than treating another consumer SSD as equivalent. -->
- Create a separate PVE storage on it (LVM-thin or ZFS). Move ONLY the CP VM disk there with
  `qm disk move <vmid> scsi0 <new-storage>` (online), one CP at a time, with etcd 3/3 between moves.
<!-- codex: Prefer the familiar LVM-thin backend unless ZFS has a separate requirement; adding a new storage stack increases tuning and operational scope, and a single-disk ZFS pool adds no redundancy. Identify the new device by stable identity and budget thin-pool data and metadata headroom before creating storage. -->
<!-- codex: Online movement of an active raw LVM-thin VM disk is supported through QEMU block mirroring; LVM-thin does not inherently require shutting down the guest, as [Proxmox staff explain](https://forum.proxmox.com/threads/vm-disk-moved-from-lvm-thin-to-lvm-thin-no-longer-thin.48308/). It still reads the suspect source and mirrors ongoing writes, so move a healthy follower first under reduced load, set a measured bandwidth limit, and monitor quorum throughout rather than only between moves. -->
<!-- codex: Check the installed PVE version's snapshot/clone restrictions and allow for copying or allocating the full 80 GB logical disk rather than only filesystem-used space. Confirm the task completed, scsi0 points at the new volume, and boot settings remain correct before considering the move successful. -->
<!-- codex: The [qm CLI](https://github.com/proxmox/pve-docs/blob/master/generated/qm.1-synopsis.adoc) retains the source as an unused disk by default; retain it until validation, then reclaim it deliberately. It becomes stale after cutover, so rollback means moving the current disk back or following documented member recovery, not blindly booting the old etcd disk. -->
- PLP drives acknowledge flush/FUA from protected DRAM, so fdatasync becomes sub-millisecond and
  immune to QLC folding/GC. Runners and everything else stay on the QLC drive.
<!-- codex: PLP can improve durable-write latency, but neither DRAM-only flush completion nor sub-millisecond latency is guaranteed by the PLP label; implementations differ and GC, thermal, firmware, and host stalls remain possible. Require full in-flight protection and measure the complete guest-to-device flush path without disabling flushes or using unsafe caching; [Kingston describes the protection mechanism](https://www.kingston.com/en/blog/servers-and-data-centers/ssd-power-loss-protection). -->
<!-- codex: “Everything else” is inaccurate: allowSchedulingOnControlPlanes is true, so moving the whole CP disk also moves container images, logs, and application-local writes onto the new drive. A missing complementary option is moving the heaviest non-control-plane disk writers to existing workers where capacity permits, while retaining the simpler whole-VM disk migration. -->
- This is the "real fix" the 09-29 notes already named, and the only option that removes
  device-internal stalls.
<!-- codex: Dedicated storage removes dependence on this shared QLC device, not every possible internal stall; replacing the existing drive or relocating the CPs to suitable hosts are alternatives. The decisive benefit is removing CI/device contention from the CP storage path, not an absolute PLP latency guarantee. -->
- Cost: hardware (~3 drives). Risk: physical install = one host reboot each (rolling, etcd 3/3
  between).
<!-- codex: Installation requires a powered-off host, so drain its Talos guests and stop its other VMs/LXCs using node-maintenance.md, including the documented D-state shutdown risk. Gate each host on both Proxmox and etcd quorum plus workload recovery, and confirm boot order and device identity after adding the SSD. -->

### 4. Keep host-level contention bounded until 3 lands: cgroup v2 `io.latency` for CP VM scopes (optional, measured)

- Set `io.latency` (target e.g. 10 ms) on the CP VMs' systemd scopes on each host. A systemd
  drop-in or hookscript re-applies it after VM restarts. The kernel then throttles sibling cgroups
  (runners) only while the CP misses its latency target.
<!-- codex: First prove that QEMU and its I/O workers are charged to the expected scope and that the physical backing device and competing workloads participate at the relevant peer level; separate per-VM thin LVs do not establish shared-device protection. io.latency protects block-I/O latency rather than directly enforcing an etcd fdatasync bound, and its hierarchy matters; see the [kernel documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html#io-latency). -->
<!-- codex: The protected scope includes all CP VM I/O, so application bulk writes can trigger runner throttling too, and an unattainable 10 ms target can heavily penalize peers without curing the stall. Prove both attribution and useful throttling before writing persistence hooks for transient PVE scopes. -->
- This addresses node1-style contention (proven correlation with runner writes) without a static
  cap. It does nothing for firmware stalls.
- Measure fsync p99 and runner throughput for 48 h, and remove it if CI throughput drops measurably.
<!-- codex: Some CI slowdown is the mechanism of protection, so reject the experiment based on an explicit latency benefit versus job-duration/queue budget, not any measurable throughput drop. Step 4 is over-engineering unless a bounded canary beats simpler load reduction; rollback must clear the active target and disable its reapplication hook. -->

### 5. Housekeeping

- Schedule a recurring registry-LXC trim on node1 (host `fstrim.timer` does not reach LXC mounts;
  Zot GC refills): weekly, chunked, CI-quiet hours.
<!-- codex: Bring this forward because reclaim already helped, but GC frees filesystem blocks while subsequent writes refill them; discard communicates those freed extents to lower layers. Verify the mp0 mount and discard pass-through, bound each trim batch, avoid reboot/migration windows, and monitor WAL latency because trim itself can produce stalls. -->
- Leave the `ControlPlaneRestartWave` severity as is; it detected a real infrastructure event.
<!-- codex: Retain this alert, but after extending leases it becomes a less sensitive storage-outage signal. Ensure independent alerts cover WAL/backend latency, absent leadership, failed proposals, and API availability so fewer restarts cannot hide continuing outages. -->

## Critical files

- `kubernetes/infra/machine-config/controlplane.yaml.tftpl`: etcd extraArgs + rationale comment
  (step 1).
- The HelmRelease values for cnpg-operator (installed by cchifor/platform, so cross-repo), kyverno,
  csi-driver-nfs and snapshot-controller: leader-election flags (step 2).
<!-- codex: Track CNPG changes and rollout in cchifor/platform explicitly; Kyverno is also delivered through platform-kyverno, as this repo's storage-policies Flux dependency documents. No local Kyverno HelmRelease was found, and snapshot-controller is a local plain Deployment, so identify each actual owner/version before promising completion of step 2. -->
- Proxmox host config (storage.cfg, CP VM disk placement). Out-of-band today; document it in
  `docs/runbooks/ai-host-setup.md` (step 3).
<!-- codex: CP disk placement is already declared in kubernetes/infra/vms.tf through var.vm_datastore, and disk placement is not ignored by lifecycle; kubernetes/infra/main.tf does not exist in this worktree. Reconcile the declaration and refreshed state after each move using staged per-CP storage selection if needed, and require a final no-drift plan so later applies cannot undo the migration. -->
- A host hookscript/systemd drop-in for io.latency plus the runbook entry (step 4).
- A host timer for the chunked LXC trim plus the runbook entry (step 5).

## Verification

<!-- codex: Establish per-member WAL/backend latency, peer RTT, leader transitions, API write latency/errors, controller availability, and comparable CI workload baselines before changing anything. Record each change separately and define abort/rollback thresholds so simultaneous lease, timing, and storage changes do not obscure which intervention worked. -->

- Step 1:
  - `talosctl -n <cp> get etcdspec -o yaml` shows 500/5000 on all three;
<!-- codex: EtcdSpec proves desired configuration only, precisely because this deployment does not restart etcd when it changes. Verify the chosen values in each running process's arguments or post-boot etcd startup logs, together with process start time, after every reboot. -->
  - `etcd_server_leader_changes_seen_total` increase/24h drops to ≤1 over 3 busy days;
  - stalls up to 5 s appear as `slow fdatasync` log lines without a term change;
  - `ControlPlaneRestartWave` does not fire.
<!-- codex: Fewer elections and alerts are insufficient: require API outage duration, successful Lease renewals, and controller work completion to improve or remain within explicit budgets. Separate planned leadership transfers/reboots from spontaneous churn, and do not induce production storage stalls merely to validate the incorrect five-second guarantee. -->
- Step 2: the controllers' restart counters stay flat across the next leader change (if any).
<!-- codex: Also verify rendered/live arguments, advancing Lease renewTime, successful reconciliation, and acceptable takeover after a controlled controller restart once storage is stable; a live but stalled controller is not success. Track pod replacements as well as container restarts, and retain the original per-component settings for GitOps rollback. -->
- Step 3:
  - `etcd_disk_wal_fsync_duration_seconds` p99 <10 ms on all members;
  - zero WAL fsyncs >1 s per day;
<!-- codex: Validate under several representative busy CI days after migration, including backend commit latency and absence of new API outages; p99 alone hides rare multi-second stalls. Compute tail-event counts per member from histogram count minus the appropriate cumulative bucket, using an actual exported boundary such as 1.024 s, and account for counter resets and missing scrapes. -->
  - host IO PSI unchanged for runners.
<!-- codex: Host-wide PSI mixes workloads and may improve when CP I/O moves, so “unchanged” is not a useful acceptance criterion. Check runner job duration/throughput separately and verify actual CP disk placement, safe cache/flush settings, SSD temperature, backup coverage, and a clean OpenTofu plan. -->
- Step 4: cp1 fsync p99 during busy CI hours vs the 10-06 baseline; runner write throughput and job
  durations vs the 10-06 baseline.
<!-- codex: Use matched workload windows and inspect the kernel's available latency/throttling statistics to prove the intended scopes are affected; the 10-06 baseline predates other changes and is confounded. For step 5, add successful timer execution, reclaimed thin-pool space, and absence of trim-correlated latency spikes. -->

<!-- codex-review-status: complete -->