# ailab — Home AI Lab Infrastructure-as-Code

*Fully Infrastructure-as-Code, rebuildable-from-scratch home lab for AI workloads.*

- **Compute:** 3× Bosgame M5 (AMD Ryzen AI Max+ 395 "Strix Halo", 128 GB unified RAM, 2 TB NVMe) running a **Proxmox VE** cluster.
- **Storage:** 1× QNAP TBS-h574TX-i5-16G all-flash NAS (`ai-storage`), QuTS hero / ZFS, connected to the nodes over **Thunderbolt/USB4** (node1/2) and a **direct 10GbE** link (node3, via a Thunderbolt→10GbE adapter).

Everything that *can* be code is code: **OpenTofu** (`bpg/proxmox`) for the Proxmox API surface, **Ansible** for the original host bring-up, and **Flux GitOps** for everything inside Kubernetes. The few QNAP storage steps with no usable API are captured as precise runbooks under `docs/runbooks/`. Day-2 reality on the current control box (Windows): WSL has no internet, so host/LXC work goes through the paramiko helpers (`scripts/node-ssh.py`, `scripts/lxc-exec.py`) and `tofu.exe` runs natively on Windows — [`CLAUDE.md`](CLAUDE.md) is the operator cheat-sheet.

> Status: **all phases live** — storage + network foundation, Talos/Cilium/Flux Kubernetes, 3-tier CSI, a full observability stack, the AI LLM appliance (router + UI), **SSO** (Authelia OIDC), a self-hosted **app suite** (Gitea, Vaultwarden, ntfy, a private OCI registry, dev-worker VMs, a Homepage dashboard), **security + dependency automation** (Trivy, cert-manager, Renovate), a **free encrypted 3-2-1 backup/DR**, public/private internet exposure, self-hosted CI runners (`cchifor/platform`), and **tiered application HA** (any one node can be drained with zero downtime on the critical path — ADR 0016). See the status table below, `docs/`, and the ADRs.

---

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

See [`docs/network-plan.md`](docs/network-plan.md) for the authoritative IP plan and [`docs/architecture.md`](docs/architecture.md) for the full design.

### Inventory (mgmt LAN 192.168.0.0/24)

| Role | IPs | vmid/ctid |
|---|---|---|
| Proxmox hosts | ai-node1/2/3 = .2 / .3 / .4 | — |
| Talos control planes (also workers) | .41 / .42 / .43 — API VIP **.40:6443** | 4001–4003 |
| AI LLM GPU LXCs (privileged, llama.cpp/Vulkan) | .44 / .45 / .46 | 5001–5003 |
| GitHub Actions runner VMs | .47 / .48 / .49 + .33 / .34 | 4101–4105 |
| Dev-worker VMs (user `c4`) | .37 / .38 / .39 | 4201–4203 |
| Registry pull-through LXC (node1) | — | 5004 |

---

## Storage

Two layers share the same QNAP ZFS pool over the Thunderbolt fabric (service IP `10.55.0.254`; ~1.1 GB/s raw, ~660 MB/s through CSI vs ~280 on 2.5 GbE — ADR 0011):

1. **Proxmox level** — the `qnap-nfs` datacenter storage (`/pve-nfs` export, mounted at `/mnt/pve/qnap-nfs` on every node, 5 TB). Deliberately holds **no VM disks** — it carries the **LLM GGUFs** (`models/`, bind-mounted into the GPU LXCs as `/models`) and doubles as the backing share for the `nfs-csi` PVs below.
2. **Kubernetes level** — three CSI StorageClasses:

