# Scale the self-hosted GitHub Actions runner pool from 3 → 6

## Context
The lab runs an ephemeral self-hosted runner pool (`self-hosted-hv`) for `cchifor/platform` CI:
3 Ubuntu 24.04 QEMU VMs (`gha-runner-1/2/3`), one per Proxmox host, provisioned by the OpenTofu
module `kubernetes/infra/runners/` and configured by the Ansible role `github_runner` (`just runners`).
See ADR 0013. We want **6 runners total** — 3 more, with the **same resource allocation and
configuration** as the existing three — to raise CI concurrency (the platform workload is heavy
Docker-Compose e2e + Buildx + Playwright + k6, and jobs currently queue on a 3-wide pool).

The existing module was already built for this: the VM resource is `for_each = var.runner_nodes`, and
every VM draws its sizing from the **shared** scalar vars (`runner_cores`, `runner_memory_mib`,
`runner_memory_floating_mib`, `runner_rootfs_gb`). So "same config for all" is guaranteed structurally —
adding runners is purely additive map entries.

Doubling pool width also roughly doubles pressure on shared dependencies the jobs touch (the Zot
registry, host storage/network, any lab services the e2e stack calls). That's acceptable, but it makes
**host RAM the binding constraint** and means the rollout must be gated on real capacity data and
validated under genuine concurrency — not just "runners registered" (see the Pre-apply gate).

## Key decision — placement (2 runners per host)
There are only **3 physical Proxmox nodes** (`ai-node1/2/3`). Six identically-sized runners therefore
means **two per host** — one additional runner co-located on each node. This is the only interpretation
consistent with "6 total, same allocation for all" absent new hardware.

Trade-off (documented, not blocking): the original design was "one runner per host for fault isolation".
Two-per-host means (a) a host failure removes 2 runners instead of 1, and (b) GitHub can now schedule
**two heavy jobs onto the same physical host** with no per-host concurrency guard — so capacity must be
validated against that worst case, not the average. The #620 mitigations (12 GiB balloon floor, 8 GiB
guest swap `swappiness=10`) protect each *guest* from being starved, but they do **not** by themselves
prove the *host* has enough RAM once a second 12 GiB floor is committed and both guests inflate toward
24 GiB. That proof is the Pre-apply gate below.

## Approach
Add `gha-runner-4/5/6` as a second runner on each node, **identical** to `1/2/3`:

| New VM | Host | `vm_id` | IP | Sizing (shared vars, unchanged) |
|---|---|---|---|---|
| `gha-runner-4` | ai-node1 | 4104 | 192.168.0.33 | 8 vCPU / 24 GiB ceiling / 12 GiB floor / 120 GiB |
| `gha-runner-5` | ai-node2 | 4105 | 192.168.0.34 | " |
| `gha-runner-6` | ai-node3 | 4106 | 192.168.0.35 | " |

- **`vm_id` 4104–4106** are free against the checked OpenTofu defaults (Talos 4001–4003, runners
  4101–4103, dev-workers 4201–4203, AI LXC 5001–5003, registry 5004 — the 41xx runner series continues
  cleanly). This is confirmed against repo state; the Pre-apply gate re-confirms against **live** Proxmox.
- **IPs `.33/.34/.35`** are free static addresses inside the reserved `.2`–`.50` block, just below the
  registry (`.36`), per `docs/network-plan.md` (authoritative) + `inventory/hosts.yml`. They are **not**
  in the router DHCP pool (`.51`+) — using `.50/.51/.52` would repeat the exact collision that displaced
  the AI LXCs off `.51`–`.53`. The three sit contiguous for tidiness; `.50` stays free. The Pre-apply gate
  re-confirms they are unused on the **live** LAN (ping/ARP/lease).
- **No new secret value; unchanged GitHub side.** All 6 share the one GitHub App (`ailab-ci-runners`,
  App 4070577 / installation 140722927) and its SOPS key. The ephemeral wrapper derives the runner name
  from `hostname`, so each new VM self-registers as `ephem-gha-runner-{4,5,6}-<epoch>-<8hex>` into
  `self-hosted-hv` with zero config change. The App's `Administration: read/write` mints registration
  tokens for any number of repo runners.
  - *Blast-radius note (security):* the existing App private key is now copied to **3 more** job-running
    VMs at `/etc/runner/app.pem` (`0400`, owned by `runner`). Jobs execute **as** `runner`, so this
    widens the surface by which a malicious job could exfiltrate the key — but that surface already exists
    on runners 1–3 and is accepted under ADR 0013 (both repos private, contributors trusted). Confirm the
    App remains installed on **only** `cchifor/platform` (least privilege); the key rotation runbook is
    unchanged.

