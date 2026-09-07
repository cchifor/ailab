# Network plan (authoritative)

This file is the human-readable source of truth; the machine-readable copy lives in
`inventory/hosts.yml` (consumed by Ansible) and `tofu/terraform.tfvars` (consumed by OpenTofu).
Keep them in sync.

## Existing networks

| Network | Purpose | Notes |
|---|---|---|
| `192.168.0.0/24` | Proxmox management LAN | nodes `.2/.3/.4`, UI `https://192.168.0.2:8006` |
| `192.168.1.0/24` | QNAP / general LAN | QNAP mgmt `ai-storage` = `192.168.1.225` (routed from `.0.x`) |

### Management LAN `192.168.0.0/24` — static allocations (IPAM registry)

**This table is the IPAM registry. Verified against the live estate 2026-09-03** (every ailab
`ipconfig0`/`net0`, plus an ARP/ping sweep of the segment). Allocate from the `free` rows ONLY, and
update this table in the SAME change that allocates the address.

Static reservations are `.2`–`.50`; the **router DHCP pool starts at `.51`** (`.51`–`.254`).

> ⚠️ **This LAN is SHARED with the `cloudlab` GPU cluster** (separate Proxmox cluster, separate repo
> `../cloudlab`). Its hosts and LXCs hold `.20`–`.22` and `.26`–`.28` and are **invisible to ailab's
> tooling** — they are not in any ailab config, so an ailab-only scan will report them as free. They
> are NOT. Check both repos before allocating.

| Range / IP | Owner | vmid | Source of truth |
|---|---|---|---|
| `.1` | LAN gateway | — | router |
| `.2 / .3 / .4` | Proxmox hosts `ai-node1/2/3` | — | `inventory/hosts.yml` |
| `.5 / .6 / .7` | **free (static)** | — | — |
| `.8`–`.13` | Dev-worker VMs `dev-worker-1..6` (2 per node) | 4201–4206 | `kubernetes/infra/dev-workers/variables.tf` |
| `.14`–`.18` | CI runner VMs `ci-runner-1..5` (GitHub + Gitea Actions) | 4101–4105 | `kubernetes/infra/runners/variables.tf` |
| `.19` | CI runner VM `ci-runner-6` | 4106 | `kubernetes/infra/runners/variables.tf` |
| `.20 / .21 / .22` | 🔒 **cloudlab** GPU hosts `cloud1/2/3` (bare metal, static) | — | `../cloudlab/inventory/hosts.yml` |
| `.23` | CI runner VM `ci-runner-10` | 4110 | `kubernetes/infra/runners/variables.tf` |
| `.24 / .25` | Reviewer VMs `reviewer-1/2` | 4501–4502 | ⚠️ unmanaged — see note below |
| `.26` | 🔒 **cloudlab** LXC `cloud-llm-3` | 5101 | `../cloudlab/README.md` |
| `.27 / .28` | 🔒 **cloudlab** LXCs `cloud-exec-1/2` | 5102–5103 | `../cloudlab/kubernetes/infra/executor-lxc/variables.tf` |
| `.29 / .30 / .31` | CI runner VMs `ci-runner-7/8/9` (moved off `.20`–`.22` on 2026-09-03; `ci-runner-9` moved ai-node2→ai-node1 on 2026-09-07) | 4107–4109 | `kubernetes/infra/runners/variables.tf` |
| `.32`–`.35` | **free (static)** | — | — |
| `.36` | OCI registry LXC `ai-registry` | 5004 | `kubernetes/infra/registry-lxc/variables.tf` |
| `.37` | Talos env-node `talos-env-node-1` (k8s member) | 4401 | ⚠️ unmanaged — see note below |
| `.38 / .39` | **free (static)** | — | — |
| `.40` | Talos control-plane VIP (k8s API `:6443`) | — | `kubernetes/infra/variables.tf` |
| `.41 / .42 / .43` | Talos control-plane VMs `talos-cp1/2/3` | 4001–4003 | `kubernetes/infra/variables.tf` |
| `.44 / .45 / .46` | AI LLM LXCs `ai-llm-1/2/3` | 5001–5003 | `kubernetes/infra/ai-lxc/variables.tf` |
| `.47 / .48 / .49` | Talos agent-node VMs `agent-node-1/2/3` (ADR 0019) | 4301–4303 | `kubernetes/infra/agent-nodes/variables.tf` |
| `.50` | **free (static)** | — | — |
| `.51`–`.254` | router DHCP pool | — | router |

**Free static space: `.5`–`.7`, `.32`–`.35`, `.38`, `.39`, `.50`** (10 addresses). Nothing else in
`.2`–`.50` is available.

