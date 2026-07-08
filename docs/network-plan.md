# Network plan (authoritative)

This file is the human-readable source of truth; the machine-readable copy lives in
`inventory/hosts.yml` (consumed by Ansible) and `tofu/terraform.tfvars` (consumed by OpenTofu).
Keep them in sync.

## Existing networks

| Network | Purpose | Notes |
|---|---|---|
| `192.168.0.0/24` | Proxmox management LAN | nodes `.2/.3/.4`, UI `https://192.168.0.2:8006` |
| `192.168.1.0/24` | QNAP / general LAN | QNAP mgmt `ai-storage` = `192.168.1.225` (routed from `.0.x`) |

### Management LAN `192.168.0.0/24` — static allocations

Static reservations are `.2`–`.50`; the **router DHCP pool starts at `.51`** (`.51`–`.254`).
Free static space: `.5`–`.7`, `.20`–`.35`, `.37`–`.39`, `.47`–`.50` (dev-workers on consecutive
`.8`–`.13`, GH-runners on `.14`–`.18`, `.19` reserved for the deferred runner-6).

| Range / IP | Owner | Source of truth |
|---|---|---|
| `.1` | LAN gateway | router |
| `.2 / .3 / .4` | Proxmox hosts `ai-node1/2/3` | `inventory/hosts.yml` |
| `.5 / .6 / .7` | free (static) — vacated by the dev-worker renumber | — |
| `.8`–`.13` | Dev-worker VMs `dev-worker-1..6` (2 per node) | `kubernetes/infra/dev-workers/variables.tf` |
| `.14`–`.18` | GitHub runner VMs `gha-runner-1..5` (2 node1/node2, 1 node3) | `kubernetes/infra/runners/variables.tf` |
| `.19` | GitHub runner VM `gha-runner-6` — reserved, deferred (node3 RAM) | `kubernetes/infra/runners/variables.tf` |
| `.20`–`.35` | free (static) | — |
| `.36` | OCI registry LXC `ai-registry` | `kubernetes/infra/registry-lxc/variables.tf` |
| `.37 / .38 / .39` | free (static) — vacated by the dev-worker renumber | — |
| `.40` | Talos control-plane VIP | `kubernetes/infra/variables.tf` |
| `.41 / .42 / .43` | Talos control-plane VMs `ai-cp-1/2/3` | `kubernetes/infra/variables.tf` |
| `.44 / .45 / .46` | AI LLM LXCs `ai-llm-1/2/3` | `kubernetes/infra/ai-lxc/variables.tf` |
| `.47 / .48 / .49` | free (static) — vacated by the GH-runner renumber | — |
| `.50` | free (static) | — |
| `.51`–`.254` | router DHCP pool | router |

> **Keep all lab static IPs inside `.2`–`.50`.** The DHCP pool starts at `.51`, so anything `.51`+
> can be leased to a random client — exactly the collision that pushed the AI LXCs off `.51`–`.53`
> into the static block (`kubernetes/infra/ai-lxc/variables.tf`). The dev-workers use `.37`–`.39`
> + `.5`–`.7` (free static space), so no router change is needed.

## Dedicated storage fabric — `10.55.0.0/24`

> ⚠️ **The /30 design below was the original plan and was NOT realized.** Live reality (verified; source of
> truth `inventory/hosts.yml`): the QNAP bridges both TB ports into `tbtbr0`, so node1/node2 share a **flat
> `10.55.0.0/24`** (`10.55.0.1`/`10.55.0.2` → `10.55.0.254`); **node3** is on a separate `10.55.1.0/24` via
> a **Thunderbolt→10GbE** adapter (Ubiquiti UACC, AQC113/`atlantic` → `enp99s0`), DIRECT to QNAP eth1
> (`10.55.1.9` → `.254` via `10.55.1.254`), MTU 1500 (jumbo-capable; bump to 9000 once QNAP eth1 is set).
> See ADR 0003 (superseded) + ADR 0011.

Point-to-point **/30s**, one per physical link. This honestly models the topology: the TB
links are point-to-point cables, **not** a switched fabric, so a flat /24 would not give
any-to-any reachability. Storage traffic is kept off both LANs.

| Link | Host iface (renamed) | Host IP /30 | QNAP iface | QNAP IP /30 | MTU |
|---|---|---|---|---|---|
| **L1** ai-node1 ↔ QNAP **TB#1** | `en05` | `10.55.0.1` | T2E port A | `10.55.0.2` | 4000 |
| **L2** ai-node2 ↔ QNAP **TB#2** | `en05` | `10.55.0.5` | T2E port B ⚠ | `10.55.0.6` | 4000 |
| **L3** ai-node3 ↔ QNAP **10GbE** | `enstor` | `10.55.0.9` | 10GbE (eth) | `10.55.0.10` | 1500 → 9000* |

\* Node3 link is now a **Thunderbolt→10GbE** adapter (Ubiquiti UACC, AQC113/`atlantic`, 10 Gbps),
DIRECT to QNAP eth1 — superseding the earlier temporary USB→2.5GbE adapter. The adapter needs the
Thunderbolt/USB4 PCIe-tunnel boot params to enumerate — codified in `ansible/host_vars/ai-node3.yml`
(`pve_grub_cmdline_linux_default`).

\*\* **Measured (fio, 2026-06-17):** node3 **write 1171 MB/s** (full 10G, ~4× the old 2.5GbE);
**read ~300 MB/s** (≈ old 2.5G) — asymmetric. `irqbalance` (via `pve_base`) is enabled; **jumbo MTU 9000
was tested and REVERTED to 1500** (it gave no benefit on the production mount). The production read is
~300 MB/s regardless of MTU because: (a) the PVE NFS mount uses the QNAP **service IP** (`10.55.0.254`) over a *single* NFSv4.0 TCP
connection the Linux client pins at MSS 1448 (one transport per server) → all read RX on one queue/core;
(b) even tuned to the max (mount via the direct eth1 IP `10.55.1.254` → jumbo MSS 8948 + `nconnect=8`,
9 conns) reads only reach ~450–600 MB/s — the **QNAP eth1 TX side** is the real ceiling (its eth1 RX is
fast: writes hit 1171). So node3 reads can't saturate 10G; the lever that helps is **`nconnect`** on the
`qnap-nfs` mount (cluster-wide remount). Impact is low: writes are full 10G and host NFS reads are rare
(k8s CSI is a separate Talos-mounted path; cp3 is the affinity-steered slow tier).

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
| `ai-node3` | 192.168.0.4 | Thunderbolt→10GbE → QNAP eth1 (direct) | `enp99s0` | AQC113 (`atlantic`); TB boot params req'd |

The QNAP enumerates over Thunderbolt on both TB nodes (`thunderbolt 1-2: … Intel Corp. ai-storage`)
and a `thunderbolt-net` netdev already exists — so Linux↔QNAP T2E is viable; we just assign IPs.

## Firewall / routing notes

- No routing needed *between* the storage /30s (hub-spoke; each node only talks to the QNAP).
- Keep NFS/iSCSI bound to the storage interfaces on the QNAP where possible.
- Management (SSH/API) stays on `192.168.0.x` / `192.168.1.x`.
