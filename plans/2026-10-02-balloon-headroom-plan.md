# Balloon headroom on ai-node1/2/3

## Context

dev-worker-1 (ai-node1) was swapping on 2026-10-02: 12 GiB visible, swap 3967/4095 MiB, CPU pressure
32%. Its balloon floor was raised live to its 16 GiB ceiling (`qm set 4201 --balloon 16384` + `qm
monitor` → `balloon 16384`), which is as far as a running VM can go: it booted without `hotplug:
memory`/`numa: 1` and with `cores: 8` and no `vcpus`, so the ceiling and the vCPU count are fixed
until a power cycle. The real question was why ballooning never gives any guest headroom.

### How PVE auto-ballooning decides (pve-manager 9.2.2, read from the installed source)

`pvestatd::auto_ballooning` steers host usage (`MemTotal - MemAvailable`) to the node's
`ballooning-target` (default 80%, a PVE 9 node option, unset here) and moves each VM by at most 100 MiB
per 10 s cycle. `PVE::AutoBalloon::compute_alg1` grows guests **under memory pressure first** (guest
free memory <= 25% of the floor) and only when none can grow does it grow every ballooned VM toward its
ceiling. It splits the goal by `shares` (default 1000). When the host goes above the target it shrinks the
guests with the most free memory first, never below the floor.

So a host sitting at 80% is the designed equilibrium, not a fault. The useful measure is the **pool**:
how far ballooned guests sit above their floors (`qm status <vmid> --verbose` `balloon` minus the
`balloon:` floor). The pool is what is left of the 80% after fixed-size VMs, LXCs, the host and every
floor are paid for.

### Measured 2026-10-02

| | node1 | node2 | node3 |
|---|---|---|---|
| Pool (Σ balloon above floor) | ~0 (every guest at its floor) | ~14 GiB | ~13 GiB |
| amdgpu TTM page pool (`/sys/kernel/debug/ttm/page_pool`) | 15 MiB | **23.1 GiB** | **23.0 GiB** |
| Fixed-size VM RSS | 39 GiB | 27 GiB | 39 GiB |
| KSM saved (`run`) | 8 GiB (0) | 13 GiB (0) | 4 GiB (0) |

- **TTM page pool.** After llama-swap idle-unloads qwen3.8-27b (22.45 GiB), its freed GTT pages stay in
  TTM's pool (cap: half of RAM). The pool is not in `MemAvailable`, so PVE counts it as used. Full
  analysis: `docs/runbooks/ai-host-setup.md` → *TTM page pool cap*.
- **KSM flapping.** `ksmtuned` stops KSM above 20% free, which is the same line pvestatd steers to, so
  it was off on all three nodes.
- **node1 is plain over-commitment.** Fixed VMs cp1 24 + agent-node-1 16, floors 3×10 (runners) + 16
  (dw1) + 12 (dw3), two LXCs and the host overhead exceed 80% before any guest grows.
- **Fixed VMs are oversized against their 14-day peak** (Prometheus, `MemTotal - MemAvailable`):
  agent-node-1/2/3 16 GiB each, peak 3.3/2.6/2.2 GiB, k8s requests <= 2.3 GiB; reviewer-1/2 4 GiB
  each, peak 0.8/0.7. A VM without a balloon device never gives back memory it has touched:
  agent-node-1 holds 15.1 GiB of host RSS.
- **Not levers:** Talos CPs (cp1 is 95% requested by pods); runner count (each of the 8 was busy ~160 h
  of 336 h, queue wait p50 264 s / p95 1695 s).

## Phase 1 — live, no reboots (items 1-3 done 2026-10-02; item 4 completes after the merge)

1. **TTM pool capped at 1 GiB on all three hosts:** `page_pool_size` set at runtime, drained through
   debugfs `page_pool_shrink`, and `ttm.page_pool_size=262144` added to GRUB. node2 `MemAvailable`
   went from 24.8 to 47.0 GiB and node3 from 24.1 to 45.9 GiB.
2. **`KSM_THRES_COEF=40`** in `/etc/ksmtuned.conf` on all three. `run=1` everywhere after one interval.
3. **`shares: 3000`** on dev-workers 4201/4202/4204/4205, outside tofu (bpg has no attribute for it).
   See `docs/runbooks/dev-workers.md`.
4. **ai-llm-1 (ctid 5001, .44) retired.** It had served nothing since 2026-09-16. This PR removes it
   from `ai-lxc` and from the `ai-llm-node` scrape endpoints first; `pct stop` + `tofu apply` follow
   the merge, so the target disappears before the container does. `.44` stays RESERVED in
   `docs/network-plan.md` until the destroy has run, then a follow-up PR frees it.

Verified on a fresh cycle (node3, 2026-10-02): a cold qwen3.8-27b load took 70 s (23.8 GiB of GTT)
and after `/unload` the pool held 261912 pages (<= the 262144 cap), with `MemAvailable` back to
45.1 GiB. Before the cap, the same unload left ~23 GiB in the pool.

## Phase 2 — rolling worker reboots, one at a time (no control planes)

5. Talos agent nodes 16 → 10 GiB (`kubernetes/infra/agent-nodes/`): drain, `talosctl shutdown`, `qm set`,
   start. 10 rather than 8 so agent-node-1's 8.6 GiB of summed pod limits still fits allocatable.
   Worth about 6 GiB of worst case per node; on node1 also ~5 GiB of RSS right away.
6. reviewer-1/2 (out-of-band, node3) 4 → 2 GiB.

## Phase 3 — once node1 has a pool

7. Drop the dw1/dw3 floor overrides (`memory_floating_mib` in `kubernetes/infra/dev-workers/variables.tf`
   says to delete them once node capacity is fixed). With `shares: 3000` pvestatd keeps the workers
   near their ceiling anyway. dev-worker-1's floor is 16384 live and 12288 in code until then, so a
   `tofu apply` of that module puts it back to 12 GiB.
8. Optional: `ballooning-target: 85` on node1 only (no model host since ai-llm-1 is gone). Keep 80 on
   node2/node3: the 20% gap absorbs a 22.45 GiB model load faster than pvestatd can shrink guests.
