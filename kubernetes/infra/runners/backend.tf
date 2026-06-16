terraform {
  # Local state (kubernetes/infra/runners/terraform.tfstate, gitignored). Separate root module
  # => separate state, so the CI runner VMs can be built/destroyed without ever planning the
  # Talos control-plane VMs (../) or the AI LXC appliance (../ai-lxc/). Migrate to remote later.
  backend "local" {}
}
