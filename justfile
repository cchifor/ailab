# ailab task runner — run from WSL2 Ubuntu (Ansible needs a Linux control node).
# `just` docs: https://github.com/casey/just  |  run `just` to list recipes.

set shell := ["bash", "-uc"]

ansible_dir := "ansible"
tofu_dir    := "tofu"

# Default: list recipes
default:
    @just --list

# Install Ansible + OpenTofu + collections into WSL (idempotent)
bootstrap:
    bash scripts/bootstrap-wsl.sh

# Read-only inventory of nodes + QNAP -> docs/_generated/
discover:
    bash scripts/discover.sh

# Ansible: full host config (base + thunderbolt + storage net + mounts)
net:
    cd {{ansible_dir}} && ansible-playbook site.yml

# Ansible: only the Thunderbolt/USB4 + storage-network bring-up
links:
    cd {{ansible_dir}} && ansible-playbook site.yml --tags net

# Ansible: validate links (iperf3 / ethtool / boltctl) -> docs/_generated/
validate:
    cd {{ansible_dir}} && ansible-playbook site.yml --tags validate

# Ansible: host node_exporter on the Proxmox hosts (feeds the AI Lab Fleet Grafana dashboard)
node-exporter:
    cd {{ansible_dir}} && ansible-playbook site.yml --tags monitoring

# Ansible: pin the CPU scaling governor to performance on the Proxmox hosts
perf:
    cd {{ansible_dir}} && ansible-playbook site.yml --tags performance

# Ansible: connectivity check
ping:
    cd {{ansible_dir}} && ansible pve_nodes -m ping

# Ansible: provision/refresh the self-hosted GitHub Actions runner VMs (cchifor/platform pool).
# Create the VMs first (tofu -chdir=kubernetes/infra/runners apply) + the github-runner SOPS secret.
# Dedicated playbook (not site.yml), so a full `just net` never touches the runner VMs.
# See docs/runbooks/ci-runners.md.
runners:
    cd {{ansible_dir}} && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey \
      ansible-playbook runners.yml

# Ansible: connectivity check for the runner VMs
ping-runners:
    cd {{ansible_dir}} && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" ansible github_runners -m ping

# Ansible: install/register the Gitea Actions runner (act_runner, host mode) on the node1/node2 runner
# VMs, ALONGSIDE the GitHub agent (forge-migration pilot). Run `just runners` first (base toolchain +
# `runner` user) and create the gitea-runner SOPS secret (org runner token). See docs/runbooks/ci-runners.md.
gitea-runners:
    cd {{ansible_dir}} && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey \
      ansible-playbook gitea-runners.yml

# Ansible: connectivity check for the Gitea Actions runner VMs
ping-gitea-runners:
    cd {{ansible_dir}} && ANSIBLE_CONFIG="$(pwd)/ansible.cfg" ansible gitea_runners -m ping

# OpenTofu: plan/apply ONLY the dev-worker VMs (separate state from runners + Talos).
dev-workers-plan:
    tofu -chdir=kubernetes/infra/dev-workers plan
dev-workers-apply:
    tofu -chdir=kubernetes/infra/dev-workers apply

# Ansible: provision/refresh the interactive dev-worker VMs (Claude Code + Codex).
# Create the VMs first (just dev-workers-apply). Dedicated playbook (not site.yml).
# See docs/runbooks/dev-workers.md.
dev-workers:
    cd {{ansible_dir}} && SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey \
      ansible-playbook dev-workers.yml

# Ansible: connectivity check for the dev-worker VMs
ping-dev-workers:
    cd {{ansible_dir}} && ansible dev_workers -m ping

# OpenTofu: plan/apply ONLY the Zot registry LXC (separate state from runners/dev-workers/Talos).
registry-plan:
    tofu -chdir=kubernetes/infra/registry-lxc plan
registry-apply:
    tofu -chdir=kubernetes/infra/registry-lxc apply

# Ansible: provision/refresh the Zot registry (registry.chifor.me). Create the LXC first
# (just registry-apply) + the registry SOPS secret. Dedicated playbook (not site.yml).
# See kubernetes/infra/registry/README.md.
registry:
    cd {{ansible_dir}} && SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey \
      ansible-playbook registry.yml

# Ansible: connectivity check for the registry LXC
ping-registry:
    cd {{ansible_dir}} && ansible registry -m ping

# Mirror an upstream image into the Zot registry (registry.chifor.me), preserving the multi-arch index + digest.
# Uses `docker buildx imagetools create` (no skopeo needed); authenticates as `ci` from the registry SOPS secret.
# Run from the main checkout (needs the gitignored age key). The Zot catch-all retention keeps tagged images.
# e.g.: just mirror-image ghcr.io/headlamp-k8s/headlamp-plugin-flux:v0.6.0 registry.chifor.me/headlamp-k8s/headlamp-plugin-flux:v0.6.0
mirror-image src dst:
    #!/usr/bin/env bash
    set -euo pipefail
    cd '{{justfile_directory()}}'   # so the relative SOPS/age-key paths resolve regardless of invocation CWD
    SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey \
      sops -d --extract '["registry_ci_password"]' ansible/secrets/registry.sops.yaml \
      | docker login registry.chifor.me -u ci --password-stdin
    # `imagetools create` PUSHES to the --tag registry by default (no --push flag exists; --dry-run skips).
    docker buildx imagetools create --tag '{{dst}}' '{{src}}'
    echo "--- mirrored; pin THIS index digest in the manifest: ---"
    docker buildx imagetools inspect '{{dst}}'

# Lint
lint:
    cd {{ansible_dir}} && ansible-lint || true
    cd {{tofu_dir}} && tofu fmt -check -recursive && tofu validate

# OpenTofu
init:
    cd {{tofu_dir}} && tofu init
plan:
    cd {{tofu_dir}} && tofu plan
apply:
    cd {{tofu_dir}} && tofu apply
fmt:
    cd {{tofu_dir}} && tofu fmt -recursive

# Show everything Proxmox knows about storage (needs API token in tfvars/env)
storage-status:
    cd {{ansible_dir}} && ansible pve_nodes -b -m command -a "pvesm status" --one-line
