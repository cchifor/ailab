# ADR 0013 — Self-hosted CI runners on the Proxmox lab (VMs)

**Status:** ACCEPTED + DEPLOYED (2026-06-16). GitHub App `ailab-ci-runners` created; 3 VMs applied
(`tofu`), runners registered to the `self-hosted-hv` pool, runner-health canary passing on the
Proxmox runners (the legacy 7G Hyper-V runners fail it). Memory ballooning 1→24 GiB. WSL was down, so
the runner host config was driven over SSH (mirrors the role); `just runners` from WSL converges.
**Update (2026-06-26, cchifor/platform#620):** CI was OOM-killing ~62% of jobs ("self-hosted runner
lost communication" / exit 137). Root cause: host RAM pressure (~80% used) drove `pvestatd` to
balloon the guests down to 1-2 GiB (the bpg default 1 GiB floor), starving running jobs. Fixed by
pinning the **balloon floor to 12 GiB** (`runner_memory_floating_mib`; the 24 GiB memory ceiling is
unchanged) and adding an **8 GiB guest swapfile** (`swappiness=10`) as an OOM reclaim valve. Both are
now codified (tofu + the `github_runner` role). The `MemoryMax=10G` cgroup cap is unrelated, unchanged.
**Update (2026-07-01, scale 3→5):** Expanded the pool to **5 runners — two on node1/node2, one on
node3** (`ci-runner-4/5`, vmid 4104/4105, IPs .33/.34), **identical** sizing + config to 1–3 via the
shared `runner_*` vars — additive `runner_nodes` map entries only; no role or secret change.
**ci-runner-6 (node3, .35 / vmid 4106) is reserved but DEFERRED** — see the measured finding below. Trade-off:
the original "one per host" fault isolation becomes two-per-host, and GitHub may co-schedule two heavy
jobs on one host, so the rollout is **gated on a per-host RAM check** — full budget being Talos CP VM +
AI LLM LXC + dev-worker + runner ×2 (plus node1's registry LXC and the iGPU VRAM carve) — with
balloon-reclaim/swap-under-peak treated as a no-go, not steady state (that is #620). See
`plans/2026-07-01-scale-ci-runners-to-6-plan.md`.
*Measured 2026-07-01 (live gate):* hosts expose only **~62 GiB** (the ~64 GiB iGPU carve takes the rest
of 128 GiB physical) and already run at **78–84 %** with one runner each (Talos ~22 GiB actual, LLM
1–8 GiB, one runner 5–9 GiB, dev-worker 1–3 GiB) → only **10–14 GiB avail** and light swap. A second
12 GiB-floor runner does **not** fit in idle headroom (node3 worst: 10.2 GiB avail, and its 122B LLM holds
7.8 GiB of **non-reclaimable** RSS with LXC `swap=0`, so the LLM cannot be capped down without OOM).
Shrinking the LLM was therefore **rejected**; only the dev-worker ceilings were cut 16→8 (peak-bounding).
Decision: deploy **5** — node3's second runner (`ci-runner-6`) is **deferred** precisely because it can't
hold its 12 GiB floor while the 122b is loaded; node3 keeps its existing single runner. node1/node2 each
take a second runner (both fit the floor at rest — 12.7 / 14.4 GiB avail); their residual risk is only the
concurrent-peak case (two heavy jobs on one host), cushioned by the 12 GiB floor + 8 GiB guest swap and
bounded by the dev-worker 16→8 cut. Add `ci-runner-6` after reducing node3's iGPU VRAM carve (or the Talos
CP allocation), which frees real host RAM.
**Update (2026-07-01, rock-solid hardening):** live triage found the pool's dominant CI failure was NOT
capacity but a **buildx ownership bug** — `~/.docker/buildx` becoming **root-owned** (a job ran `docker
buildx` under `sudo`) and staying that way across ephemeral cycles, so every later docker-build job on
that VM failed with `stat ~/.docker/buildx/instances: permission denied` / `EACCES … buildx/certs` (~10
of the last 12 failed runs). Because the runner *VM* persists (only the *registration* is ephemeral),
nothing re-chowned it; the original 3 runners only escaped via a one-off manual `chown` in PR #4. Fixes:
(1) **`runner-reclaim.sh` now self-heals** — it runs as root before every job (`ExecStartPre=+`) and
`chown -R runner:runner ~/.docker`, and reaps the accumulating **named `buildx_buildkit_builder-*_state`
volumes** (via `docker buildx rm --all-inactive` + `docker volume prune -af`; `prune -f` skipped named
volumes, so ~40 GiB had piled up → a latent disk-full failure). (2) **Observability gap closed** — a new
`PrometheusRule` (`ci-runners`) alerts on runner down / disk filling / #620 memory-pressure, plus a
node_exporter textfile **health beacon** (`runner_docker_config_root_owned`) that trips
**CIRunnerDockerConfigRootOwned** if the self-heal ever regresses. (3) The manual guest-agent enable step
is **codified** via the PVE API (`terraform_data.enable_guest_agent`, no SSH) — APPLIED (needed a
trailing-slash fix on `pve_endpoint`, #82, or the raw curl 500s on the double slash). (4) The ephemeral
wrapper got a bounded token-fetch retry. (5) Cross-repo gate DONE: platform's `runner-health.yml` canary
now asserts `~/.docker` is runner-owned + runs a `docker buildx build` smoke test (cchifor/platform#682).
**Remaining:** optionally narrow the App install to platform-only; optionally purge the drained,
powered-off BEAST Multipass VMs. (The 4 Hyper-V runners were decommissioned 2026-06-16.)
**Update (2026-07-03) — dev-worker/CP memory rebalance.** The "dev-worker ceilings cut 16→8" note above
was **reverted**: it was never applied to the live VMs, and the dev-workers OOM-thrashed at their 2 GiB
balloon floor under host oversubscription. Fix: **per-node balloon floors** (dw1 8 / dw2 10 / dw3 6 GiB,
ceiling back to 16) + **downsized the Talos CP VMs** (cp1 24 / cp2 24 / cp3 28 GiB) to free host RAM —
measured CP working set is only ~9 GiB, so the 32 GiB reservation was mostly reclaimable cache (ADR 0009
Update 2026-07-03). **ci-runner-6 stays DEFERRED:** the CP downsize freed ~4–8 GiB/node, but node3's
122B still maxes VRAM+GTT and a 2nd runner needs a full 12 GiB floor node3 can't hold. See cchifor/ailab#85, #86.
**Relates to:** ADR 0001 (OpenTofu + Ansible), ADR 0006 (Talos/Flux/Cilium), ADR 0008 (AI appliance =
LXC *outside* Talos), ADR 0009 (control-plane colocation / tight RAM budget).

## Context
`cchifor/platform` (private SaaS) runs its CI on a self-hosted **ephemeral** runner pool: 4 VMs
(`hv-runner-1..4`), Multipass-managed Ubuntu 24.04 on the Windows Hyper-V host `BEAST`, label
**`self-hosted-hv`** (selected via the repo variable `RUNNER_LABEL`). The workload is Docker-heavy —
`docker compose` (pinned **v2.31.0**) multi-service stacks, Buildx → ghcr.io, Playwright e2e (in a
containerized `e2e-runner` image), Python via `uv`, `k6`. The contract is version-controlled in that
repo (`infra/runner/`, `.github/runner-hooks/`) and asserted nightly by `runner-health.yml`.

That pool is tied to a Windows host (UAC + Multipass) and hit real pain: disks at 100%, constrained
dynamic memory, and "red on unrelated PR" flakes from root-owned `_work` leftovers. We want 3
equivalent runners on the codified Proxmox lab (ample headroom), brought into the same pool, then to
retire the Hyper-V VMs. `cchifor` is a GitHub **User** account → runners are necessarily
**repo-scoped** to `cchifor/platform` (user accounts can't host org-level runners).

## Decision
Provision **3 full QEMU VMs** (Ubuntu 24.04 cloud image), one per Proxmox host (`ci-runner-1/2/3`,
vmid 4101–4103, IPs .47/.48/.49 in the static-reserved `.2–.50` block, 8 vCPU / 24 GiB / 120 GiB), via
a new OpenTofu root module `kubernetes/infra/runners/` (bpg/proxmox, API-token auth + ssh, mirroring
the Talos `infra/` module). Configure them with the Ansible role **`github_runner`** (`just runners`),
which **ports the platform runner contract verbatim** — ephemeral wrapper, systemd unit + drop-ins
(`MemoryMax=10G`), the job-started hook, the between-jobs reclaim, `daemon.json` — and installs the
Docker stack (Compose pinned 2.31.0 + Buildx), `uv`, `k6`, Node. Runners register **ephemerally** into
the existing **`self-hosted-hv`** pool (additive; no workflow change), validated by platform's
`runner-health.yml`, after which the Hyper-V VMs are decommissioned.

The **only intentional change** vs the live pool is registration auth: a **GitHub App** (JWT →
installation token → registration token, in the ephemeral wrapper) instead of a static PAT. The
runner name format (`ephem-<host>-<epoch>-<8hex>`) and `MemoryMax=10G` are preserved so the canary
passes unchanged.

## Alternatives considered
- **Privileged LXC + Docker** (matches the AI-LXC pattern, lighter): rejected as primary. The pool we're
  replicating is VM-based; Docker-in-LXC adds overlay2/cgroup-delegation quirks, needs the
  `memory.max` canary check re-validated, and a privileged LXC running arbitrary CI containers (Compose
  + Buildx) has a larger host-escape surface than a VM. VMs are a 1:1 port and isolate CI at the kernel.
- **ARC (Actions Runner Controller) on Talos**, GitOps-managed: rejected. (a) **Circularity** — CI that
  manages the cluster running from inside it; a bad apply could kill the runner mid-job. (b) Tight
  CP-only nodes (~28 GiB allocatable, ADR 0009) — `minRunners` pins standing RAM for a bursty workload.
  (c) Talos immutability fights dind/privileged needed for container builds. (d) It contradicts the
  lab's own doctrine (ADR 0008): heavy/privileged workloads run as guests *outside* Talos. Documented
  as the considered-and-rejected K8s-native option.
- **GitHub-hosted runners**: can't run the e2e stack against lab resources, and the repo is already
  committed to self-hosted (the whole `infra/runner/` contract).
- **Auth: fine-grained PAT** (what the live pool uses): the App is scoped, revocable, auto-rotating, and
  higher-limit — preferred. PAT remains the documented fallback (one `github_token` key in the secret).
- **JIT config** (`generate-jitconfig`) instead of `--ephemeral` + registration token: a valid future
  hardening (drops the registration-token round-trip), but it would diverge from the proven, canary-
  validated contract for no functional gain here (the token is minted seconds before use and consumed
  immediately). Kept `--ephemeral`; JIT can adopt the same `ephem-*` name later, canary-transparent.

## Consequences
- **Ballooning needs a floor (#620).** Letting `floating` drop to 1 GiB invited `pvestatd`, under host
  RAM pressure, to reclaim a *running* runner's RAM and OOM the job. The floor is now 12 GiB so a guest
  can't be squeezed below the CI working set; the host still reclaims idle headroom up to the 24 GiB
  ceiling. The 8 GiB guest swap is a backstop, not a substitute. Cost: ~33 GiB of host RAM is now
  committed across the 3 guests even when idle (hosts verified healthy, 79-85% used).
- The runner registration auth diverges from the live pool (App vs PAT) — intentional; the App key is
  the only new secret (SOPS, written 0400, never job-readable beyond what a workflow could already do).
- CI scope is unchanged: the runners execute platform's existing workflows (which push images + commit
  back). ailab's own (optional, read-only) infra CI is out of scope for now and would need its own
  repo-scoped registration (User account → no shared org pool).
- The runners are provisioned by a dedicated `ansible/runners.yml` playbook (`just runners`), kept out
  of `site.yml` so a full `just net` never connects to runner VMs that may not exist yet.
- A self-hosted runner remains a trusted-code boundary: safe because both repos are private and
  contributors are trusted. The job-started hook + ephemeral cycle remove the stale-workspace flakes.
- Keeping the contract in sync across two repos is a maintenance cost, but platform's nightly
  `runner-health.yml` canary catches drift automatically.
