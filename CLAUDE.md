# CLAUDE.md — cchifor/ailab

100%-IaC home AI lab: 3× Bosgame M5 (Strix Halo / gfx1151 iGPU) **Proxmox** cluster + a QNAP NAS,
running **Talos** Kubernetes (3-CP HA) with **Cilium** + **Flux** (GitOps), an **llama.cpp/Vulkan** LLM
appliance in privileged LXCs, self-hosted **CI runners** (Gitea Actions — GitHub Actions dormant), and interactive dev-worker VMs.
Provisioned with **OpenTofu** (bpg/proxmox) + Python/paramiko scripts. This file is orientation +
gotchas; the source of truth is `docs/decisions/` (ADRs) and `docs/runbooks/`.

>  **Forge = Gitea (`git.chifor.me`), NOT GitHub.** As of 2026-07-09 (ADR 0017) Gitea is the
> **master** forge for this repo and `cchifor/platform`. Push, open PRs, and run CI on
> **Gitea** (`git.chifor.me/cchifor/ailab`, org `cchifor`). `github.com/cchifor/*` is a
> **read-only push-mirror backup** (GitHub Actions dormant). Use the Gitea API / `tea` /
> `scripts/forge.sh` (gitea arm), **NOT `gh`**. Log in at git.chifor.me via Authelia.
>
> **Flux's bootstrap source is the GITHUB MIRROR** (`https://github.com/cchifor/ailab.git`,
> unauthenticated — it is public, and a source with no credential cannot write). It used to be
> in-cluster Gitea; that was a bootstrap loop, because repairing a dead forge required fetching
> from the dead forge (2026-09-08, ~4.5h). Gitea is still the sole WRITER and mirrors to GitHub
> in ~4s. Application repos still source from Gitea.

## Workflow / GitOps
- **Kubernetes** (`kubernetes/apps/**`): **Flux** reconciles `main` **from the GitHub mirror** (`github.com/cchifor/ailab`, W5 — see the box above) — merge to Gitea to ship; it mirrors in ~4s. Push/PRs still go to **Gitea** (squash-merge).
- **VMs/LXCs** (`kubernetes/infra/**`): **OpenTofu**, applied by hand via `just` (Flux does NOT manage these).
  Modules: `infra/` (Talos CPs) · `infra/runners/` · `infra/dev-workers/` · `infra/agent-nodes/` (Talos workers, AgentForge v2) · `infra/ai-lxc/` · `infra/registry-lxc/`.
  Recipes: `just plan|apply|fmt` (Talos CPs) · `just runners` · `just dev-workers` · `just agent-nodes-plan/apply` · `just registry` (+ `*-plan/apply`). `just --list` for all.
- Secrets = **SOPS + age** (`.sops.yaml`, key at **`kubernetes/infra/_out/age.agekey`** — matches README; `_out/` is gitignored so it does NOT exist in a git worktree, resolve the main checkout's copy via `"$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)"`). **Never commit `_out/`** — kubeconfig, talosconfig, age key, and tofu creds all live there (gitignored).
- Run **tofu on Windows** (`~/.tofubin/tofu.exe`): providers are `windows_amd64` and **WSL has no internet**, so Ansible-over-`/mnt/c` and tofu provider downloads fail there. State is **local** (`kubernetes/infra/**/terraform.tfstate`).

## Reaching the cluster (easy to hit the WRONG one)
- `kubectl` default context is **`home-lab`, a DIFFERENT k3s cluster**. For ailab use **`kubectl --context admin@ai`** (merged into `~/.kube/config`) or `KUBECONFIG=kubernetes/infra/_out/kubeconfig`.
- Prometheus is distroless (no shell) → `kubectl --context admin@ai -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090`, then curl the HTTP API.

## Reaching hosts/guests (no Ansible in WSL → paramiko scripts)
- Proxmox hosts (root): `python scripts/node-ssh.py <192.168.0.2|.3|.4> "<cmd>"`
- LXCs: `python scripts/lxc-exec.py <host> <ctid>`
- Runner VMs: `ssh ubuntu@<ip>` · dev-workers: `ssh c4@<ip>` (key `~/.ssh/id_ed25519`)

## Talos / control-plane safety
- Graceful CP reboot = **`talosctl shutdown -n <cp-ip>`** — **`qm shutdown`/ACPI does NOT stop Talos** (falls back to a hard stop). Then `qm set <vmid> --memory …` + `qm start`.
- Use **`_out/talosctl-1112.exe`** (v1.11.2, matches the cluster). The system `talosctl` is v1.6.2 and **UNSAFE** (silently drops newer config keys).
- Roll **ONE CP at a time**; verify **`talosctl … etcd status` is 3/3 in-sync** (quorum) between each reboot.

## Inventory (mgmt LAN 192.168.0.0/24)

**`docs/network-plan.md` is the IPAM registry — read it before allocating ANY address.** The LAN is
shared with the `cloudlab` GPU cluster (`.20`–`.22`, `.26`–`.28`), which is invisible to ailab tooling.
Free static space is only `.5`–`.7`, `.32`–`.35`, `.38`, `.39`, `.50`.

| Role | IPs | vmid |
|---|---|---|
| Proxmox hosts | ai-node1/2/3 = .2 / .3 / .4 | — |
| Talos CPs | .41 / .42 / .43 (API VIP .40:6443) | 4001–4003 |
| GHA / Gitea CI runners | .14 / .15 / .16 / .17 / .18 | 4101–4105 |
| CI runners (adopted into tofu 2026-09-07) | .19 / .29 / .30 / .31 / .23 | 4106–4110 |
| dev-workers | .8–.13 (user `c4`; also the agentforge hosts, ADR 0018) | 4201–4206 |
| Agent nodes (Talos workers, AgentForge v2, ADR 0019) | .47 / .48 / .49 | 4301–4303 |
| Talos env-node (out-of-band, not in tofu) | .37 | 4401 |
| Reviewer VMs (out-of-band, not in tofu) | .24 / .25 | 4501–4502 |
| AI LLM LXCs | .44 / .45 / .46 | 5001–5003 |
| registry LXC (node1) | .36 | 5004 |

> **Renumbering a guest takes THREE edits**: the guest (netplan), the tofu variable, AND the Proxmox
> `ipconfig0` (`qm set <vmid> --ipconfig0 …` + `qm cloudinit update <vmid>`). Both VM modules set
> `lifecycle { ignore_changes = [initialization] }`, so `tofu plan` is BLIND to `ipconfig0` drift —
> skipping the third edit leaves a stale address that cloud-init re-applies on the next reboot. That
> is what produced the 2026-09-03 collisions with the Talos nodes. See `docs/network-plan.md`.

## Where to look
`docs/decisions/` = ADRs (living decisions) · `docs/runbooks/` = operations (`ci-runners`, `dev-workers`, `ai-host-setup`, `internet-exposure`) · `plans/` = dated planning records (historical — don't rewrite) · `README.md` = repo overview.
