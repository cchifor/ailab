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

# OpenTofu: plan/apply ONLY the Talos AGENT node pool (AgentForge v2 compute; separate state from
# runners/dev-workers/Talos-CPs). Talos WORKERS that JOIN the existing cluster — the module reads
# infra/'s state READ-ONLY for the cluster machine_secrets (Option B, ADR 0019), so `tofu apply` in
# infra/ must have run once to expose the machine_secrets/client_configuration outputs FIRST. Run
# from Windows in practice (the talos provider is windows_amd64) — these are the WSL mirrors.
# Requires the Talos nocloud image staged on each node (scripts/stage-talos-image.sh) FIRST.
agent-nodes-plan:
    tofu -chdir=kubernetes/infra/agent-nodes plan
# Depends on nested-virt-verify (Stage-2 gate): never provision the Kata pool against a host lacking nested=Y.
agent-nodes-apply: nested-virt-verify
    tofu -chdir=kubernetes/infra/agent-nodes apply

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

# Test the gitea-runner cleanup disk-reclaim logic (mocked docker/df; no VM needed)
test-gitea-runner:
    bash {{ansible_dir}}/roles/gitea_runner/tests/test-cleanup.sh

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

# ============================ AgentForge v2 activation ============================
# Fully-IaC activation of the dormant v2 stack (docs/runbooks/agentforge-activation.md).
# Staged: 0 images -> ⛔1 operators/security merge (OpenBao auto-init/unseal/provision) ->
# ⛔2 Kata pool -> ⛔3 agentforge merge -> ⛔4 un-gate -> ⛔5 boundary tests -> v1.1.

# Stage-2 gate: assert AMD nested virt is live on every Proxmox host (Kata /dev/kvm prereq)
nested-virt-verify:
    python scripts/check-nested-virt.py

# PREFLIGHT #2: assert the host-mode Gitea act_runner CI pool is fit before the Stage-0/Stage-4 image
# builds run on it (daemon, docker, host-mode label, egress, capacity + the Gitea-API online check).
# Needs GITEA_TOKEN (scope read:admin,read:organization); use `--skip-api` for host-side only. See
# docs/runbooks/ci-runners.md §8.
ci-runners-preflight *args:
    python scripts/check-ci-runners.py {{args}}

# Stage-0 pin: bootstrap-class image digests ONLY (orchestrator + platform). Its own commit; MUST NOT
# un-gate a workload. Usage: just pin-bootstrap sha256:<orch> sha256:<platform>
pin-bootstrap orchestrator platform:
    python scripts/pin-image-digests.py "orchestrator={{orchestrator}}" "agentforge-platform={{platform}}"

# Stage-4 pin: workload image digests. Usage: just pin-workloads broker=sha256:.. sandbox=sha256:.. ...
pin-workloads +pins:
    python scripts/pin-image-digests.py {{pins}}

# Stage-1 verify: OpenBao init/unseal/provision + the canary SecretStore login (the E2E k8s-auth proof)
openbao-status:
    kubectl --context admin@ai -n openbao get pods,jobs,secrets,cm,secretstore,externalsecret 2>/dev/null || true
    @echo "--- canary ExternalSecret (must be Ready=True after provision) ---"
    kubectl --context admin@ai -n openbao get externalsecret openbao-canary -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}{"\n"}' 2>/dev/null || true

# agentforge-platform-activation.md step 3: create the agentforge_platform DB + afp_admin/afp_app
# roles via the bootstrap.sql \gexec one-shot (idempotent; resolves the infra-pg primary per run).
# Kube context defaults to admin@ai here (estate convention — the CURRENT default context is a
# DIFFERENT cluster, a write-path hazard for anything that touches infra-pg). scripts/af-db.sh itself
# keeps an empty=current-context contract (see its header); sibling scripts/verify-sandbox-boundary.sh
# instead defaults its own KUBECTL_CONTEXT to admin@ai internally — this recipe pins the default here
# at the call site instead, but AF_KUBE_CONTEXT can still be set/overridden by the caller.
# Bootstrap the agentforge_platform DB (roles + DB, idempotent \gexec)
af-db-init:
    AF_KUBE_CONTEXT="${AF_KUBE_CONTEXT:-admin@ai}" bash scripts/af-db.sh init

# agentforge-platform-activation.md step 4: (re-)run the platform schema/RLS migration Job + verify
# alembic head + RLS forced. Kube context defaults to admin@ai (see af-db-init comment above).
# Run the platform schema/RLS migration Job and verify alembic head + RLS
af-db-migrate:
    AF_KUBE_CONTEXT="${AF_KUBE_CONTEXT:-admin@ai}" bash scripts/af-db.sh migrate

# PR-B go-live verification walk: rollout status, pinned-digest match, in-pod /readyz, external
# /healthz over the cloudflared tunnel. Run AFTER PR-B (- deployment.yaml) has merged + reconciled.
# Kube context defaults to admin@ai (see af-db-init comment above).
# Post-deploy smoke test for the agentforge-platform CP (rollout, digest, readyz, healthz)
af-cp-smoke:
    AF_KUBE_CONTEXT="${AF_KUBE_CONTEXT:-admin@ai}" bash scripts/af-cp-smoke.sh

# Step-0 pin verify (scripted, replaces the eyeball curl): assert <image>:<tag> still resolves to
# <digest> before trusting/merging a pin. digest is OPTIONAL for image=agentforge-platform (self-
# defaults to whatever's currently pinned in deployment.yaml, so this example can't drift stale). Usage:
#   just pin-verify agentforge-platform 276ccad                  # self-defaulting, always current
#   just pin-verify agentforge-platform 276ccad sha256:<64hex>   # explicit override
# Verify an image:tag still resolves to its approved (or self-defaulted) digest
pin-verify image tag digest="":
    bash scripts/verify-image-digest.sh {{image}} {{tag}} {{digest}}

# NB: `agent-nodes-plan`/`agent-nodes-apply` already exist above; run `just nested-virt-verify` FIRST
# (Stage-2 gate) before `just agent-nodes-apply` — the Kata pool needs nested=Y on the target hosts.
