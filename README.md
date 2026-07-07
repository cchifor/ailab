# ailab — Home AI Lab Infrastructure-as-Code

Fully **Infrastructure-as-Code, rebuildable-from-scratch** home lab for AI workloads.

- **Compute:** 3× Bosgame M5 (AMD Ryzen AI Max+ 395 "Strix Halo", 128 GB unified RAM, 2 TB NVMe) running a **Proxmox VE** cluster.
- **Storage:** 1× QNAP TBS-h574TX-i5-16G all-flash NAS (`ai-storage`), QuTS hero / ZFS, connected to the nodes over **Thunderbolt/USB4** (node1/2) and a **direct 10GbE** link (node3, via a Thunderbolt→10GbE adapter).

Everything that *can* be code is code: **OpenTofu** (`bpg/proxmox`) for the Proxmox API surface and **Ansible** (run from WSL2 Ubuntu) for host-level configuration. The few QNAP storage steps that have no usable API are captured as precise runbooks under `docs/runbooks/`.

> Status: **all phases live** — storage + network foundation, Talos/Cilium/Flux Kubernetes, 3-tier CSI, a full observability stack, the AI LLM appliance (router + UI), **SSO** (Authelia OIDC), a self-hosted **app suite** (Gitea, Vaultwarden, ntfy, a private OCI registry, dev-worker VMs, a Homepage dashboard), **security + dependency automation** (Trivy, cert-manager, Renovate), a **free encrypted 3-2-1 backup/DR**, public/private internet exposure, and self-hosted CI runners (`cchifor/platform`). See the status table below, `docs/`, and the ADRs.

## Topology (summary)

