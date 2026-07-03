# CLAUDE.md — cchifor/ailab

100%-IaC home AI lab: 3× Bosgame M5 (Strix Halo / gfx1151 iGPU) **Proxmox** cluster + a QNAP NAS,
running **Talos** Kubernetes (3-CP HA) with **Cilium** + **Flux** (GitOps), an **llama.cpp/Vulkan** LLM
appliance in privileged LXCs, self-hosted **GitHub Actions runners**, and interactive dev-worker VMs.
Provisioned with **OpenTofu** (bpg/proxmox) + Python/paramiko scripts. This file is orientation +
gotchas; the source of truth is `docs/decisions/` (ADRs) and `docs/runbooks/`.

## Workflow / GitOps
- **Kubernetes** (`kubernetes/apps/**`): **Flux** reconciles `main` — merge to ship. Changes go via PR → squash-merge.
- **VMs/LXCs** (`kubernetes/infra/**`): **OpenTofu**, applied by hand via `just` (Flux does NOT manage these).
  Modules: `infra/` (Talos CPs) · `infra/runners/` · `infra/dev-workers/` · `infra/ai-lxc/` · `infra/registry-lxc/`.
  Recipes: `just plan|apply|fmt` (Talos CPs) · `just runners` · `just dev-workers` · `just registry` (+ `*-plan/apply`). `just --list` for all.
- Secrets = **SOPS + age** (`.sops.yaml`, key at `_out/age.agekey`). **Never commit `_out/`** — kubeconfig, talosconfig, age key, and tofu creds all live there (gitignored).
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
| Role | IPs | vmid |
|---|---|---|
| Proxmox hosts | ai-node1/2/3 = .2 / .3 / .4 | — |
| Talos CPs | .41 / .42 / .43 (API VIP .40:6443) | 4001–4003 |
| GHA runners | .47 / .48 / .49 + .33 / .34 | 4101–4105 |
| dev-workers | .37 / .38 / .39 (user `c4`) | 4201–4203 |
| AI LLM LXCs | .44 / .45 / .46 | 5001–5003 |
| registry LXC (node1) | — | 5004 |

## Where to look
`docs/decisions/` = ADRs (living decisions) · `docs/runbooks/` = operations (`ci-runners`, `dev-workers`, `ai-host-setup`, `internet-exposure`) · `plans/` = dated planning records (historical — don't rewrite) · `README.md` = repo overview.
