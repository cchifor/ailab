terraform {
  # Local state (kubernetes/infra/ai-lxc/terraform.tfstate, gitignored). Separate from
  # the Talos cluster state in ../terraform.tfstate. Migrate to remote later.
  backend "local" {}
}
