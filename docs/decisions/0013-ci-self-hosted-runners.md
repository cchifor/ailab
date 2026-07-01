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
**Update (2026-07-01, scale 3→6):** Doubled the pool to **6 runners — two per Proxmox host**
(`gha-runner-4/5/6`, vmid 4104–4106, IPs .33/.34/.35), **identical** sizing + config to 1–3 via the
shared `runner_*` vars — additive `runner_nodes` map entries only; no role or secret change. Trade-off:
the original "one per host" fault isolation becomes two-per-host, and GitHub may co-schedule two heavy
jobs on one host, so the rollout is **gated on a per-host RAM check** — full budget being Talos CP VM +
AI LLM LXC + dev-worker + runner ×2 (plus node1's registry LXC and the iGPU VRAM carve) — with
balloon-reclaim/swap-under-peak treated as a no-go, not steady state (that is #620). See
`plans/2026-07-01-scale-gha-runners-to-6-plan.md`.
**Remaining:** decommission the 4 Hyper-V runners; optionally narrow the App install to platform-only.
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
Provision **3 full QEMU VMs** (Ubuntu 24.04 cloud image), one per Proxmox host (`gha-runner-1/2/3`,
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
