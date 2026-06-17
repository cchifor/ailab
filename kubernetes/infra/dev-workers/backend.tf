terraform {
  # Local state (kubernetes/infra/dev-workers/terraform.tfstate, gitignored). Separate root module
  # => separate state, so the dev-worker VMs can be built/destroyed without ever planning the
  # Talos control-plane VMs (../), the AI LXC appliance (../ai-lxc/), or the CI runners
  # (../runners/). Migrate to remote later.
  backend "local" {}
}
