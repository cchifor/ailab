# Network plan (authoritative)

This file is the human-readable source of truth; the machine-readable copy lives in
`inventory/hosts.yml` (consumed by Ansible) and `tofu/terraform.tfvars` (consumed by OpenTofu).
Keep them in sync.

## Existing networks

| Network | Purpose | Notes |
|---|---|---|
| `192.168.0.0/24` | Proxmox management LAN | nodes `.2/.3/.4`, UI `https://192.168.0.2:8006` |
| `192.168.1.0/24` | QNAP / general LAN | QNAP mgmt `ai-storage` = `192.168.1.225` (routed from `.0.x`) |

## Dedicated storage fabric — `10.55.0.0/24`

Point-to-point **/30s**, one per physical link. This honestly models the topology: the TB
links are point-to-point cables, **not** a switched fabric, so a flat /24 would not give
any-to-any reachability. Storage traffic is kept off both LANs.

| Link | Host iface (renamed) | Host IP /30 | QNAP iface | QNAP IP /30 | MTU |
|---|---|---|---|---|---|
| **L1** ai-node1 ↔ QNAP **TB#1** | `en05` | `10.55.0.1` | T2E port A | `10.55.0.2` | 4000 |
| **L2** ai-node2 ↔ QNAP **TB#2** | `en05` | `10.55.0.5` | T2E port B ⚠ | `10.55.0.6` | 4000 |
| **L3** ai-node3 ↔ QNAP **10GbE** | `enstor` | `10.55.0.9` | 10GbE (eth) | `10.55.0.10` | 1500 → 9000* |

\* Node3 link is via a temporary USB→2.5GbE adapter (~2.35 Gbps). Jumbo frames enabled only
if the adapter chipset supports it (validated in discovery); raise to 9000 with the future
Thunderbolt→10G adapter.

⚠ QNAP documents a known driver issue with T2E on **Thunderbolt port 2** — validate both
ports; if port 2 is flaky, swap cabling so the two TB nodes use port 1 + the most stable
port, or move node2 to 10GbE and re-plan.

### Single mount target — `10.55.0.254`

So the Proxmox cluster (and later K8s) can use **one** storage address while each node's
traffic still pins to its own fast link:

- QNAP binds a service IP **`10.55.0.254/32`** (alias on a virtual/loopback interface).
- Each node has a static route: `10.55.0.254/32 via <QNAP /30 IP on that node's link>`
  (node1 → via `10.55.0.2`, node2 → via `10.55.0.6`, node3 → via `10.55.0.10`).
- QNAP's return path to each node is the directly-connected /30 (no extra routes needed).

Proxmox NFS storage then uses `server=10.55.0.254`, shared cluster-wide, each node pinned to
its link. Node routes are applied by Ansible (`storage_net` role); the QNAP alias/route is a
runbook step (`docs/runbooks/qnap-storage-setup.md`).

**Fallback** (if QuTS hero can't bind the alias cleanly): register NFS over the QNAP LAN IP
`192.168.1.225` (works cluster-wide, slower, shared with mgmt) and/or per-node-restricted
Proxmox storage entries on the per-link QNAP IPs.

## Node ↔ cable mapping (CONFIRMED by discovery 2026-06-14)

| Node | Mgmt IP | Storage link | Current kernel name | Key id |
|---|---|---|---|---|
| `ai-node1` | 192.168.0.2 | Thunderbolt → QNAP | `thunderbolt0` | USB4 router `pci-0000:c7:00.6` |
| `ai-node2` | 192.168.0.3 | Thunderbolt → QNAP | `thunderbolt0` | USB4 router `pci-0000:c7:00.6` |
| `ai-node3` | 192.168.0.4 | USB→2.5GbE → QNAP 10GbE | `enxc0eac367835a` | MAC `c0:ea:c3:67:83:5a` (r8152) |

The QNAP enumerates over Thunderbolt on both TB nodes (`thunderbolt 1-2: … Intel Corp. ai-storage`)
and a `thunderbolt-net` netdev already exists — so Linux↔QNAP T2E is viable; we just assign IPs.

## Firewall / routing notes

- No routing needed *between* the storage /30s (hub-spoke; each node only talks to the QNAP).
- Keep NFS/iSCSI bound to the storage interfaces on the QNAP where possible.
- Management (SSH/API) stays on `192.168.0.x` / `192.168.1.x`.
