# Runbook — QNAP QuTS hero storage setup

The QNAP has no usable IaC provider for pools/shares, so this layer is a precise runbook.
Steps marked `[CLI]` can be scripted over SSH; steps marked `[UI]` are done in QuTS hero web UI.
**Capture the final state** (screenshots + `scripts/qnap-api.sh inventory`) into `docs/_generated/`.

> Prereq: discovery done (`just discover`), drive geometry **approved** by owner (ADR 0002).
> Destructive — assumes QNAP is fresh (wipe OK).

## 1. Firmware / edition
- `[UI]` Confirm **QuTS hero h5.1.x+** (Control Panel → System → Firmware Update).
- `[UI]` Enable **SSH** (Control Panel → Telnet/SSH).

## 2. Thunderbolt T2E (the #1 risk — do early, test both ports)
- `[UI]` Control Panel → Network & Virtual Switch → **Interfaces → Thunderbolt**.
  Enable **T2E**, set **static** IPs:
  - TB port 1 → `10.55.0.2/30`
  - TB port 2 → `10.55.0.6/30`  *(QNAP has a known port-2 T2E bug — verify carefully)*
- The assigned IP is shown on the NAS LCD. Plug node1→TB1, node2→TB2.
- After node-side bring-up (`thunderbolt-bringup.md`), validate per `validation.md`. If TB port 2
  is unstable → move node2 to 10GbE (see fallback) and update `docs/network-plan.md` + inventory.

## 3. 10GbE interface (node3 path)
- `[UI]` Interfaces → 10GbE adapter → static `10.55.0.10/30`. (Multi-gig; will negotiate 2.5G
  against node3's temp USB adapter, ~9.4 Gbps once the TB→10G adapter replaces it.)

## 4. Storage service IP `10.55.0.254` (single mount target)
Goal: one address reachable from all nodes, each over its own link.
- `[UI]` Add a **virtual adapter / IP alias** bound with IP `10.55.0.254/32` (or `/24`).
  Try Network & Virtual Switch → add a Virtual Switch / secondary IP on a storage interface.
- The /30 peers are directly connected, so the QNAP already routes back to each node.
- **If QuTS hero won't bind a clean alias:** skip this; use the **fallback** — Proxmox NFS over
  `192.168.1.225`, and/or per-node-restricted Proxmox storage on `.2/.6/.10`. Record which path is live.

## 5. ZFS storage pool  `[UI]` (geometry per ADR 0002, approved in discovery)
- Storage & Snapshots → Storage → **Create Pool** (QuTS hero = ZFS).
  - Geometry: **mirror/RAID10-style** (2× 2-way mirror + hot spare) *or* **RAID-Z1** (5-wide) — as approved.
  - Alert threshold ~80%.
- After creation, set dataset properties `[CLI]` (over SSH):
  ```bash
  # discover pool name first: zpool list ; zfs list
  zfs set compression=lz4 <pool>
  zfs get dedup <pool>        # MUST be off (default) — never enable on 16 GB RAM
  zfs set atime=off <pool>
  ```

## 6. Shared folder + NFS export  `[UI]`
- Create a **shared folder** `pve-nfs` on the pool (record host path, e.g. `/share/pve-nfs`
  or `/share/<CACHEDEV>/pve-nfs` — note the exact path for `tofu`).
- Control Panel → Network & File Services → **NFS** → enable **NFSv4**.
- Shared Folders → `pve-nfs` → Edit Shared Folder Permission → **NFS host access**:
  - Allow `10.55.0.0/24` (storage net) — and `192.168.0.0/24` if using the LAN fallback.
  - Squash: *no root squash* for Proxmox VM-disk storage (or map root appropriately), `rw`, `sync`.
- `[CLI]` verify: `cat /etc/exports` ; from a node `showmount -e 10.55.0.254`.

## 7. (Deferred) iSCSI + CSI
Not in this phase. The later Kubernetes phase uses the official `qnap-dev/QNAP-CSI-PlugIn`
(iSCSI RWO + SMB RWX). Leave the iSCSI service configured by the CSI driver then.

## 8. Record state
```bash
bash scripts/qnap-api.sh inventory > docs/_generated/qnap-state.txt   # read-only
```
Commit `docs/_generated/qnap-state.txt`? It's gitignored by default (machine-specific). Copy the
key facts (pool name, export path, geometry, IPs) into `inventory/hosts.yml` + `network-plan.md`.

## Outputs needed by the rest of the IaaC
| Value | Used by |
|---|---|
| pool name | runbook / CSI later |
| NFS export host path (e.g. `/share/pve-nfs`) | `tofu/terraform.tfvars` (`qnap_nfs_export`) |
| storage server IP (`10.55.0.254` or fallback) | `tofu/terraform.tfvars` (`qnap_nfs_server`) |
