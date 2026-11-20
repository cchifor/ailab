# ADR 0043: Talos control-plane disks on the QNAP (`qnap-nfs`)

**Status:** ACCEPTED (2026-11-20), owner-approved, and live.
- talos-cp1 was moved first, 07:27-07:37Z.
- talos-cp2 (14:07-14:16Z) and talos-cp3 (14:17-14:26Z) followed on the owner's "go ahead with cp2 and 3 on qnap now".
- The owner accepted the durability risks on #1753: comment 132957 (the open questions) and comment 133410 (the
  decision).

**Implementation state** (the one place to update as it changes):
- tofu records the datastore: **merged** (#1898, `d93e3046`, 2026-11-20).
- The boot-order guard: **PENDING, PR #1899, not merged.** Until it is merged and rolled out on a host, nothing holds
  that host's CP start for the NAS: after any host boot, check that the CP is running (`qm status <vmid>`).

**Supersedes:** the README's "Talos OS + etcd: NOT on the NAS ... (etcd belongs on local disk)".

**Related:**
- ADR 0042: the shared local NVMe and the etcd stalls.
- ADR 0004: Thunderbolt-net to the QNAP on Linux, the primary storage-path risk.
- ADR 0010: backup and DR.
- ADR 0016: the availability model.
- #1898: tofu records the datastore (merged).
- #1899: the boot-order guard (see "Implementation state").

## Context

Each Proxmox host has ONE consumer NVMe (Kingston OM8TAP42048K1 2 TB, QLC, DRAM-less, no power-loss protection).
- Wear on 2026-11-20: 91% / 87% / 56% (node1/2/3).
- That drive carried the host's Talos control-plane VM, i.e. its etcd member, next to the CI runners, dev workers, an
  agent node and the LLM LXCs.

etcd stalled on it, at low load as well as high:
- **2026-11-17:** WAL fsync p99 was 1.2 s on cp1 and 2.9 s on cp2 at the same time. Both followers were slow, so the
  cluster had no quorum, and writes stalled for ~10 s.
- **2026-11-18 03:53Z:** the leader, cp3, logged eight `slow fdatasync` of ~1.5 s. Reads stalled 8-10 s on every
  apiserver, and the Relay agent lost its lease. node3's drive went from 0.1 to 52 ms average write latency at only
  500-900 writes/s. node3 is the healthiest, coolest drive of the three, so the device itself stalls.
- **ADR 0042** (cache=unsafe on the rebuildable disks, host writeback bounds, runner caps) cut the noise from the
  other guests. It cannot fix a device that stalls on its own.
- **An iocost trial** (blk-iocost giving etcd the drive first, #1876, 2026-11-19): the node3 canary coincided with a
  cp3 VM freeze and was rolled back. node1 still runs it.
- **The durable fix**, a datacenter NVMe with power-loss protection for etcd, is the owner's hardware call, and it is
  not now.

**Measured 2026-11-20, 4 KiB `O_DSYNC` writes:** 0.08-0.19 ms on the QNAP over NFS, against 1.8-10.7 ms on the local
NVMe.

**The QNAP side** (read-only record, #1753 comment 132957):
- The pool `zpool1` is raidz1 of 5 x Kingston KC3000 2 TB: consumer NVMe, volatile write cache, no power-loss
  protection.
- Flushes are honoured end to end: `sync=standard`, ZFS flush-disable parameters 0/0.
- QuTS's `_vfs_zfs_qnap_zil_acc_enable` ("ZIL acceleration") is on, and its semantics are not documented by QNAP.

**The path.**
- The hosts mount the `/pve-nfs` export of `10.55.0.254` at `/mnt/pve/qnap-nfs`: NFSv4.0, `hard,timeo=600,retrans=2`.
- node1 and node2 reach it over Thunderbolt-net (the `tbtbr0` bridge on the NAS).
- node3 reaches it over its Thunderbolt-to-10G adapter, a direct link.

## Decision

1. **All three CP disks (`scsi0` of VMs 4001-4003) are raw files on `qnap-nfs`**, at
   `images/<vmid>/vm-<vmid>-disk-0.raw`.
   - **How they moved:** live, one CP at a time, with
     `qm disk move <vmid> scsi0 qnap-nfs --format raw --delete 1 --bwlimit 153600`. QEMU mirrors the disk while the VM
     runs, about 9-10 min per 80 GiB, and the local LV is deleted after the copy.
   - **Not moved:** the cloud-init drive (`ide2`) stays on `local-lvm`, as `cloudinit_datastore = "local-lvm"`
     records (#1898). PVE regenerates that drive's image on every start that is not a migration (`vm_start_nolock`
     calls `apply_cloudinit_config`; read in PVE 9.2.2's `QemuServer.pm`). So a CP **start** still needs its host's
     `local-lvm` writable: a full or read-only thin pool can keep a CP from starting, although etcd no longer lives
     there.
   - **Afterwards:** etcd 3/3 in sync with leader cp1, and WAL fsync p99 0.002-0.013 s.
2. **tofu records the datastore** (#1898, merged): `vm_datastore = "qnap-nfs"` in `kubernetes/infra`, and a separate
   `cloudinit_datastore = "local-lvm"`. The read-only plan before the merge showed no resource changes.
   - The rule: move a disk with `qm disk move` first, then make the variable match. Never change the variable to move
     a disk.
   - Why: with the variable out of step, bpg 0.123.1 plans an in-place `disk.datastore_id` update on all three CPs
     at once. That is a move nobody here has tested, and bpg issue #1256 reports a VM recreated instead.
3. **A boot-order guard** (#1899, `pve_base` tag `nfs_disk_gate`) is part of this decision. Its state is under
   "Implementation state" above.
   - **What it does:** a pve-guests drop-in waits, at most 600 s, until `qnap-nfs` is mounted, writable and serving
     the host's CP disk. Then pve-guests starts the onboot guests.
   - **Why it is needed:** pve-guests runs once per boot and never retries, and PVE refuses a VM whose storage is
     offline. The storage link comes up during boot, and after the 2026-10-30 power cut the NAS served NFS ~6 min
     after the hosts. Without the guard, a host reboot or a power cut could leave a CP stopped.
   - **After a timeout:** pve-guests starts everything anyway, and PVE refuses each CP whose share is still offline.
     A refused CP needs `qm start <vmid>` once the share is back.
   - #1899 carries the reasoning (`ansible/roles/pve_base/defaults/main.yml`), the rollout and the owner-gated reboot
     test (`docs/runbooks/node-maintenance.md`, "Boot-order guard").

## Accepted risks

The owner accepted these; the comment numbers are on #1753.

1. **The QNAP is a single point of failure for the control plane.** All three etcd members' disks share one NAS, one
   pool and one NFS service IP.
   - A NAS reboot, firmware update, pool fault or NFS hang stalls all three members at once: a full control-plane
     outage. Source: by construction. The three disks are files on that one share (`pvesm list qnap-nfs` and
     `qm config`, 2026-11-20). No such outage has been observed since the move.
   - Before this, a NAS outage took the stateful apps down: every database, Gitea and Prometheus are on its iSCSI
     LUNs. The control plane itself stayed up. Now it goes too.
   - The mount is `hard`, so CP disk I/O blocks rather than fails while the NAS is away, and resumes when it answers.
     How the Talos guests ride out a multi-minute stall (guest SCSI timeouts, the XFS on EPHEMERAL) has not been
     tested.
2. **The storage link (ADR 0004).**
   - node1/2's etcd I/O now rides Thunderbolt-net, which ADR 0004 names the primary risk: QNAP documents its T2E link
     for macOS and Windows only.
   - node3's rides a USB4-tunnelled 10G adapter that needs special kernel parameters to enumerate.
   - A link flap on one host stalls that host's member only, and quorum holds.
   - The NAS's NFS service IP lives on its `tbtbr0` bridge. After the 2026-10-30 cold boot, the NAS rebuilt that
     bridge ~4.5 min into its boot.
3. **Power.** The QNAP is on a UPS with a clean-shutdown path (owner, 2026-11-20 ~11:30Z). This ADR assumes nothing
   about the hosts' power.
4. **Durability of acknowledged writes on the pool.**
   - Flushes propagate to the pool drives.
   - The drives have volatile caches and no power-loss protection, and `zil_acc`'s behaviour is QNAP's, undocumented.
   - The owner confirms `zil_acc` is flush-safe and explicitly accepts the residual power-loss risk of the consumer
     pool drives: comment 133410, options (a) and (d) of comment 132957.
   - No power-loss test was run.
5. **Shared NAS load: a latency event, not an outage.** etcd now shares the pool with the iSCSI LUNs, the `nfs-csi`
   volumes, the LLM model reads and the image imports.
   - A scrub, a resilver or a large copy can raise CP disk latency. Nothing shows that one stalls etcd beyond its
     tolerances: not measured, because no scrub has run on the pool since the move.
   - The monthly scrub is a NAS cron job at 05:00 on the 1st, and its first run took 3m43s for 985 G allocated
     (`qnap-storage-setup.md` §4).
   - So a scrub is not an owner-planned outage. Run scrubs and big copies off-hours and watch etcd's alerts:
     `etcdHighFsyncDurations` (WAL fsync p99 > 0.5 s warning, > 1 s critical, for 10 min), `etcdHighCommitDurations`
     (backend commit p99 > 0.25 s for 10 min) and `etcdHighNumberOfLeaderChanges` (4 or more in 15 min). The
     thresholds were read from the live PrometheusRules on 2026-11-20.
   - If one fires during a scrub, stop the scrub: `zpool scrub -s zpool1` on the NAS.
6. **The backups share the failure domain.** talos-backup's etcd snapshots land on versitygw, which runs on a USB-NVMe
   in the same NAS chassis. The nightly rclone-crypt copy to Google Drive (04:00) is the only copy outside the NAS.

## Consequences

- **QNAP maintenance that takes the share away is control-plane maintenance.** That means:
  - a NAS reboot, including a firmware update, which reboots it;
  - pool work that takes the pool or the export offline;
  - work on the NAS's Thunderbolt or network, where the NFS service IP lives.

  Plan these as a **full-cluster outage**, with the owner (risk 1). A scrub or a resilver is not on this list: it is a
  latency event (risk 5).
  - Take an etcd snapshot first:
    `_out/talosctl-1142.exe -n <cp-ip> -e <cp-ip> etcd snapshot _out/etcd-snapshots/<name>.db`.
  - **Open item:** a rehearsed procedure for a planned NAS outage, stopping the CPs before and starting them after the
    share is mounted on every host, does not exist yet.
- **Host maintenance** is unchanged: one CP at a time, etcd 3/3 between hosts. After a host boot, check that its CP
  is running. Where the guard is live (see "Implementation state"), its journal line says how long the start waited.
- **tofu on `kubernetes/infra`** plans clean again since #1898 merged: no resource changes, apart from two stale
  outputs that predate it. A `disk_gb` resize now grows a raw file on the NAS.
- **The local NVMe** no longer carries etcd. The CPs wrote little (<= 0.07 TB/day each), so the gain is latency
  isolation, not wear.

## Rollback

1. Move each disk back live, one CP at a time: `qm disk move <vmid> scsi0 local-lvm --delete 1`.
   - It is the same QEMU mirror in reverse, and the VM stays up. Add `--bwlimit` as for the move.
   - Each host's thin pool needs ~80 GiB free, the disks' size since the 2026-09-27 resize (`pve/data`; "Thin pool
     fill" in `node-maintenance.md`).
   - Check etcd 3/3 in sync between CPs.
2. Set `vm_datastore = "local-lvm"` and confirm `tofu plan` shows no resource changes.
3. If the guard is installed, it can stay: with nothing onboot on `qnap-nfs` it passes at once.

## Alternatives considered

- **Keep etcd local and keep mitigating** (ADR 0042, iocost): this reduced the stalls but did not remove them, because
  the device stalls by itself. The iocost canary on node3 coincided with a cp3 freeze.
- **A datacenter NVMe with power-loss protection for etcd:** the durable fix, deferred by the owner on cost.
