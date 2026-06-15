# ailab — Home AI Lab Infrastructure-as-Code

Fully **Infrastructure-as-Code, rebuildable-from-scratch** home lab for AI workloads.

- **Compute:** 3× Bosgame M5 (AMD Ryzen AI Max+ 395 "Strix Halo", 128 GB unified RAM, 2 TB NVMe) running a **Proxmox VE** cluster.
- **Storage:** 1× QNAP TBS-h574TX-i5-16G all-flash NAS (`ai-storage`), QuTS hero / ZFS, connected to the nodes over **Thunderbolt/USB4** (2 nodes) and **10GbE** (1 node, temporarily via a USB→2.5GbE adapter).

Everything that *can* be code is code: **OpenTofu** (`bpg/proxmox`) for the Proxmox API surface and **Ansible** (run from WSL2 Ubuntu) for host-level configuration. The few QNAP storage steps that have no usable API are captured as precise runbooks under `docs/runbooks/`.

> Status: **all phases live** — storage + network foundation, Talos/Cilium/Flux Kubernetes, NFS CSI, observability, the AI LLM appliance (5 models + router + UI), and public/private internet exposure. See the status table below, `docs/`, and the ADRs.

## Topology (summary)

```
                          QNAP ai-storage (QuTS hero / ZFS, all-flash)
                          mgmt 192.168.1.225
            TB#1 ┌──────────────┼──────────────┐ 10GbE
                 │ 10.55.0.2     │ 10.55.0.6    │ 10.55.0.10
                 │ (T2E)         │ (T2E)        │
       10.55.0.1 │     10.55.0.5 │     10.55.0.9│ (USB→2.5GbE, temp)
     ┌───────────┴──┐ ┌──────────┴──┐ ┌─────────┴────┐
     │ ai-node1     │ │ ai-node2    │ │ ai-node3     │
     │ 192.168.0.2  │ │ 192.168.0.3 │ │ 192.168.0.4  │   (mgmt LAN)
     └──────────────┘ └─────────────┘ └──────────────┘
        Strix Halo       Strix Halo       Strix Halo
   Dedicated storage net 10.55.0.0/24 (point-to-point /30s).
   QNAP service IP 10.55.0.254 reachable from every node over its own link.
```

See `docs/network-plan.md` for the authoritative IP plan and `docs/architecture.md` for the full design.

## Quick start

Prereqs are set up once on the Windows control box inside **WSL2 Ubuntu** (Ansible has no native Windows control node):

```bash
# from WSL, in the repo (it lives on the Windows FS at /mnt/c/Users/chifo/work/home/ailab)
just bootstrap     # install ansible + opentofu + collections into WSL
just discover      # read-only inventory of nodes + QNAP -> docs/_generated/
just net           # ansible: bring up Thunderbolt/10GbE storage links
just validate      # iperf3 / mount tests -> docs/_generated/
just plan          # opentofu plan (Proxmox storage)
just apply         # opentofu apply (register QNAP NFS in Proxmox)
```

Run `just` with no args to list all tasks. Raw commands are in each `justfile` recipe if you prefer not to install `just`.

## Repository layout

| Path | Purpose |
|---|---|
| `docs/` | architecture, network plan, ADRs, runbooks |
| `inventory/hosts.yml` | single source of truth for hosts, IPs, roles |
| `ansible/` | host-level config: kernel, Thunderbolt links, storage net, NFS mounts, validation |
| `tofu/` | OpenTofu (`bpg/proxmox`): datacenter storage now, VMs/K8s later |
| `scripts/` | bootstrap + read-only discovery helpers |
| `kubernetes/` | live — `infra/` (Talos VMs + `ai-lxc/` GPU LXCs, OpenTofu) + `apps/` (Flux GitOps: CSI, observability, AI, edge/exposure) |

## Secrets & state

- No secrets in git. Copy `tofu/terraform.tfvars.example` → `tofu/terraform.tfvars` (gitignored) and `ansible/group_vars/vault.example.yml` → an Ansible Vault file.
- OpenTofu state is local + gitignored for now; migrate to remote state before the lab grows (noted in `tofu/backend.tf`).

## Status

| Phase | State |
|---|---|
| 0 — Control env + access | ✅ done (SSH key, PVE API token, QNAP qcli) |
| 1 — Discovery | ✅ done (PVE 9.2.2/k7.0.2; QNAP QuTS hero h5.2.9, RAID-Z1) |
| 2 — Host networking (TB/10GbE) | ✅ done — 3 storage links up + persistent |
| 3 — QNAP storage (ZFS/NFS) | ✅ done — `pve-nfs` exported; service IP + export persisted as code (cron reconciler) |
| 4 — Validation | ✅ done — reboot-persistence verified (node1 **and** QNAP); ~1.1 GB/s over TB |
| 5 — Register NFS in Proxmox | ✅ done — `qnap-nfs` active on all 3 nodes (`/mnt/pve/qnap-nfs`, 5 TB) |
| K8s cluster (Talos + Cilium + Flux) | ✅ done — 3-node HA, GitOps live (`docs/k8s-architecture.md`) |
| K8s storage (3 tiers) | ✅ done — `nfs-csi` (RWX default), `local-path` (node-local NVMe), `qnap-iscsi` (network block from the ZFS pool, RWO, migratable, ZFS snapshots — Trident `csi.trident.qnap.io`). Prometheus TSDB on `qnap-iscsi`. (`docs/k8s-followups.md`) |
| K8s observability | ✅ metrics (Prometheus/Grafana/node-exporter) + AI/GPU Grafana dashboard; logs (Loki+Alloy) |
| K8s: AI LLM appliance | ✅ done — 3× privileged GPU LXC, llama.cpp Vulkan; **5 models** (Qwen3-30B-A3B, Qwen3-Coder-30B-A3B, gpt-oss-120B, Qwen3.5-122B, Qwen3-VL-8B vision) behind **LiteLLM** + **Open WebUI**; GPU+inference metrics (`docs/runbooks/ai-host-setup.md`, ADR 0008) |
| K8s: ingress + internet exposure | ✅ done — **Cloudflare Tunnel** (chat.chifor.me + Access) + **Tailscale** subnet-router mesh (192.168.0.0/24 + 10.55.0.0/24); `docs/runbooks/internet-exposure.md` |

**Proven live (2026-06-14):** Linux↔QNAP Thunderbolt T2E works; both TB ports + 10GbE up;
all nodes reach the NFS service IP `10.55.0.254`; OpenTofu-managed `qnap-nfs` mounted cluster-wide.
**Reboot-tested:** node reboot → TB link + mount auto-recover (~46 s); QNAP reboot → cron restores
the bridge IP and re-exports NFS (fixing a boot-race that left the TB subnet read-only) → all 3
nodes writable + active automatically.
