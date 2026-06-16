# Implementation review — github-runners — round 1

<!-- codex-impl-review-status: finalized -->

## Findings

### site.yml second play gather_facts blocks full playbook before VMs exist

**Location:** ansible/site.yml

**Severity:** important — **RESOLVED**

Moved the runners play out of `site.yml` into a dedicated `ansible/runners.yml` playbook (`just runners`
now runs it). A full untagged `just net` no longer connects to runner VMs that may not exist yet.
`site.yml` carries only a NOTE pointing at `runners.yml`. ADR 0013 consequence updated.

### Plan document specifies agent { enabled = true } but implementation has false

**Location:** plans/2026-06-16-github-runners-plan.md, kubernetes/infra/runners/main.tf

**Severity:** nit — **RESOLVED**

Updated the plan doc to `agent { enabled = false }` (matches the shipped module; the minimal cloud image
ships no qemu-guest-agent, so `true` would hang `apply` — the role installs the agent instead).

### content_type = "iso" for .img file is unconventional but correct per Proxmox

**Location:** kubernetes/infra/runners/main.tf

**Severity:** nit — no change (the existing comment documents that Proxmox accepts `.img`/`.qcow2`
disk images under the iso content type).

### Verified correct (no action)

- **Ephemeral name** `ephem-$(hostname)-<epoch>-<8hex>` matches the canary regex; Jinja/bash rendering clean.
- **MemoryMax=10G** renders correctly → satisfies the canary cgroup assertion (10737418240).
- **SOPS** ansible/secrets path rule is ordered before the generic data|stringData rule; `.example` won't match.
- **Compose plugin path** `/usr/local/lib/docker/cli-plugins` is standard; **NodeSource** `suites: nodistro` correct for Node 20.
- **IP .47-.49 + vmid 4101-3** collision-free; **ci-runners-node.yaml** mirrors proxmox-node.yaml.
- **Runner user in docker group** is an accepted ephemeral-CI tradeoff (same as the Hyper-V pool).
- **become** from `ansible_user=ubuntu` works (cloud-image sudo); `set -euo pipefail` + `|| true` on
  `config.sh remove` is the correct idiom.

## Diff stat (round 1, 3df7ad0..impl commit)

 28 files changed, 1094 insertions(+)  — see `git diff 3df7ad0..HEAD --stat`.
