# Kubernetes & AI (Phase 3+)

Architecture **decided** 2026-06-14 (full design: `docs/k8s-architecture.md`; rationale: ADRs
0005/0006/0007). Built incrementally on top of the storage/network foundation.

## Decided stack
- **Cluster:** Talos Linux as Proxmox VMs (OpenTofu: `bpg/proxmox` + `siderolabs/talos`). 3 stacked
  control planes (HA, schedulable). Proxmox is kept on all nodes.
- **CNI:** Cilium (kube-proxy-free, KubePrism, Hubble).
- **GitOps:** Flux (+ SOPS+age). Bootstrapped as the last tofu step; owns everything in-cluster.
- **Storage:** QNAP iSCSI CSI (default RWO, snapshots) + `csi-driver-nfs` on `pve-nfs` (RWX).
- **AI:** privileged Proxmox **LXC** running llama.cpp (Vulkan) / Ollama on the host iGPU (ROCm),
  exposed to k8s via an `ExternalName` service. (iGPU-in-VM ruled out; bare-metal Talos deferred.)
- **Observability:** kube-prometheus-stack + Loki + Grafana Alloy; GPU metrics via `node_exporter` sysfs.
- **Ingress/exposure:** Traefik + cert-manager (Cloudflare DNS-01 `*.chifor.me`); Cloudflare Tunnel +
  Access (public); Tailscale operator (admin). `ingress-nginx` is EOL — not used.

## Layout
```
kubernetes/
├─ infra/   # OpenTofu: Talos image (qemu-guest-agent, iscsi-tools, util-linux-tools), VMs,
│           #          machine-config, Cilium + Flux bootstrap
└─ apps/    # Flux GitOps root (Kustomize + HelmRelease), SOPS-encrypted secrets
            #   clusters/ai/ sync waves: cilium -> csi -> monitoring -> ingress -> apps
```

## Build order
1. Cluster (Talos VMs + Cilium + Flux)  2. Storage CSI (iSCSI+NFS)  3. AI LXC appliance
4. Observability  5. Ingress + internet exposure

See `docs/k8s-architecture.md` for topology, sizing, the storage-networking decision, and the manual
prerequisites (Cloudflare/Tailscale accounts, SOPS age key, BIOS VRAM, QNAP iSCSI LUN).
