# Scale the self-hosted GitHub Actions runner pool from 3 → 6

## Context
The lab runs an ephemeral self-hosted runner pool (`self-hosted-hv`) for `cchifor/platform` CI:
3 Ubuntu 24.04 QEMU VMs (`gha-runner-1/2/3`), one per Proxmox host, provisioned by the OpenTofu
module `kubernetes/infra/runners/` and configured by the Ansible role `github_runner` (`just runners`).
See ADR 0013. We want **6 runners total** — 3 more, with the **same resource allocation and
configuration** as the existing three — to increase CI concurrency (the platform workload is heavy
Docker-Compose e2e + Buildx + Playwright + k6, and jobs currently queue on a 3-wide pool).

The existing module was already built for this: the VM resource is `for_each = var.runner_nodes`, and
every VM draws its sizing from the **shared** scalar vars (`runner_cores`, `runner_memory_mib`,
`runner_memory_floating_mib`, `runner_rootfs_gb`). So "same config for all" is guaranteed structurally —
adding runners is purely additive map entries; `main.tf` does not change.

## Key decision — placement (2 runners per host)
There are only **3 physical Proxmox nodes** (`ai-node1/2/3`). Six identically-sized runners therefore
means **two per host** — one additional runner co-located on each node. This is the only interpretation
consistent with "6 total, same allocation for all" absent new hardware.

Trade-off (documented, not blocking): the original design was "one runner per host for fault isolation".
Two-per-host means a host failure removes 2 runners instead of 1, and raises steady-state RAM commitment
per host. Mitigations already in the design carry over unchanged: 12 GiB balloon floor (prevents the #620
OOM-under-pressure), 8 GiB guest swap (`swappiness=10`), and the `ci-runner-node` Prometheus scrape.

## Approach
Add `gha-runner-4/5/6` as a second runner on each node, **identical** to `1/2/3`:

| New VM | Host | `vm_id` | IP | Sizing (shared vars, unchanged) |
|---|---|---|---|---|
| `gha-runner-4` | ai-node1 | 4104 | 192.168.0.33 | 8 vCPU / 24 GiB ceiling / 12 GiB floor / 120 GiB |
| `gha-runner-5` | ai-node2 | 4105 | 192.168.0.34 | " |
| `gha-runner-6` | ai-node3 | 4106 | 192.168.0.35 | " |

- **`vm_id` 4104–4106** are free (Talos 4001–4003, runners 4101–4103, dev-workers 4201–4203, AI LXC
  5001–5003, registry 5004 — the 41xx runner series continues cleanly).
- **IPs `.33/.34/.35`** are free static addresses inside the reserved `.2`–`.50` block, just below the
  registry (`.36`). They are **not** in the router DHCP pool (`.51`+) — using `.50/.51/.52` would repeat
  the exact collision that displaced the AI LXCs off `.51`–`.53`. The three sit contiguous for tidiness;
  `.50` stays free. (`docs/network-plan.md` is the authoritative allocation table.)
- **No new secrets, no GitHub-side change.** All 6 share the one GitHub App (`ailab-ci-runners`, App
  4070577 / installation 140722927) and its SOPS key. The ephemeral wrapper derives the runner name from
  `hostname`, so each new VM self-registers as `ephem-gha-runner-{4,5,6}-<epoch>-<8hex>` into
  `self-hosted-hv` with zero config change. The App's `Administration: read/write` already mints
  registration tokens for any number of repo runners; platform's `runner-health.yml` canary passes
  because config is byte-identical.

### Edits (declarative only; all reversible, in-repo)
1. **`kubernetes/infra/runners/variables.tf`** — add the 3 entries to the `runner_nodes` map `default`;
   update the header comment (currently "one runner VM per physical host" / lists `.47`–`.49`, 4101–4103)
   to describe 2-per-host and the new IPs/vmids. Leave all sizing vars untouched.
2. **`inventory/hosts.yml`** — add `gha-runner-4/5/6` (`ansible_host` `.33/.34/.35`) to the
   `github_runners` group.
