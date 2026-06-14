# ADR 0006 — Kubernetes stack: Talos + Cilium + Flux + Traefik

**Status:** accepted · **Date:** 2026-06-14

## Context
Need a 100%-IaC, rebuildable Kubernetes cluster on the 3-node Proxmox cluster, with GitOps, a
capable CNI, and a maintained ingress. Decided after a cited analysis.

## Decision
- **Talos Linux as Proxmox VMs**, provisioned by OpenTofu (`bpg/proxmox` + `siderolabs/talos`).
  Immutable, API-only, declarative machine-config — the cleanest rebuildable target. 3 stacked
  control-plane VMs (HA, `allowSchedulingOnControlPlanes=true`).
- **Cilium** CNI with `kubeProxyReplacement=true` (Talos `cni=none`, kube-proxy disabled, KubePrism).
  Required for real NetworkPolicy (Flannel silently ignores it) + Hubble.
- **Flux** for GitOps (+ SOPS+age secrets), bootstrapped as the last OpenTofu step.
- **Traefik** ingress + cert-manager. **`ingress-nginx` is retired/EOL (Mar 2026)** — explicitly avoided.

## Consequences
- A minimal inline/bootstrap Cilium is required before Flux can take over (nothing schedules without a CNI).
- `cpu.type=host` (best perf) blocks live migration; Talos has no memory hotplug (size RAM up-front).
- Pin known-good versions (Talos 1.11/1.12, Cilium 1.16/1.17); upgrades = image replacement (immutable).

## Alternatives rejected
k3s/kubeadm (mutable, drift-prone), LXC-k8s (fragile); Flannel (no NetworkPolicy); ingress-nginx (EOL);
Argo CD (fine, but Flux chosen for lighter pull-only + tofu-native bootstrap + SOPS fit).
