# Runbook — QNAP QuTS hero storage setup

QuTS hero h5.2.9 on `ai-storage`. Most of this is scripted via `qcli` (persistent); exactly
**one** step needs the QNAP UI (the Thunderbolt bridge IP — qcli has no interface ID for the
system bridge). Nothing here destroys the existing **`zpool1`** RAID-Z1 pool (kept per ADR 0002).

## State captured at build time
- Pool **`zpool1`**: RAID-Z1, 5× 2 TB (Kingston KC3000, PCIe Gen3×2), ~6 TB usable, `dedup=off`. Kept.
- Shared folder **`pve-nfs`** created on it (ZFS dataset `zpool1/zfs18`), `compress=on`, thin, exported at **`/pve-nfs`** (NFS **v4.0**).
- NFS host access: `10.55.0.0/24` and `10.55.1.0/24` → `rw, no_root_squash`.

## 1. Scripted part (network + share) — `scripts/qnap-setup.sh`
```bash
bash scripts/qnap-setup.sh        # idempotent; uses qcli over SSH (.env creds)
```
This does, via `qcli`:
- `eth1` (10GbE → node3) → static **`10.55.1.254/24`**
- enable NFS (v3 + v4)
- create shared folder `pve-nfs` on `poolID=1` (thin, `compress=1`, `dedup=0`)
- NFS host access for `10.55.0.0/24` + `10.55.1.0/24` (`rw`, `no_root_squash`)

Equivalent raw qcli (for reference):
```
qcli -l user=<admin> pw=<pw> saveauthsid=yes
qcli_network -m interfaceID=eth1 IPType=STATIC IP=10.55.1.254 netmask=255.255.255.0 dns_type=manual
qcli_networkservice -n nfsServerEnabled=Enabled nfsServerEnabledV4=Enabled
qcli_sharedfolder -s sharename=pve-nfs poolID=1 comment=ProxmoxNFS guest=deny compress=1 dedup=0 type=1 size=5497558138880
qcli_sharedfolder -N sharename=pve-nfs Access=Enabled
qcli_sharedfolder -T sharename=pve-nfs HostIP=10.55.0.0/24 Permission=rw Squash=no_root_squash secure=1 sync=1 wdelay=0
qcli_sharedfolder -T sharename=pve-nfs HostIP=10.55.1.0/24 Permission=rw Squash=no_root_squash secure=1 sync=1 wdelay=0
```

## 2. Thunderbolt bridge service IP — persisted as code (no UI)
The two TB ports auto-bridge into the **"Thunderbolt Bridge (System Default)"** (`tbtbr0`), and
T2E activates automatically when a node's Thunderbolt interface comes up
(`/etc/init.d/thunderbolt_net.sh` adds the port to `tbtbr0`) — so no manual "enable T2E" step.

`qcli_network` can't target the system bridge (no interface ID), so `scripts/qnap-setup.sh`
installs an **idempotent reconciler** on the persistent DOM and a cron entry:
- `/etc/config/tb-storage-ip.sh` → `ip addr add 10.55.0.254/24 dev tbtbr0` (only if missing)
- `* * * * * /etc/config/tb-storage-ip.sh` in `/etc/config/crontab` (survives reboot)

The IP self-heals within ~1 min of any QNAP reboot — this is the **NFS service IP** the whole
cluster mounts.

*Alternative (manual, also persistent):* Control Panel → Network & Virtual Switch → Interfaces →
Thunderbolt → Static `10.55.0.254/255.255.255.0`, no gateway. If you set this, remove the cron
reconciler to avoid duplication.

## 3. Optional cleanup of default datasets
The factory left empty datasets (`ZFS1_DATA`, `ZFS530_DATA`, `Public`, `zfs1107`). They're harmless
(~51 MB). Remove via the UI (Control Panel → Shared Folders) if you want a tidy box — not required.

## Outputs consumed by the rest of the IaaC
| Value | Where |
|---|---|
| NFS server IP `10.55.0.254` | `tofu` `qnap_nfs_server`, `inventory` `storage_service_ip` |
| Export path `/pve-nfs` | `tofu` `qnap_nfs_export` |
| NFS version `vers=4.0` | `tofu` `qnap_nfs_options` |

## Validation
```bash
python scripts/node-ssh.py 192.168.0.2 'mount -t nfs -o vers=4.0 10.55.0.254:/pve-nfs /mnt/t && \
  dd if=/dev/zero of=/mnt/t/x bs=1M count=3072 conv=fdatasync; rm /mnt/t/x; umount /mnt/t'
# expect ~1.1 GB/s write over Thunderbolt
```

---

# Ongoing operation (added by the 2026-08-16 storage audit)

The build steps above leave a working NAS but an **unmaintained and unwatched** one. These four items
are what the audit added; all are on the box itself, so a factory reset or a firmware update that
resets config can silently undo them — re-check them after either.

## 4. Pool scrubbing — monthly

The pool had **never been scrubbed** in the 68 days since `zpool create` (`zpool status` read
`scan: none requested`; `zpool history` held only the create line). Note that `auto_data_scrubbing = 1`
*was* set in `uLinux.conf` and meant nothing — do not trust that flag as evidence a scrub runs.

On RAID-Z1 a scrub is the only thing that finds latent corruption before a second fault makes it
unrecoverable. Now scheduled in the persistent QNAP crontab:

