# ADR 0009 — Control-plane colocation hardening

**Status:** ACCEPTED (2026-06-15) · **Date:** 2026-06-15
**Relates to:** ADR 0006 (Talos/Flux/Cilium), ADR 0008 (AI LXC RAM footprint).

## Context
The 3 Talos control planes are **untainted** (`allowSchedulingOnControlPlanes: true`) — every workload
shares the 3 nodes with the host-level **etcd** service (etcd runs as a Talos *service*, not a pod). With
only 3 nodes we can't add dedicated workers, so before hosting a microservices platform we must stop a
noisy workload from starving etcd/kubelet. Audit found: no kubelet reservations, no PriorityClasses, no
LimitRange/ResourceQuota. Current load is light (memory requests 4–9% of allocatable), so this is
preventive headroom, not a fix for present pressure.

## Decision — built-in primitives only, no new operators
1. **Kubelet reservations (the only lever that protects the host etcd service).** Talos machine-config
   `machine.kubelet.extraConfig`: `kubeReserved` + `systemReserved` `{cpu 500m, memory 1Gi, eph 1Gi}`
   each (system bumped because etcd draws from the system slice), `evictionHard
   {memory.available 5%, nodefs/imagefs 10%}`. Reduces Allocatable ~31.5 → ~28 GiB/node (accounting;
   `enforceNodeAllocatable` stays `["pods"]` so etcd is never cgroup-killed). Applied via tofu — a
   kubelet-config change is a **kubelet restart, not a reboot**; rolled **one CP at a time** with
   etcd-quorum checks (the `talos.tf` `for_each` would otherwise hit all 3 at once → use `-target`).
2. **PriorityClasses** (`infrastructure/core/`, wired first): `platform-critical` (1e6),
   `platform-normal` (1e5, `globalDefault`), `platform-low` (1e3, `preemptionPolicy: Never`) — all below
   the built-in `system-*`. Critical assigned to monitoring (prometheus/alertmanager/grafana/loki/alloy),
   CSI (csi-driver-nfs), and cloudflared; everything else inherits the global default.
3. **LimitRange per workload namespace** (ai, edge, monitoring): `default`/`defaultRequest` only — **no
   `max`/`min`**, so a LimitRange can never *reject* a pod (avoids silently blocking privileged CSI
   DaemonSet pods). It backfills requests/limits onto anything that omits them.
4. **ResourceQuota: deferred.** Hard per-namespace caps are a follow-up; if added, **never on
   `trident`/`local-path-storage`/`kube-system`** (a tight quota there blocks CSI node DaemonSets and
   breaks all attach). Kyverno "require requests" is an optional later stretch — LimitRange default
   injection already closes the unbounded-pod gap.

## The RAM ceiling (hard consequence)
~128 GiB/host = **64 GiB iGPU VRAM carve** (firmware, not cgroup-charged) + **32 GiB CP VM** + **~24 GiB
AI LXC cap** (but the Vulkan model lives in the VRAM heap, so the LXC uses ~0.5 GiB system RAM at steady
state). The CP VM **cannot grow past 32 GiB without shrinking the AI footprint**, and Talos has no memory
hotplug. After fencing, each node exposes ~28 GiB Allocatable. So the platform's always-on stateful
budget is **finite (~84 GiB across 3 nodes minus current monitoring/AI/edge)** — every new stateful
service must carry a request/limit and (eventually) a quota slot. This is why admission governance
matters more here than on an elastic cloud cluster.

## Consequences
- A workload memory spike now hits `evictionHard` and evicts low/normal-priority pods before the node
  runs the kubelet/etcd out of memory; `platform-critical` infra survives longest.
- Slightly less schedulable RAM (~28 vs ~31.5 GiB/node) — immaterial at current 4–9% load.
- Flux controllers (`gotk-components.yaml`) are intentionally left untouched (already
  `system-cluster-critical`, own quota); `flux-system` is excluded from the LimitRanges.
