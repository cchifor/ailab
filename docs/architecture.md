# Architecture

## Goals

1. **100% reproducible** — the lab can be rebuilt from this repo (+ the QNAP runbook for the one non-API'able layer).
2. **Storage + network first** — a fast, validated storage fabric the future Kubernetes/AI cluster will consume.
3. **AI-ready** — Strix Halo iGPU (ROCm) compute, large-memory LLM inference, exposed safely to the internet later.

## Hardware

| Role | Device | Key specs | Notes |
|---|---|---|---|
| Compute ×3 | Bosgame M5 | Ryzen AI Max+ 395 (Strix Halo), 128 GB unified LPDDR5x, 2 TB NVMe, 2× USB4 40G, 1× 2.5GbE (RTL8125) | iGPU Radeon 8060S (gfx1151, ROCm preview). NPU not usable on Linux yet. |
| Storage ×1 | QNAP TBS-h574TX-i5-16G | i5-1340PE, **16 GB soldered**, 5× E1.S/M.2 NVMe **(PCIe Gen3 ×2 each)**, 1× 10GbE, 1× 2.5GbE, 2× TB4 | QuTS hero (ZFS). 16 GB RAM ⇒ compression yes, **dedup no**. |

## Layers

```
┌─────────────────────────────────────────────────────────────┐
│ Applications (deferred)        apps via GitOps                │
├─────────────────────────────────────────────────────────────┤
│ Kubernetes (deferred)   Talos VMs + iGPU node (LXC/bare)     │
│                         QNAP CSI (iSCSI/SMB), observability   │
├─────────────────────────────────────────────────────────────┤
│ Proxmox VE cluster      VMs/LXC, datacenter NFS storage       │  <- OpenTofu (bpg/proxmox)
├─────────────────────────────────────────────────────────────┤
│ Host OS (Debian/PVE)    kernel pin, thunderbolt-net, ifupdown │  <- Ansible
├─────────────────────────────────────────────────────────────┤
│ Storage fabric          TB/USB4 + 10GbE point-to-point /30s   │  <- Ansible + QNAP runbook
├─────────────────────────────────────────────────────────────┤
│ QNAP QuTS hero          ZFS pool, NFS export, T2E             │  <- runbook + scripted SSH/API
└─────────────────────────────────────────────────────────────┘
```

## Tooling split (why two tools)

- **Ansible** owns everything the Proxmox API cannot touch: `/etc/modules`, kernel pinning, systemd `.link`/udev for Thunderbolt interface naming, `/etc/network/interfaces`, MTU, static routes, NFS client mounts, and validation. Runs from **WSL2 Ubuntu** (no native Windows control node).
- **OpenTofu** (`bpg/proxmox`) owns the Proxmox API surface: datacenter storage entries now; VMs/LXC and the Talos K8s cluster later.
- **QNAP** has no usable IaC provider for pools/shares → a precise **runbook** (`docs/runbooks/qnap-storage-setup.md`) plus scripted SSH/API where safe. Everything downstream of the QNAP (Proxmox storage, K8s CSI) stays declarative.

Run order: **Ansible (host net + mounts) → OpenTofu (register storage)**. QNAP pool/export is created (runbook) between host-net bring-up and the Proxmox registration.

## Key design decisions

See `docs/decisions/` (ADRs). Summary:

- **0001** OpenTofu + Ansible (not Terraform, not all-Ansible).
- **0002** QuTS hero / ZFS, `compression=lz4`, no dedup.
- **0003** Dedicated `10.55.0.0/24` storage net as point-to-point /30s + a QNAP service IP `10.55.0.254` reachable per-link.
- **0004** Thunderbolt-net ↔ QNAP T2E on Linux is the #1 risk — validate per-port early, 10GbE fallback.
- **0005** AI GPU access via LXC (or bare-metal Talos), **not** VM PCIe passthrough — deferred decision.

## Performance expectations (set realistically)

| Path | Expected | Why |
|---|---|---|
| Thunderbolt/USB4 (Strix Halo, Linux) | ~10–11 Gbps/dir | stock `thunderbolt-net` driver-bound, not 40G |
| Node3 temp USB→2.5GbE | ~2.35 Gbps | adapter limit until TB→10G arrives |
| Future 10GbE | ~9.4 Gbps | line rate |
| Per NVMe drive | ~1.6 GB/s | QNAP slots are PCIe Gen3 ×2 |

TB's value here is **two extra dedicated links + isolation**, not raw speed over 10GbE.