| Class | Driver | Shape | Reclaim | Used by |
|---|---|---|---|---|
| `nfs-csi` (default) | [csi-driver-nfs](https://github.com/kubernetes-csi/csi-driver-nfs) | RWX file, subdirs of `/pve-nfs` | Delete | loki, valkey (strive), open-webui, both RustFS object stores |
| `qnap-iscsi` | QNAP [Trident](https://github.com/qnap-dev/QNAP-CSI-PlugIn) (`csi.trident.qnap.io`) | RWO block LUNs (ext4), **VolumeSnapshots** | **Retain** | all databases: strive-pg ×3, infra-pg ×2, Prometheus TSDB, Tempo, Gitea, Vaultwarden, ntfy, Trivy |
| `local-path` | rancher local-path | node-local NVMe | Delete | **nothing** (kept for scratch only — node-local storage pins pods to a node, ADR 0016) |

**Where things actually persist:**

- **Talos OS + etcd: NOT on the NAS** — each control-plane VM's 40 G disk is `local-lvm` on the node's NVMe (etcd belongs on local disk). Its DR story is `talos-backup` age-encrypted etcd snapshots, not shared storage.
- **Databases** live on `qnap-iscsi` **block LUNs**, which are *invisible in the NFS share* — Trident carves them from the ZFS pool via the QNAP API (see them in the QNAP iSCSI UI or `tridentctl get volume`). A LUN re-attaches wherever its pod reschedules (node-loss-migratable), but is single-attach — so anything needing fast failover uses **app-level replication** (CNPG streaming) instead of volume migration (~6-min force-detach on hard node loss; ADR 0016 + `docs/runbooks/node-maintenance.md`).
- **RWX/file PVs** are plain `pvc-<uuid>/` directories inside `/pve-nfs`, next to `models/`.
- ⚠️ **"qnap-nfs looks empty" gotcha:** the Proxmox UI storage panel only lists Proxmox content types (VM images, ISOs, backups) — all deliberately empty here. `ls /mnt/pve/qnap-nfs/` from any node shows the real contents (`models/` + the `pvc-*` dirs).

**Backup (3-2-1, free — ADR 0010):** Layer A = CSI VolumeSnapshots (`qnap-iscsi` class, round-trip validated). Layer B = **Velero** (CSI snapshot data-movement via Kopia) + **talos-backup** → **versitygw** S3 on a QNAP USB-NVMe (local copy, off the ZFS pool) → nightly **rclone-crypt → Google Drive** (encrypted off-site). Talos secrets bundle is SOPS-escrowed in git.

Decisions: ADR 0002 (QuTS hero/ZFS) · 0003 (storage network) · 0007 (class-per-workload rules) · 0010 (backup/DR) · 0011 (CSI over Thunderbolt). Manual NAS steps: [`docs/runbooks/qnap-storage-setup.md`](docs/runbooks/qnap-storage-setup.md).

---

## Quick start (day-0 bring-up)

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

Run `just` with no args to list all tasks — the K8s-era recipes live there too (`just runners`, `just dev-workers`, `just registry`, `just plan|apply` for the Talos CPs). Raw commands are in each `justfile` recipe if you prefer not to install `just`.

### Day-2 operations (the gotchas that bite)

- **Kubernetes apps** (`kubernetes/apps/**`): Flux reconciles `main` — PR → squash-merge to ship. **VMs/LXCs** (`kubernetes/infra/**`): OpenTofu applied by hand via `just` (Flux does NOT manage these).
- **kubectl:** the default context is a *different* cluster — always `kubectl --context admin@ai` or `KUBECONFIG=kubernetes/infra/_out/kubeconfig`.
- **talosctl:** use `_out/talosctl-1112.exe` (v1.11.2, matches the cluster); the system talosctl is v1.6.2 and silently drops newer config keys. Node maintenance = `talosctl shutdown`, **one node at a time**, etcd 3/3 between nodes — [`docs/runbooks/node-maintenance.md`](docs/runbooks/node-maintenance.md).
- **Hosts/LXCs:** `python scripts/node-ssh.py <host-ip> "<cmd>"` and `python scripts/lxc-exec.py <host> <ctid>` (no Ansible in WSL on this box).

---

## Repository layout

| Path | Purpose |
|---|---|
| `docs/` | architecture, network plan, ADRs (`decisions/`), runbooks |
| `inventory/hosts.yml` | single source of truth for hosts, IPs, roles |
| `ansible/` | host-level config: kernel, Thunderbolt links, storage net, NFS mounts, host `node_exporter`, CPU governor, validation |
| `tofu/` | OpenTofu (`bpg/proxmox`): datacenter storage (the original phase-0 module) |
| `scripts/` | bootstrap, discovery, model-fetch, and the paramiko host/LXC helpers |
| `kubernetes/infra/` | OpenTofu: Talos CP VMs, `ai-lxc/` GPU LXCs, `runners/`, `dev-workers/`, `registry-lxc/`, `cloudflare/` edge; Talos machine config in `machine-config/` |
| `kubernetes/apps/` | Flux GitOps: CSI + storage, observability, AI, SSO, `databases/` (shared CNPG), edge/exposure, backup/DR, security, the app suite |
| `plans/` | dated planning records (historical — don't rewrite) |
| `bench/` | live-server tok/s benchmarks (carve-vs-GTT) |

## Documentation

| Topic | Where |
|---|---|
| Full design / architecture | [`docs/architecture.md`](docs/architecture.md), [`docs/k8s-architecture.md`](docs/k8s-architecture.md) |
| Authoritative IP plan | [`docs/network-plan.md`](docs/network-plan.md) |
| **Decisions (source of truth)** | [`docs/decisions/`](docs/decisions/) — 16+ ADRs, living documents |
| Operations | [`docs/runbooks/`](docs/runbooks/) — node maintenance & node loss, AI host setup, internet exposure, CI runners, dev workers, QNAP storage, registry cache |
| Operator cheat-sheet (paths, contexts, gotchas) | [`CLAUDE.md`](CLAUDE.md) |
| K8s follow-ups / backlog | [`docs/k8s-followups.md`](docs/k8s-followups.md) |

## Secrets & state

- **No secrets in git.** Kubernetes secrets are **SOPS + age** (`*.sops.yaml`, rules in `.sops.yaml`); the age key lives at `kubernetes/infra/_out/age.agekey`. **Never commit `_out/`** — kubeconfig, talosconfig, the age key, and tofu creds all live there (gitignored).
- Phase-0 tooling: copy `tofu/terraform.tfvars.example` → `tofu/terraform.tfvars` (gitignored) and `ansible/group_vars/vault.example.yml` → an Ansible Vault file.
- OpenTofu state is **local + gitignored** per module (`kubernetes/infra/**/terraform.tfstate`); migrate to remote state before the lab grows.

---

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
| K8s storage (3 tiers) | ✅ done — see the [Storage](#storage) section; **VolumeSnapshots** live (external-snapshotter v8 + class; round-trip validated) |
| K8s platform hardening | ✅ done — colocation governance (kubelet reservations + PriorityClasses + LimitRanges, ADR 0009); **CSI on the Thunderbolt fabric** (host-router+SNAT, A1 — `nfs-csi` + `qnap-iscsi` at `10.55.0.254`, ~660 MB/s vs ~280 on 2.5 GbE, ADR 0011) + a per-node storage-fabric health-check (blackbox DaemonSet + alert). |
| K8s backup / DR (3-2-1, free) | ✅ done — Layer A: CSI VolumeSnapshots. Layer B: **Velero** (CSI snapshot data-movement via Kopia, cluster state + PV data) + **talos-backup** (age-encrypted etcd snapshots) → **versitygw** S3 on a QNAP USB-NVMe (local copy) → nightly **rclone-crypt → Google Drive** (encrypted off-site). Talos secrets bundle SOPS-escrowed in git; backup→restore round-trip verified (ADR 0010). |
| K8s SSO | ✅ done — self-hosted **Authelia** OIDC at `sso.chifor.me` (×2 replicas on infra-pg Postgres + auth-valkey sessions); clients: Grafana, Open WebUI, Homepage, Gitea, the registry (ADR 0012/0016) |
| K8s app suite + dashboards | ✅ done — **Homepage** (`home.chifor.me`), **Gatus** uptime, **Headlamp** + **Hubble UI** (cluster/network), **Gitea** (`git.chifor.me`, SSO), **Vaultwarden** (`vault.chifor.me`, own auth + `/admin` behind CF Access), **ntfy** push alerts (Alertmanager→phone), a private **Zot OCI registry** (`registry.chifor.me`) |
| K8s security + automation | ✅ done — **cert-manager** (LE-via-Cloudflare-DNS01 + internal CA), **Trivy Operator** (cluster-wide vuln/misconfig scanning → Grafana), **Renovate** (self-hosted dependency-update PRs) |
| Dev-worker VMs | ✅ done — 3 Ubuntu VMs (`dev-worker-1/2/3`, .37/.38/.39) running Claude Code + Codex in persistent tmux, ttyd web terminals at `dw1/2/3.chifor.me` (CF Access); OpenTofu + ansible role `dev_worker` |
| K8s observability | ✅ metrics (Prometheus/Grafana) + logs (Loki+Alloy) + traces (Tempo, strive platform). Single **"AI Lab Fleet"** default dashboard — Hypervisors (host `node_exporter` on the 3 Proxmox hosts), Instances (VMs/CTs via `prometheus-pve-exporter`), AI (iGPU + llama.cpp), Storage (pools + PVCs + disk I/O + QNAP fabric) |
| K8s: AI LLM appliance | ✅ done — 3× privileged GPU LXC, llama.cpp Vulkan; daily driver **Qwen3.6-35B-A3B** (256K context, image+video), **gpt-oss-120B**, **Qwen3.5-122B**, **Gemma-4-26B** (vision, on-demand) behind **LiteLLM** + **Open WebUI**; small-carve (512 MB) + GTT on all 3 nodes; GPU+inference metrics (`docs/runbooks/ai-host-setup.md`, ADR 0008/0015) |
| K8s: ingress + internet exposure | ✅ done — **Cloudflare Tunnel** (chat.chifor.me + Access) + **Tailscale** subnet-router mesh (192.168.0.0/24 + 10.55.0.0/24); `docs/runbooks/internet-exposure.md` |
| CI: self-hosted runners | ✅ done — 5 ephemeral runner VMs for `cchifor/platform` CI (Docker + Compose + Buildx + Playwright + uv + k6); OpenTofu (`kubernetes/infra/runners/`) + ansible role `github_runner`; **GitHub App** auth, `self-hosted-hv` pool, memory ballooning; runner-health canary (ADR 0013, `docs/runbooks/ci-runners.md`) |
| K8s application HA (#100) | ✅ done — tiered availability (ADR 0016): Tier A survives any one-node drain (**strive-pg ×3** [platform repo], **infra-pg** CNPG ×2, **Grafana ×2 on Postgres**, **Authelia ×2** + auth-valkey sessions, cloudflared ×2 — PDB/spread/tolerations), Tier B accepted singletons documented; `ha-rules` guardrail alerts; `docs/runbooks/node-maintenance.md`. **Acceptance drain test passed 2026-07-07**: primary-node drain = 36 s drain / ~7 s DB write gap (was: 5-min hang + hard-killed DB); replica-node drain = 0 write failures (spec: `docs/superpowers/specs/2026-07-06-strive-platform-ha-tier1-spec.md`) |

**Proven live (2026-06-14):** Linux↔QNAP Thunderbolt T2E works; both TB ports + 10GbE up;
all nodes reach the NFS service IP `10.55.0.254`; OpenTofu-managed `qnap-nfs` mounted cluster-wide.
**Reboot-tested:** node reboot → TB link + mount auto-recover (~46 s); QNAP reboot → cron restores
the bridge IP and re-exports NFS (fixing a boot-race that left the TB subnet read-only) → all 3
nodes writable + active automatically.
