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
# See docs/runbooks/ci-runners.md.
runners:
    cd {{ansible_dir}} && SOPS_AGE_KEY_FILE=../kubernetes/infra/_out/age.agekey \
      ansible-playbook site.yml --limit github_runners --tags runners

# Ansible: connectivity check for the runner VMs
ping-runners:
    cd {{ansible_dir}} && ansible github_runners -m ping

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
