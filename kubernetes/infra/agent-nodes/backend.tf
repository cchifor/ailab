terraform {
  # Local state (kubernetes/infra/agent-nodes/terraform.tfstate, gitignored). Separate root module
  # => separate state, so the Talos agent-worker VMs can be built/destroyed without ever planning
  # the CP-critical Talos control-plane VMs (../), the dev-worker VMs (../dev-workers/), the AI LXC
  # (../ai-lxc/), or the CI runners (../runners/). Mirrors the dev-workers/runners/registry
  # separation — deliberately keeps the safety-critical infra/ root module untouched by agent-pool
  # applies (CLAUDE.md CP-safety rules). The one link back to infra/ is READ-ONLY: talos.tf reads
  # infra/'s state via terraform_remote_state to reuse the existing cluster machine_secrets.
  backend "local" {}
}
