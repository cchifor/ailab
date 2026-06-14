# ADR 0001 — IaaC stack: OpenTofu + Ansible

**Status:** accepted · **Date:** 2026-06-14

## Context
Control box is Windows 11 + WSL2 Ubuntu + Docker, with OpenTofu/Terraform, kubectl, helm.
We need declarative provisioning of the Proxmox API surface and imperative-but-idempotent
host OS configuration (kernel, Thunderbolt networking, mounts) that the Proxmox API cannot do.

## Decision
- **OpenTofu** with the **`bpg/proxmox`** provider for the Proxmox API surface (datacenter
  storage now; VMs/LXC and Talos K8s later). `bpg` is actively maintained and supports PVE 8/9,
  storage, VMs, LXC, SDN, PCI hardware mappings, users/tokens. (Telmate provider is unmaintained.)
- **Ansible** (installed in WSL2 Ubuntu) for host-level config via root SSH.
- Run order: **Ansible → OpenTofu**.

## Consequences
- Two tools, clear boundary: Ansible = inside the host OS; OpenTofu = the PVE API.
- OpenTofu chosen over Terraform per owner preference (open-source license). `bpg` works with both.
- A few `bpg` operations (snippets, hardware mappings) need PAM/root SSH, not just an API token —
  acceptable since we already have root SSH for Ansible.

## Alternatives rejected
- **All-Ansible** (proxmoxer): weaker drift control / declarative VM lifecycle.
- **Terraform + Telmate**: provider unmaintained.
- **Pulumi**: no benefit here; adds a language runtime.