3. **`kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml`** — add `.33/.34/.35` to the
   `Endpoints.addresses`; update the "3 … .47/.48/.49" header comment to 6 + the full IP list.
4. **`docs/network-plan.md`** — add a `gha-runner-4/5/6 → .33/.34/.35` row to the static-allocation
   table; correct the free-space note (`.5–.36` + `.50` → `.5–.32` + `.50`) and add the missing
   `.36 = ai-registry` row it currently omits (needed so the "`.33`–`.35` free" claim is verifiable).
5. **`docs/runbooks/ci-runners.md`** — "3 ephemeral runner VMs" → 6; update the VMs table row (vmid
   4101–4106, IPs, "two per host"); update the verify step to expect `ephem-gha-runner-{1..6}`.
6. **`docs/decisions/0013-ci-self-hosted-runners.md`** — append a dated `Update (2026-07-01)` note (same
   style as the existing #620 note) recording the 3→6 scale, the 2-per-host placement, the retained
   fault-isolation trade-off, and the capacity gate below. (A full new ADR is unwarranted — same module,
   role, and pool; incremental scale, not a new architectural decision.)

`kubernetes/infra/runners/main.tf`, the `github_runner` role, `group_vars/github_runners.yml`, and the
SOPS secret are **unchanged** by design.

## Capacity gate (the real risk — validate before apply)
ADR 0013 records the hosts at **79–85 % RAM** with one runner each (12 GiB floor ≈ 33 GiB committed
across the 3 guests) alongside Talos + the AI LLM LXC. Adding a second runner per host commits another
~12 GiB floor/host and, under simultaneous CI peaks, both guests inflate toward the 24 GiB ceiling.
This is feasible but materially tighter than 1-per-host. **Before `tofu apply`, verify per-host headroom**
so we don't reintroduce #620:

```bash
for h in 2 3 4; do ssh root@192.168.0.$h 'hostname; free -g | awk "/Mem:/{print \$2\" GiB total, \"\$7\" GiB avail\"}"'; done
```

Require ≥ ~14 GiB available headroom per host at idle (one runner's floor + margin). If a host is short,
options in order of preference: (a) trim the AI LLM LXC ceiling on that host, (b) stagger — bring the new
runners up one at a time and watch `node_memory_MemAvailable` on the CI-runner dashboard, (c) as a last
resort accept balloon-reclaim + guest swap under peak (the #620 mitigations are designed for exactly this).
Do **not** lower the shared 12 GiB floor or 24 GiB ceiling — that would violate "same config" and re-open #620.

## Verification (end-to-end, operator-run — outward-facing, hard to reverse)
These steps create real VMs and register real runners; run them deliberately after the capacity gate.
```bash
cd kubernetes/infra/runners
tofu plan          # EXPECT: 3 VMs to add (gha-runner-4/5/6), 0 to change/destroy on 1/2/3
tofu apply         # creates the 3 new VMs (image already downloaded)
tofu output runner_vms   # 6 entries
just ping-runners  # SSH reachability for all 6 (ansible_user=ubuntu)
just runners       # idempotent: installs the toolchain + ephemeral contract on 4/5/6, re-asserts 1/2/3
```
Then confirm:
- **GitHub** → `cchifor/platform` → Settings → Actions → Runners → `self-hosted-hv` shows `Idle`
  `ephem-gha-runner-{4,5,6}-…` alongside the existing three (6 total).
- **Canary** → Actions → "Runner pool health" — re-run until it lands on a new host; asserts the
  `ephem-*` name, `MemoryMax=10G`, wrapper, hook env.
- **Metrics** → Prometheus shows **6** `job=ci-runner-node` targets up (`.33/.34/.35` + `.47/.48/.49`).
- **Host RAM** → the CI-runner dashboard's `node_memory_MemAvailable` on each host stays healthy under a
  real PR's e2e run (no exit-137).

<!-- codex-review-status: pending -->
