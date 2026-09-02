# Local state. NOTE (2026-09-02): the module was first applied from the session scratchpad
# clone (worktree-isolated session cannot write the main checkout) — like env-pool, the
# authoritative terraform.tfstate must be handed over to the main checkout after merge,
# then verified with a no-op plan. Until then, do not apply from the main checkout.
terraform {
  backend "local" {}
}
