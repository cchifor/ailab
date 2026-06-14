terraform {
  # Local state (kubernetes/infra/terraform.tfstate, gitignored). Migrate to remote later.
  backend "local" {}
}
