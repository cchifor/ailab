# ADR 0011 — k8s CSI over Thunderbolt via host-router + SNAT (Approach A1)

**Status:** ACCEPTED + **CUTOVER EXECUTED** (2026-06-15) — both nfs-csi and qnap-iscsi now ride the
Thunderbolt fabric (`10.55.0.254`). **Supersedes** ADR 0003's "/30 point-to-point" topology claim (below).
**Relates to:** ADR 0007 (k8s storage), the QNAP iSCSI CSI work.

## Context
k8s CSI (`nfs-csi` + `qnap-iscsi`) talks to the QNAP over the **2.5 GbE mgmt LAN** (`192.168.1.225`),
leaving the ~1.1 GB/s Thunderbolt fabric (`10.55.0.0/24`, QNAP `10.55.0.254`) unused by the VMs. We want
CSI on TB **without** giving the Talos VMs a 2nd NIC (the `deviceSelector: driver: virtio_net` becomes
ambiguous, and a mis-merge could move the control-plane VIP). Decided after two multi-agent reviews + a
codex cross-review, all of which independently picked A1.

### Topology correction (supersedes ADR 0003 / network-plan.md)
ADR 0003 + `docs/network-plan.md` describe **/30 point-to-point** links and were **never realized**. Live,
SSH-verified reality (the source of truth is `inventory/hosts.yml`): the QNAP **bridges both TB ports into
`tbtbr0`**, making `10.55.0.0/24` a **single shared L2** for node1 (`10.55.0.1`) + node2 (`10.55.0.2`),
both reaching `10.55.0.254` directly. **node3 has no TB** — it is on a separate `10.55.1.0/24` via a
USB→2.5 GbE adapter (`10.55.1.9`) and routes to `10.55.0.254` through `10.55.1.254`. ADR 0003's rationale
for keeping CSI on the mgmt LAN ("/30s, no shared L2") is therefore invalid.

