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

# Ansible: connectivity check
ping:
    cd {{ansible_dir}} && ansible pve_nodes -m ping

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
