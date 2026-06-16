terraform {
  # Local state (kubernetes/infra/cloudflare/terraform.tfstate, gitignored). Separate from the Talos
  # cluster state (../terraform.tfstate) and the ai-lxc state. Migrate to remote later.
  backend "local" {}
}