## Decision — Approach A1: host-as-router + SNAT (VM keeps a single NIC, no passthrough)
The Proxmox host (already on the fabric) routes + **SNATs** the VM's storage traffic over `thunderbolt0`:
- **tofu:** per-node `host_ip` (the host's vmbr0 IP) on the `control_planes` map + a `storage_service_ip`
  var; the Talos machine-config gets an **additive route** `10.55.0.254/32 via <host_ip>` (single NIC,
  VIP + default route untouched).
- **Ansible `storage_router` role:** persistent `ip_forward` + an idempotent `iptables POSTROUTING SNAT`
  (`-s 192.168.0.0/24 -d 10.55.0.254 -o <storage_iface> -j SNAT --to-source <storage_ip>`), reconciled by
  a 30 s systemd timer (mirrors the existing `tb-link` self-heal). **SNAT is mandatory** — the QNAP has no
  return route to `192.168.0.0/24` and we keep **zero QNAP changes** (its exports already grant
  `10.55.0.0/24`+`10.55.1.0/24` rw). node3 SNATs via its USB path uniformly (per-host vars).
- Gated by `storage_router_enabled` (default **false**) — build-ready, not yet cut over.

### Validated (canary, 2026-06-15)
With the host SNAT on ai-node1 + the `/32` route surgically added to cp1 (JSON6902 append — VIP/default
route preserved, no reboot), a pod on cp1 reached the QNAP **NFS (2049) and iSCSI (3260)** through the
routed+SNAT path; the host's `POSTROUTING` SNAT counter incremented (traffic crossed `thunderbolt0`).
Canary then fully reverted — runtime unchanged, CSI still on mgmt. Feasibility proven.

## Rejected
- **A2/A3 — L2-bridge `thunderbolt0` + 2nd VM NIC:** depends on the **unproven** behaviour of
  thunderbolt-net forwarding a *bridged foreign MAC* to the QNAP `tbtbr0` (upstream Proxmox-TB setups
  route, not bridge); also moves the host's live storage IP onto a bridge and risks VIP drift. High blast.
- **B1 — proxmox-csi-plugin:** clean "Proxmox owns storage" model, but supplants the just-validated
  qnap-iscsi stack, needs a **privileged Proxmox API token in-cluster**, and is RWO-only. Reconsider only
  if we later want Proxmox to own storage end-to-end.
- **Longhorn / status-quo-forever:** see ADR 0010 / the throughput goal.

## Cutover — executed 2026-06-15 (what actually happened)
Pre-flight gate (verified before touching anything): the QNAP serves `:8080` (API), `:2049` (NFS),
`:3260` (iSCSI) on `10.55.0.254` (dual-stack listen), and all 3 nodes reach them through the SNAT.
1. **Host routers (all 3):** installed the persistent `storage_router` (sysctl `ip_forward` + idempotent
   SNAT + 30 s self-heal timer) via SSH. Hardened the script with `flock` + `iptables -w` after a
   timer/manual race double-added a rule on first deploy.
2. **Talos route → all 3 CPs, one at a time** (surgical JSON6902 append; VIP/default route preserved, no
   reboot; etcd healthy between each). Soak-tested: a pod on every node reached `:8080/:2049/:3260`.
3. **iSCSI flip — finding: `storageAddress` is IMMUTABLE on a live Trident backend** (update rejected
   *"invalid backend update"*; no outage — it kept running on .225). So the flip is **delete + recreate**
   the backend, which requires the backend to have **zero volumes** first. Emptied it by tearing down the
   Prometheus PV (STS surgery; ~hours-old TSDB, negligible loss), deleted the TBC, Flux recreated it on
   `.254` (new UUID, `Bound/Success` — proving the API works over the SNAT). Recreated Prometheus → fresh
   PVC provisioned + attached via **`10.55.0.254:3260`** (confirmed by `iscsiadm -m session`).
4. **NFS flip:** StorageClass `server → 10.55.0.254` (immutable param → delete + recreate the SC; existing
   Loki/Open-WebUI PVs keep their baked `.225` server, unaffected). **Finding: QuTS hero rejects NFSv4.1**
   (`mount.nfs: Protocol not supported`) — pinned to **`nfsvers=4.0`** (already single-port / NAT-friendly;
   the v3 fragility codex flagged never applied). Validated: 1 GiB direct write over the fabric ≈
   **660 MB/s** vs the ~280 MB/s 2.5 GbE ceiling.
5. **Prometheus pinned to TB:** Talos `nodeLabels ailab.io/storage-tier` (cp1/cp2=thunderbolt, cp3=ethernet,
   kept the `exclude-from-external-load-balancers` default) + a preferred `nodeAffinity` → Prometheus moved
   cp3→cp1 (RWO volume live-migrated, re-attached on `.254`).
6. **MTU 1500** kept (jumbo deferred). **`192.168.1.225` remains the documented quick-revert.**

## Remaining follow-up
- **Storage health-check:** a dropped TB route/SNAT leaves a node `Ready` but its CSI I/O hung. The 30 s
  self-heal timer is the primary mitigation; an *alert* still wants a **per-node** TCP probe to `.254`
  (a single blackbox Probe lands on one node and misses per-host SNAT failures — needs a DaemonSet-style
  check or kubelet-volume-stats staleness). Deferred.

## Consequences
- CSI now rides Thunderbolt: ~2.4× measured on the TB nodes (single-stream NFS; iSCSI similar), **no steady
  RAM/CPU cost** (just routing). node3 (USB, 2.5 GbE, separate /24) reaches `.254` over its slower link —
  fast-storage workloads are affinity-steered to cp1/cp2.
- New failure MODE: a node's CSI I/O depends on its host's forward+SNAT path (mitigated by the self-heal
  timer + the mgmt-LAN fallback; alerting is the open follow-up above).
- IaC stays rebuildable; the host bridge is **not** needed (A1 uses the raw routed `thunderbolt0`). NOTE:
  the live route/nodeLabels were applied via talosctl (tofu not installed here); the committed `.tftpl` is
  the source of truth — a future `tofu apply` converges (no-op).