```
0 5 1 * * /sbin/zpool scrub zpool1     # /etc/config/crontab, 05:00 on the 1st
```

Added idempotently and reloaded with `crontab /etc/config/crontab`. Original saved as
`/etc/config/crontab.bak.audit`. First run took **3m43s** on this pool (985 G allocated, all NVMe) and
repaired 0 with 0 errors — cheap enough that monthly is not worth debating.

```bash
python scripts/qnap-ssh.py "zpool status zpool1 | head -4"   # check scan: line
```

## 5. SNMP — the NAS's only Prometheus scrape

The NAS was the one major component with **no metrics scrape at all** (only a blackbox TCP probe to
2049/3260), and it has no SMTP either, so a failed drive in the RAID-Z1 would have alerted **nobody**.

SNMP is now enabled, and `kubernetes/apps/infrastructure/monitoring/qnap-snmp-exporter.yaml` scrapes
it. Security matters here: the factory `/etc/config/snmpd.conf` ships **`rwcommunity public`** — a
writable agent — so it was rewritten before the service was started:

- `rwcommunity` removed entirely; a single random read-only community
- source-restricted to `192.168.0.0/23` + `10.55.0.0/24` + `10.55.1.0/24`
- community lives in the SOPS secret `qnap-snmp-auth` (monitoring ns); original config saved as
  `/etc/config/snmpd.conf.bak.audit`

**If you ever re-enable SNMP from the QNAP UI, re-check that `rwcommunity` did not come back.**

Covered: per-disk SMART verdict + temperature, disk model/capacity, fan RPM, system + CPU temperature,
RAM. **Not covered** — the QNAP MIB does not expose ZFS pool state or capacity at all; the only
"volume" table it serves describes the 1 GB system volume. Pool DEGRADED is caught indirectly via the
per-disk SMART metric. Verify with:

```bash
python scripts/qnap-ssh.py 'getcfg SNMP "Service Enable"'    # TRUE
# then, in-cluster:
kubectl --context admin@ai -n monitoring logs deploy/qnap-snmp-exporter
```

## 6. Thin-LUN reclaim — UNRESOLVED, and why

The `qnap-iscsi` LUNs are thin zvols (`refreservation=none`). Nothing ever tells the array which
blocks the guest freed, so allocation only ratchets up: **103.5 GB allocated on the NAS against
55.4 GiB actually used** inside the filesystems (~48 GB stale). Worst case is the Prometheus LUN at
43.1 G of a 48 G volsize (90%) while holding 17.2 GiB — the source of the recurring `LUN has reached
the threshold (90%)` events (275 since June, across several LUNs, seen by no one until the audit).

**`mountOptions: [discard]` does not work on this backend.** It was implemented, measured, and
reverted. The Kubernetes half is fine — kubelet passes the option down faithfully:

```
volume_capability:<mount:<fs_type:"ext4" mount_flags:"discard" > >    # NodeStageVolume request
```

but `csi.trident.qnap.io` drops it. Its `NodeStageVolume` performs no mount at all (`target=` empty);
the real mount happens during publish and logs as:

```
mount_linux.MountDevice device=/dev/sdf mountpoint=/var/lib/kubelet/... options=
```

`options=` is empty. The driver mounts using its own `publish_context.mountOptions`, which is `""`,
and there is no knob for it in either the StorageClass parameters or the TridentBackendConfig — the
`qnap_config` storage pools expose only `serviceLevel` / `labels` / `features`, with no `defaults`
block. Confirm for yourself with:

```bash
kubectl --context admin@ai -n trident logs trident-node-linux-<pod> -c trident-main \
  | grep -E "MountDevice|mount_flags"
```

**What is left.** Reclaim on this backend needs a periodic `fstrim` against the kubelet mount paths,
which requires a **privileged pod with hostPath** — a real security-posture decision on a cluster
that runs `baseline` PSA and deliberately set `nodeAgent.disableHostPath` on Velero. That trade-off
is the operator's to make, so nothing was built. Weigh it against the actual impact: this is wasted
capacity and alert noise on a pool with 5.62 TB free, not a availability risk — ZFS is copy-on-write,
so a thin LUN that reaches its volsize keeps serving writes normally.

**What was done here:** set `compression=lz4` on all 15 LUN zvols (they were `compression=off` while
the parent share had it on). Applies to newly written blocks only.

## Open items this audit did NOT close

| Gap | Why it is still open |
|---|---|
| **No UPS** | Needs hardware. All 5 SSDs already report `unsafe_shutdowns: 2`. ZFS tolerates power loss; the exposure is the ext4 filesystems inside the iSCSI LUNs (Postgres, Gitea, Prometheus). |
| **No QNAP-native notification** | Needs SMTP credentials, or a decision to point Notification Center at the in-cluster ntfy. Matters because it is the only alert path that still works when the *cluster* is what is down — Prometheus alerting cannot report its own storage dying. |
| **`/mnt/ext` at 93%** | 29 MB free on the 417 MB QTS app partition (`/dev/md13`). All QNAP system files — nothing safe to prune by hand. Firmware updates and app installs write here, so it is a known wedge point. Not exposed over SNMP; check by hand. |
| **Thin-LUN reclaim** | See §6. Needs a privileged `fstrim` DaemonSet; deliberately not built without an operator decision on the PSA/hostPath trade-off. |
