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
| **L1** pve-node1 ↔ QNAP **TB#1** | `en05` | `10.55.0.1` | T2E port 1 | `10.55.0.2` | 4000 |
| **L2** pve-node2 ↔ QNAP **TB#2** | `en05` | `10.55.0.5` | T2E port 2 ⚠ | `10.55.0.6` | 4000 |
| **L3** pve-node3 ↔ QNAP **10GbE** | `enstor` | `10.55.0.9` | 10GbE (eth) | `10.55.0.10` | 1500 → 9000* |

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

## Node ↔ cable mapping

`pve-node1`/`pve-node2` = the two Thunderbolt-connected nodes; `pve-node3` = the 10GbE
(temp USB-2.5G) node. The exact physical-node → mgmt-IP → cable mapping is **confirmed
during discovery** and recorded in `inventory/hosts.yml`.

## Firewall / routing notes

- No routing needed *between* the storage /30s (hub-spoke; each node only talks to the QNAP).
- Keep NFS/iSCSI bound to the storage interfaces on the QNAP where possible.
- Management (SSH/API) stays on `192.168.0.x` / `192.168.1.x`.
