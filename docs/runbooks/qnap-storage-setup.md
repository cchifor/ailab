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

## 2. UI part (one field) — Thunderbolt bridge service IP
The two TB ports are auto-bridged into the **"Thunderbolt Bridge (System Default)"** (`tbtbr0`).
`qcli_network` cannot target it (no interface ID), so set it once in the UI:

> **Control Panel → Network & Virtual Switch → Interfaces → Thunderbolt** (the bridge) →
> **Edit / Configure** → IPv4 **Static**: IP **`10.55.0.254`**, mask **`255.255.255.0`**,
> no gateway → Apply.

This is the **NFS service IP** the whole cluster mounts. It must persist across reboots (UI does this).
*(Interim, non-persistent, for testing: `sudo ip addr add 10.55.0.254/24 dev tbtbr0`.)*

T2E activates automatically when a node's Thunderbolt interface comes up (`/etc/init.d/thunderbolt_net.sh`
adds the port to the bridge), so no extra QNAP T2E "enable" step is needed.

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
