# Kubernetes & apps — DEFERRED (scaffold only)

This tree is intentionally empty for now. It is built **after** the storage + network
foundation is up and validated (Objectives 1 & 2). The design below is recorded so the repo
structure is ready; nothing here is applied yet.

## Planned design (from research; finalize when this phase starts)

### Cluster
- **Talos Linux** VMs on Proxmox, provisioned by OpenTofu (`bpg/proxmox` for VMs +
  `siderolabs/talos` for machine config / bootstrap). Immutable, API-driven, fits full-rebuild.
- 3 control-plane-capable nodes. Worker layout TBD.

### GPU / AI node (the key fork — see ADR 0005)
- Strix Halo iGPU (gfx1151, ROCm preview) is reliable via **privileged LXC** (`/dev/dri`+`/dev/kfd`)
  or **bare-metal Talos** with `siderolabs/amdgpu` + AMD GPU Operator — **not** VM PCIe passthrough
  (AMD reset bug). Keep this node's provisioning in a separate module from generic workers.
- Inference on the iGPU (llama.cpp `GGML_HIP_NO_VMM=ON`, `HSA_OVERRIDE_GFX_VERSION=11.5.1`).
  NPU unusable on Linux today.

### Storage (CSI)
- Official **`qnap-dev/QNAP-CSI-PlugIn`** (Helm): **iSCSI** RWO default StorageClass + **SMB** RWX.
  (No NFS in that driver.) Reuses the same storage fabric brought up in Phase 2.
- democratic-csi / terricain-csi are dead ends for QNAP.

### Platform
- **GitOps**: Argo CD or Flux (TBD) — fixes the `apps/` layout.
- **Observability**: kube-prometheus-stack + Loki + Grafana, plus the AMD GPU metrics exporter.
- **Ingress/exposure**: ingress-nginx/Traefik + cert-manager, fronted by **Cloudflare Tunnel**
  (no open ports) using `chifor.me`; Tailscale for admin-only access.

## Layout (when built)
```
kubernetes/
├─ infra/   # OpenTofu: Talos image factory, VM provisioning, cluster bootstrap, GPU node module
└─ apps/    # GitOps root (app-of-apps / clusters/<name>): csi, monitoring, ingress, tunnel, apps
```
