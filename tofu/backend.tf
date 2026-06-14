terraform {
  # Local state for now (tofu/terraform.tfstate, gitignored).
  # TODO: migrate to remote state (e.g. an NFS/MinIO/S3 bucket on the lab) before it grows.
  backend "local" {}
}
