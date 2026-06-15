# ADR 0011 — k8s CSI over Thunderbolt via host-router + SNAT (Approach A1)

**Status:** ACCEPTED (2026-06-15) — IaC build-ready + canary-validated; **CSI cutover DEFERRED** until a
measured I/O bottleneck. **Supersedes** ADR 0003's "/30 point-to-point" topology claim (see below).
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

## The deferred cutover (trigger = a measured I/O bottleneck; do in a maintenance window)
1. `storage_router_enabled: true` in group_vars; `just router` (applies SNAT on all 3 hosts).
2. Roll the Talos route to all 3 CPs **one at a time** — `tofu apply -target='talos_machine_configuration_apply.cp["cpN"]'` (the `for_each` would otherwise hit all 3 at once → etcd-quorum risk); verify each Ready + etcd healthy between.
3. Flip `nfs-csi` StorageClass `server` and the `qnap-iscsi` backend `storageAddress` `192.168.1.225 → 10.55.0.254`.
4. **Migrate the existing Prometheus iSCSI PV** (portal baked at .225, reclaim Retain) via the STS surgery (scale prometheus-operator to 0, delete STS + PVC, scale back). NFS PVCs remount cleanly (same fsid).
5. Use **NFSv4.1/4.2 over TCP** (NFSv3 aux-RPC is NAT-fragile); confirm the **iSCSI target advertises the `10.55.0.254` portal** (NAT won't rewrite the login payload); keep **MTU 1500** (jumbo deferred — clamp MSS if later enabled).
6. Deploy a **storage health-check** (blackbox-exporter Probe to `10.55.0.254:2049`+`:3260` from the cluster + a PrometheusRule) — a dropped TB route/SNAT leaves a node `Ready` but storage hung.
7. node3 stays ~2.35 Gbps (USB) — affinity perf-sensitive workloads (Prometheus) to node1/2. Keep `192.168.1.225` as the documented quick-revert.

## Consequences
- ~4–5× CSI throughput ceiling on the 2 TB nodes once cut over; **no steady RAM/CPU cost** (just routing).
- A new failure MODE after cutover: a node's CSI I/O depends on its host's forward+SNAT path (mitigated by
  the self-heal timer + the health-check + the mgmt-LAN fallback).
- IaC stays rebuildable; the host bridge is **not** needed (A1 uses the raw routed `thunderbolt0`).