> **Keep all lab static IPs inside `.2`–`.50`.** The DHCP pool starts at `.51`, so anything `.51`+
> can be leased to a random client — exactly the collision that pushed the AI LXCs off `.51`–`.53`
> into the static block (`kubernetes/infra/ai-lxc/variables.tf`).

#### ⚠️ VMs not managed by OpenTofu

`ci-runner-6..10` (4106–4110), `reviewer-1/2` (4501–4502) and `talos-env-node-1` (4401) are **running
but tracked in no tofu state**. They were created out-of-band, so `tofu plan` cannot see their
addresses and will not warn when a managed module is given one of them. Until they are imported,
this table is their only record — treat it as load-bearing.

#### ⚠️ `ipconfig0` drift is invisible to `tofu plan`

Both VM modules set `lifecycle { ignore_changes = [initialization] }`, because cloud-init only
applies at first boot and the live IPs were later renumbered **in-guest** (netplan). The consequence
bit this estate on 2026-09-03: the runners/dev-workers were renumbered in-guest to `.8`–`.18` and the
tofu variables were updated to match, but the Proxmox `ipconfig0` metadata still held the ORIGINAL
allocation (`.47/.48/.49`, `.33/.34`, `.37`–`.39`, `.5`–`.7`). Those addresses were then handed to the
ADR 0019 agent-nodes and to `talos-env-node-1` — so any reboot of a runner or dev-worker would have
re-applied cloud-init and collided with a live Talos k8s member. `ipconfig0` was corrected to match
the live/declared IPs on 2026-09-03.

**When you renumber a guest in-place, change it in THREE places:** the guest (netplan), the tofu
variable, and the Proxmox `ipconfig0` (`qm set <vmid> --ipconfig0 gw=…,ip=…/24` then
`qm cloudinit update <vmid>`). Verify with:

```sh
# every declared ailab address (VM + LXC), duplicates flagged.
# Run on any ai-node: /etc/pve is cluster-shared.
pvesh get /cluster/resources --type vm --output-format json |
  python3 -c "import sys,json;[print(r['vmid'],r.get('name')) for r in json.load(sys.stdin)]"

# declared vs ACTUALLY running (the drift that caused the 2026-09-03 collisions)
qm config <vmid> | grep ipconfig0      # what cloud-init will apply on next boot
qm agent <vmid> network-get-interfaces # what the guest is using right now

# remember the OTHER cluster on this LAN
grep -rn ip= ../cloudlab/kubernetes/infra/*/variables.tf ../cloudlab/inventory/hosts.yml
```

## Kubernetes cluster networks (Talos + Cilium)

These are **internal to the cluster** and never appear on the LAN. Both are set at Talos cluster
creation and cannot be changed without rebuilding the cluster.

| Network | Purpose | Source of truth |
|---|---|---|
| `10.244.0.0/16` | **Pod** CIDR (`kube-controller-manager --cluster-cidr`) | Talos machine config |
| `10.96.0.0/12` | **Service / ClusterIP** CIDR (`kube-apiserver --service-cluster-ip-range`) | Talos machine config |

### Reserved broker ClusterIP range

`10.96.0.0/12` spans **`10.96.0.0`–`10.111.255.255`**. Every Service ClusterIP in the cluster —
including the AgentForge broker Services — is allocated from it by the apiserver.

> **Do not hand out a broker ClusterIP by picking one.** A few broker Services **pin** their
> ClusterIP (`spec.clusterIP`) so the address survives a Service delete/recreate. A pinned address
> must (a) fall inside `10.96.0.0/12` — the apiserver rejects anything outside it — and (b) not
> collide with an address already allocated, which the apiserver also rejects, leaving the Service
> uncreated and the broker unreachable.
>
> The safe procedure is: create the Service **without** `clusterIP`, let the apiserver allocate,
> then pin the address it chose. Never invent one.
>
> **Exception**: CP-managed adds (Settings → Subscriptions → Add account) allocate from the
> operator-reserved `AFP_BROKER_CLUSTERIP_POOL` (currently `10.96.0.192/26`, inside the KEP-3070
> static band the dynamic allocator prefers not to use) and pin that chosen address directly — see
> `docs/runbooks/agentforge-platform-activation.md` (Day-2 — LLM subscriptions operations) for the
> KEP-3070 fallback caveat (the static band is a preference, not an absolute reservation) and how
> a collision would surface (failed `kubectl apply`, not a silent outage).
>
> Both rules are machine-checked. `scripts/gen-broker-inventory.py` fails if a pinned broker
> ClusterIP falls outside the CIDR or is pinned twice, and the current allocation is listed in the
> generated `kubernetes/apps/infrastructure/agentforge-broker/broker-inventory.yaml`. Check that
> file (and `kubectl -n agentforge-broker get svc`) before pinning a new one.

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