### Edits (declarative only; all reversible, in-repo)
1. **`kubernetes/infra/runners/variables.tf`** — add the 3 entries to the `runner_nodes` map `default`;
   update the header comment (currently "one runner VM per physical host" / lists `.47`–`.49`, 4101–4103)
   to describe 2-per-host and the new IPs/vmids. Leave all sizing vars untouched.
2. **`kubernetes/infra/runners/main.tf`** — **comment-only**: the file header still says "one per Proxmox
   node"; reword to "two per Proxmox node (see variables.tf)". No resource/logic change.
3. **`inventory/hosts.yml`** — add `gha-runner-4/5/6` (`ansible_host` `.33/.34/.35`) to the
   `github_runners` group.
4. **`kubernetes/apps/infrastructure/monitoring/ci-runners-node.yaml`** — add `.33/.34/.35` to the
   `Endpoints.addresses`; update the "3 … .47/.48/.49" header comment to 6 + the full IP list. *(This
   manifest is Flux-managed and tracked on `main`; it only reaches the cluster after the change is merged
   and Flux reconciles — verified explicitly below.)*
5. **`docs/network-plan.md`** — add a `gha-runner-4/5/6 → .33/.34/.35` row to the static-allocation table;
   correct the free-space note (`.5–.36` + `.50` → `.5–.32` + `.50`) and add the missing `.36 = ai-registry`
   row it currently omits (so the "`.33`–`.35` free" claim is self-verifiable).
6. **`docs/runbooks/ci-runners.md`** — "3 ephemeral runner VMs" → 6; update the VMs table row (vmid
   4101–4106, IPs, "two per host"); update the verify step to expect `ephem-gha-runner-{1..6}`.
