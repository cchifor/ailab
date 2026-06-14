# Discovery findings — 2026-06-14

Snapshot from `scripts/discover.py` (read-only). Machine-specific raw output is in
`docs/_generated/` (gitignored); the durable facts are captured here.

## Proxmox cluster `ai`
- **3 nodes**, quorate (3/3 votes), corosync/knet. PVE **9.2.2**, kernel **7.0.2-6-pve** (Debian 13 trixie).
  → modern enough for USB4 + gfx1151 ROCm; **no kernel pin required** (`pve_pin_kernel: false`).
- Per node (identical Bosgame M5): AMD **Ryzen AI Max+ 395** (16C/32T), GPU **Radeon 8060S**
  (`/dev/dri/card0` + `/dev/renderD128` + `/dev/kfd` present → ROCm-ready).
- **RAM visible to OS ≈ 62 GiB** (of 128 GB) → ~64 GB carved to the iGPU in BIOS (fixed UMA/VRAM
  split). Confirm this is intentional; it's the per-node VRAM budget for AI (ADR 0005).
- Local storage per node: `nvme0n1` 1.9 TB (Kingston OM8TAP42048K1), `local` (dir) + `local-lvm` (lvmthin).

| PVE node | Mgmt IP | Storage link | Iface (now) | Identity |
|---|---|---|---|---|
| ai-node1 | 192.168.0.2 | Thunderbolt → QNAP | `thunderbolt0` (`thunderbolt-net`) | USB4 router `pci-0000:c7:00.6` |
| ai-node2 | 192.168.0.3 | Thunderbolt → QNAP | `thunderbolt0` (`thunderbolt-net`) | USB4 router `pci-0000:c7:00.6` |
| ai-node3 | 192.168.0.4 | USB→2.5GbE → QNAP 10GbE | `enxc0eac367835a` (`r8152`) | MAC `c0:ea:c3:67:83:5a` |

Onboard NIC on every node: `nic0` = `r8169` (RTL8125) **2.5GbE**, bridged to `vmbr0` (mgmt).
Wi-Fi `wlp195s0` (mt7925e) down.

## Thunderbolt ↔ QNAP — #1 risk de-risked ✅
On both TB nodes the kernel sees the QNAP as a Thunderbolt **XDomain host**:
```
thunderbolt 1-2: new host found, vendor=0x8086 device=0x1
thunderbolt 1-2: Intel Corp. ai-storage
```
`thunderbolt`+`thunderbolt_net` modules are loaded and a `thunderbolt0` netdev exists (DOWN, unconfigured).
QNAP TB controller is **Intel** (well-supported by `thunderbolt-net`). Remaining work: assign static
IPs both ends (T2E on the QNAP), bring up, and `iperf3`-validate per port.

## QNAP — BLOCKED on SSH
- Web UI reachable (`192.168.1.225:8080`), but **SSH port 22 refused** → SSH service is disabled.
- Needed to inventory installed NVMe drives (→ pool geometry decision) and script storage.
- **Action:** enable Control Panel → Telnet/SSH → Allow SSH (note the port if not 22). If admin MFA is
  on, password SSH may be blocked → use a dedicated service account or app password.
