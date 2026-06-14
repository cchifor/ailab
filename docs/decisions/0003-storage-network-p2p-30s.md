# ADR 0003 — Storage network: point-to-point /30s + service IP 10.55.0.254

**Status:** accepted · **Date:** 2026-06-14

## Context
2 nodes connect to the QNAP over Thunderbolt/USB4 (point-to-point cables), 1 node over 10GbE
(temp USB→2.5GbE). The QNAP mgmt is on a different subnet (`192.168.1.225`) from the Proxmox
mgmt LAN (`192.168.0.x`). TB links are point-to-point, not a switched fabric, so any-to-any L2
is impossible across them.

## Decision
- Dedicated range **`10.55.0.0/24`** carved into **/30s**, one per link (see `docs/network-plan.md`).
- The QNAP binds a single **service IP `10.55.0.254/32`**; each node holds a static `/32` route to
  it via that node's QNAP /30 peer. Proxmox/K8s use the one address; traffic pins per-link.
- Storage kept off both existing LANs.

## Consequences
- One Proxmox NFS storage entry works cluster-wide while each node uses its own fast link.
- Matches Proxmox iSCSI/NFS best practice (separate subnet per path).
- Requires the QNAP to bind an alias IP + we add node routes (Ansible). If QuTS hero can't bind
  the alias cleanly, fall back to NFS over `192.168.1.225` and/or per-node-restricted storage.

## Alternatives rejected
- **Flat /24 storage VLAN**: needs a switch; impossible over point-to-point TB.
- **Mount per-link QNAP IPs directly** (no service IP): breaks single shared-storage identity
  needed for clean Proxmox live migration.
