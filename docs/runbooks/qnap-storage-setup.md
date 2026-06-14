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
