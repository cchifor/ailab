# Kubernetes + AI architecture (Phase 3+)

Design for the cluster that runs on top of the storage/network foundation. Decisions below were
made with the user after a cited analysis (2026-06-14); they revise parts of the initial scaffold.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Keep Proxmox? | **Yes** — all 3 nodes stay Proxmox | Preserves the cluster + the storage fabric + host ROCm we already built |
| Kubernetes | **Talos Linux as Proxmox VMs** (bpg + siderolabs/talos via OpenTofu) | Immutable, API-only, 100% IaC/rebuildable |
| GPU / AI | **Privileged LXC AI appliance** (llama.cpp/Ollama on host ROCm) exposed to k8s | iGPU-in-VM is unusable (AMD reset bug); host ROCm/`/dev/kfd` already works; keeps Proxmox |
| GitOps | **Flux** (+ SOPS+age secrets) | Lighter, pull-only, first-class OpenTofu bootstrap + cleanest SOPS integration |
| Storage | **iSCSI (default RWO) + NFS (RWX)** | QNAP iSCSI CSI = real ZFS snapshots/clones; NFS reuses the live `pve-nfs` for shared volumes |
| CNI | **Cilium** (kube-proxy-free, KubePrism) | eBPF + Hubble; required for real NetworkPolicy (Flannel ignores it) |
| Ingress | **Traefik** + cert-manager | `ingress-nginx` is **retired/EOL (Mar 2026)** — do not use it |
| Public exposure | **Cloudflare Tunnel** (`chifor.me`, no open ports) + Cloudflare Access | Egress-only, hides home IP, free WAF; DNS-01 wildcard via cert-manager |
| Admin access | **Tailscale operator** (private tailnet) | Grafana/Flux/Traefik/kube-api off the public internet |
| Observability | kube-prometheus-stack + Loki (singleBinary/filesystem) + **Grafana Alloy** | Promtail is EOL; GPU metrics via `node_exporter` sysfs/DRM (AMD exporter is blind on gfx1151) |
| Inference engine | **llama.cpp (Vulkan) + Ollama**; **not vLLM** | ~96–100 tok/s decode (30B MoE) vs vLLM ~4 tok/s + source-only build on this UMA APU |

## Topology (Keep-Proxmox model)

```
Each Bosgame M5 (Proxmox host, ~62GB system RAM + ~64GB iGPU VRAM carveout):
  ├─ Talos control-plane VM  (HA: one per host, etcd quorum 2/3, schedulable)
  └─ Privileged LXC "ai"     (/dev/kfd+/dev/dri -> ROCm/Vulkan; llama.cpp/Ollama)
QNAP: iSCSI LUN (RWO) + existing pve-nfs (RWX), over the 10.55.0.0/24 fabric.
```
- **Control plane:** 3 stacked Talos CP VMs (one per node), `allowSchedulingOnControlPlanes=true`.
  Start ~8 vCPU / 32 GB / 40 GB disk each (tune; RAM is the binding constraint after the VRAM carveout).
- **AI:** start with **one** privileged LXC running the LLM, exposed to k8s via an `ExternalName`/Endpoints
  service; scale to one-per-node + a model router later. Models stored on QNAP.
- VMs use `cpu.type=host` (homogeneous CPUs; no live migration) and **no memory hotplug** (Talos limitation).

## Storage networking note (resolve at CSI sub-phase)
The TB/10GbE fabric (`10.55.0.0/24`, service IP `10.55.0.254`) terminates on the **hosts**, not the VMs.
Two ways to give the Talos VMs storage:
1. **Mgmt path (simple):** VMs reach QNAP at `192.168.1.225` over 2.5 GbE; add `192.168.0.0/24` to the QNAP
   iSCSI/NFS ACL. Works immediately, ~2.35 Gbps.
2. **TB-fabric path (fast):** bridge each host's storage link into a Proxmox bridge + give each VM a NIC on
   `10.55.0.0/24`; CSI traffic then uses Thunderbolt. More setup.
Bring the cluster up first (mgmt networking), then choose the storage path in the CSI sub-phase.

## Strix Halo iGPU specifics (for the AI LXC / future bare-metal)
- ROCm path: `HSA_OVERRIDE_GFX_VERSION=11.5.1`, `GGML_HIP_NO_VMM=ON`; best for prompt processing.
- Vulkan path (`OLLAMA_VULKAN=1` / llama.cpp `full-vulkan` image): best decode, most reliable today.
- Use the **fixed BIOS VRAM carve-out** (~64 GB already set) until Talos 1.12 + ROCm 7.x fix dynamic alloc.
- gfx1151 is ROCm "preview" (not on AMD's support matrix) — pin versions, gate upgrades.

## Build roadmap (sub-phases, built incrementally via Flux GitOps)
1. **Cluster** — `kubernetes/infra` tofu: Talos image (extensions: qemu-guest-agent, iscsi-tools,
   util-linux-tools), 3 CP VMs, machine config (cni=none, kube-proxy disabled), bootstrap, kubeconfig;
   Cilium; Flux bootstrap (SOPS+age).
2. **Storage CSI** — QNAP iSCSI LUN (`qcli_iscsi`, scripted) + official QNAP CSI (default RWO SC) +
   csi-driver-nfs on `pve-nfs` (RWX); decide storage networking (above).
3. **AI appliance** — privileged LXC (tofu bpg container + device passthrough) running llama.cpp/Ollama;
   `ExternalName` service into k8s.
4. **Observability** — kube-prometheus-stack + Loki + Alloy; GPU sysfs/DRM metrics + Grafana dashboards.
5. **Ingress + exposure** — Traefik + cert-manager (Cloudflare DNS-01 `*.chifor.me`); Cloudflare Tunnel
   + Cloudflare Access; Tailscale operator; Cilium default-deny NetworkPolicy.

## Repo layout
```
kubernetes/
├─ infra/          # OpenTofu (run like the storage layer): talos image, VMs, machine-config, flux bootstrap
│  ├─ machine-config/  *.yaml.tftpl (control-plane, worker)
│  └─ *.tf
└─ apps/           # Flux GitOps root (Kustomize + HelmRelease), SOPS-encrypted secrets
   ├─ clusters/ai/     # Flux Kustomizations (sync waves: cilium -> csi -> monitoring -> ingress -> apps)
   └─ infrastructure/, apps/
```

## Manual prerequisites (documented for rebuildability; can't be GitOps'd)
- Cloudflare account + `chifor.me` zone + scoped API token; Tailscale account + OAuth client.
- SOPS **age** key (the one bootstrap secret — back up offline, never commit).
- BIOS iGPU VRAM carve-out per node (already ~64 GB).
- QNAP iSCSI target/LUN (scripted via `qcli_iscsi`, captured in the runbook).