```
                          QNAP ai-storage (QuTS hero / ZFS, all-flash)
                          mgmt 192.168.1.225
            TB#1 ┌──────────────┼──────────────┐ 10GbE
                 │ 10.55.0.2     │ 10.55.0.6    │ 10.55.0.10
                 │ (T2E)         │ (T2E)        │
       10.55.0.1 │     10.55.0.5 │   10.55.1.9 │ (direct 10GbE)
     ┌───────────┴──┐ ┌──────────┴──┐ ┌─────────┴────┐
     │ ai-node1     │ │ ai-node2    │ │ ai-node3     │
     │ 192.168.0.2  │ │ 192.168.0.3 │ │ 192.168.0.4  │   (mgmt LAN)
     └──────────────┘ └─────────────┘ └──────────────┘
        Strix Halo       Strix Halo       Strix Halo
   Storage fabric 10.55.0.0/24 (node1/2 over the QNAP Thunderbolt bridge `tbtbr0`);
   node3 on a direct 10GbE link (10.55.1.0/24). Service IP 10.55.0.254 reachable from every node.
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
| `ansible/` | host-level config: kernel, Thunderbolt links, storage net, NFS mounts, host `node_exporter`, CPU performance governor, validation |
| `tofu/` | OpenTofu (`bpg/proxmox`): datacenter storage now, VMs/K8s later |
| `scripts/` | bootstrap + read-only discovery helpers |
| `kubernetes/` | live — `infra/` (Talos VMs + `ai-lxc/` GPU LXCs + dev-worker/runner/registry VMs, OpenTofu; `cloudflare/` edge as code) + `apps/` (Flux GitOps: CSI, observability, AI, SSO, edge/exposure, backup/DR, security, the self-hosted app suite) |

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
| K8s storage (3 tiers) | ✅ done — `nfs-csi` (RWX default), `local-path` (node-local NVMe), `qnap-iscsi` (network block from the ZFS pool, RWO, migratable — Trident `csi.trident.qnap.io`). Prometheus TSDB on `qnap-iscsi`. **VolumeSnapshots** live (external-snapshotter v8 + class; round-trip validated). (`docs/k8s-followups.md`) |
| K8s platform hardening | ✅ done — colocation governance (kubelet reservations + PriorityClasses + LimitRanges, ADR 0009); **CSI on the Thunderbolt fabric** (host-router+SNAT, A1 — `nfs-csi` + `qnap-iscsi` at `10.55.0.254`, ~660 MB/s vs ~280 on 2.5 GbE, ADR 0011) + a per-node storage-fabric health-check (blackbox DaemonSet + alert). |
| K8s backup / DR (3-2-1, free) | ✅ done — Layer A: CSI VolumeSnapshots. Layer B: **Velero** (CSI snapshot data-movement via Kopia, cluster state + PV data) + **talos-backup** (age-encrypted etcd snapshots) → **versitygw** S3 on a QNAP USB-NVMe (local copy) → nightly **rclone-crypt → Google Drive** (encrypted off-site). Talos secrets bundle SOPS-escrowed in git; backup→restore round-trip verified (ADR 0010). |
| K8s SSO | ✅ done — self-hosted **Authelia** OIDC at `sso.chifor.me`; clients: Grafana, Open WebUI, Homepage, Gitea, the registry (ADR 0012) |
| K8s app suite + dashboards | ✅ done — **Homepage** (`home.chifor.me`), **Gatus** uptime, **Headlamp** + **Hubble UI** (cluster/network), **Gitea** (`git.chifor.me`, SSO), **Vaultwarden** (`vault.chifor.me`, own auth + `/admin` behind CF Access), **ntfy** push alerts (Alertmanager→phone), a private **Zot OCI registry** (`registry.chifor.me`) |
| K8s security + automation | ✅ done — **cert-manager** (LE-via-Cloudflare-DNS01 + internal CA), **Trivy Operator** (cluster-wide vuln/misconfig scanning → Grafana), **Renovate** (self-hosted dependency-update PRs) |
| Dev-worker VMs | ✅ done — 3 Ubuntu VMs (`dev-worker-1/2/3`, .37/.38/.39) running Claude Code + Codex in persistent tmux, ttyd web terminals at `dw1/2/3.chifor.me` (CF Access); OpenTofu + ansible role `dev_worker` |
| K8s observability | ✅ metrics (Prometheus/Grafana) + logs (Loki+Alloy). Single **"AI Lab Fleet"** default dashboard — Hypervisors (host `node_exporter` on the 3 Proxmox hosts, ansible role `node_exporter`), Instances (VMs/CTs via `prometheus-pve-exporter`), AI (iGPU + llama.cpp), Storage (pools + PVCs + disk I/O + QNAP fabric). (`docs/k8s-followups.md` #14) |
| K8s: AI LLM appliance | ✅ done — 3× privileged GPU LXC, llama.cpp Vulkan; daily driver **Qwen3-30B-A3B** + **Qwen3.6-35B-A3B** (coder/vision), **gpt-oss-120B**, **Qwen3.5-122B**, **Gemma-4-26B** (vision) behind **LiteLLM** + **Open WebUI**; GPU+inference metrics (`docs/runbooks/ai-host-setup.md`, ADR 0008) |
| K8s: ingress + internet exposure | ✅ done — **Cloudflare Tunnel** (chat.chifor.me + Access) + **Tailscale** subnet-router mesh (192.168.0.0/24 + 10.55.0.0/24); `docs/runbooks/internet-exposure.md` |
| CI: self-hosted runners | ✅ done — 3 ephemeral runner VMs (`gha-runner-1/2/3`, .47/.48/.49, vmid 4101-3) for `cchifor/platform` CI (Docker + Compose 2.31 + Buildx + Playwright + uv + k6); OpenTofu (`kubernetes/infra/runners/`) + ansible role `github_runner`; **GitHub App** auth, joined the `self-hosted-hv` pool, memory ballooning 1→24 GiB; runner-health canary passing on the Proxmox runners. (ADR 0013, `docs/runbooks/ci-runners.md`) |
| K8s application HA (#100) | 🔄 ailab side ✅ — tiered availability (ADR 0016): Tier A ×2 + PDB/spread (cloudflared, infra-pg CNPG, Grafana→Postgres, Authelia + auth-valkey), Tier B accepted singletons documented; `ha-rules` guardrail alerts; `docs/runbooks/node-maintenance.md` (out-of-service-taint fast path). Pending in `cchifor/platform`: strive-pg ×3 + valkey fix per `docs/superpowers/specs/2026-07-06-strive-platform-ha-tier1-spec.md`, then the acceptance drain test |

**Proven live (2026-06-14):** Linux↔QNAP Thunderbolt T2E works; both TB ports + 10GbE up;
all nodes reach the NFS service IP `10.55.0.254`; OpenTofu-managed `qnap-nfs` mounted cluster-wide.
**Reboot-tested:** node reboot → TB link + mount auto-recover (~46 s); QNAP reboot → cron restores
the bridge IP and re-exports NFS (fixing a boot-race that left the TB subnet read-only) → all 3
nodes writable + active automatically.