7. **`docs/decisions/0013-ci-self-hosted-runners.md`** — append a dated `Update (2026-07-01)` note (same
   style as the existing #620 note) recording the 3→6 scale, the 2-per-host placement, the retained
   fault-isolation trade-off, and the capacity gate. (A full new ADR is unwarranted — same module, role,
   and pool; incremental scale, not a new architectural decision.)
8. **`kubernetes/infra/ai-lxc/variables.tf`** — small adjacent cleanup: the header comment still narrates
   the abandoned `.51–.53` LXC IPs as if current; align it with the network plan so the `.51+`-is-DHCP
   rationale referenced above stays consistent. (Skip if it reads as out-of-scope at review.)

The `github_runner` role, `group_vars/github_runners.yml`, and the SOPS secret are **unchanged** — the
new runners are byte-identical in config to the existing three, which is what proves "same configuration".

## Pre-apply gate (hard go/no-go — the real risk lives here)
Repo state alone does **not** establish feasibility, and the tree carries a stale optimistic note
(`variables.tf`: "~119 GiB free each") that predates the dev-workers, the registry LXC, and the #620
finding that hosts already run at **79–85 % RAM** with a single runner each. So gate on **live data**, not
optimism, and abort if a host is short rather than reintroducing #620.

**(A) Live collision re-check (must be clean on every host):**
```bash
# VMIDs 4104-4106 must not exist anywhere in the cluster:
ssh root@192.168.0.2 'pvesh get /cluster/resources --type vm --output-format json' | \
  jq -r '.[].vmid' | sort -n | grep -E '^(4104|4105|4106)$' && echo "COLLISION" || echo "vmids free"
# IPs .33/.34/.35 must be dark on the live LAN (no ping, no ARP entry, no DHCP lease):
for o in 33 34 35; do ping -c1 -W1 192.168.0.$o >/dev/null 2>&1 && echo ".$o IN USE" || echo ".$o free"; done
```

**(B) Capacity budget — full per-host accounting, real numbers.** Each host today carries, *before* the
new runner: a **Talos CP VM (~32 GiB, non-ballooning)**, the **AI LLM LXC (24 GiB cap)**, a **dev-worker
VM (~16 GiB ceiling / ~2 GiB floor)**, one **gha-runner (24 GiB ceiling / 12 GiB floor)**, plus PVE/host
overhead and the **iGPU VRAM/GTT carve** (modest on the daily-driver model, but up to ~64 GiB when a
heavyweight LLM is loaded on-demand). `ai-node1` **also** hosts the **registry LXC**. Adding a second
runner commits **another ~12 GiB floor** and, at worst case, **+24 GiB ceiling** that can coincide with
the other guests' peaks. Collect the real picture (not rounded `free -g`):
```bash
for h in 2 3 4; do echo "== 192.168.0.$h =="; ssh root@192.168.0.$h '
  free -m | awk "/Mem:/{printf \"MemTotal=%dMiB MemAvailable=%dMiB\n\",\$2,\$7}";
  for id in $(qm list | awk "NR>1{print \$1}"); do echo -n "vm $id: "; qm config $id | grep -E "^(memory|balloon):" | tr "\n" " "; echo; done;
  for id in $(pct list | awk "NR>1{print \$1}"); do echo -n "ct $id: "; pct config $id | grep -E "^memory:"; done'
done
# Also read the trend, not a single sample: node_memory_MemAvailable_bytes per host on the CI-runner
# Grafana dashboard over a representative busy window.
```
**Go criterion:** each host must retain enough headroom that the new runner's **12 GiB floor** fits with
margin *and* a realistic concurrent peak (both local runners busy + the AI LXC at its cap + dev-worker
active) does not drive the host into sustained balloon-reclaim or swap. Idle `MemAvailable` ≳ 14 GiB is a
*necessary floor check, not sufficient* — weigh it against the ceiling sum above and the live balloon
state. **Balloon reclaim / guest swap under an expected peak is a DEGRADED condition, not an acceptable
steady state** (that is exactly #620); if the data shows a host would routinely land there, treat it as
**no-go** for that host.

**If a host is short (in order of preference):** (a) stagger — bring the new runners up **one host at a
time**, watch `MemAvailable` under a real e2e load before proceeding to the next; (b) trim the AI LLM LXC
ceiling on the tight host, or schedule heavyweight-LLM runs to not overlap peak CI; (c) pause and consult
before proceeding — do **not** lower the shared 12 GiB floor / 24 GiB ceiling (that would violate "same
config" and re-open #620). Record the go/no-go outcome in the ADR update.

## Apply & register (operator-run — outward-facing, hard to reverse)
Only after the gate passes. These create real VMs and register real runners.
```bash
cd kubernetes/infra/runners
tofu fmt -check && tofu validate      # static hygiene
tofu plan                             # EXPECT: 3 to add (gha-runner-4/5/6); 0 to change/destroy.
                                      # Scan the plan: NO diffs to gha-runner-1/2/3 disks or cloud-init.
tofu apply
tofu output runner_vms                # 6 entries
# Configure/register ONLY the new hosts first, so 1/2/3's in-flight jobs aren't restarted:
cd ../../.. && SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey \
  ansible-playbook ansible/runners.yml --limit 'gha-runner-4,gha-runner-5,gha-runner-6'
# (`just runners` runs the whole group and may bounce 1/2/3 — use it only during an idle window.)
```

## Verification (prove identical config + healthy under load)
- **Config identity (not just "shared vars"):** on the host, `qm config 4104` shows `memory: 24576` +
  `balloon: 12288` + `cores: 8` (and same for 4105/4106); in-guest, `free -h` shows the 8 GiB swap and
  `systemctl show -p MemoryMax actions.runner.cchifor-platform.service` is `10737418240` — i.e. 4/5/6
  match 1/2/3 exactly.
- **Registration (API, not just UI):** `gh api repos/cchifor/platform/actions/runners` lists **6** online
  `self-hosted-hv` runners and **no** growing backlog of offline `ephem-*` entries (registration-loop
  cleanup working).
- **Per-VM canary (deterministic, not luck):** confirm the `runner-health.yml` canary result covers each
  of 4/5/6 — check job logs / API for the host identity, or run a small matrix so every new VM executes at
  least once. (The canary lives in `cchifor/platform`; a single re-run is probabilistic across 6 runners.)
- **Metrics (Flux actually applied it):** after merge, `flux reconcile kustomization infrastructure` (or
  wait for sync), then `kubectl -n monitoring get endpoints ci-runner-node` shows all 6 IPs and Prometheus
  reports **6** `job=ci-runner-node` targets `up` (node_exporter listening on `.33/.34/.35:9100`).
- **Host RAM under real concurrency:** drive a max-concurrency load (enough platform jobs that two heavy
  e2e jobs land on the same physical host) and confirm no `exit 137` / "runner lost communication", and no
  OOM/balloon-pressure events in `dmesg` / the PVE task log. This is the true #620 regression test.

<!-- codex-review-status: complete -->
