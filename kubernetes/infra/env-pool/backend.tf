terraform {
  # Local state, separate root module — env-pool VMs can be built/destroyed without ever planning
  # the CPs (infra/), agent-nodes, dev-workers, runners, or LXCs (CLAUDE.md CP-safety rules). The
  # one link back to infra/ is READ-ONLY (terraform_remote_state, talos.tf).
  #
  # STATE LOCATION (2026-09-21): the authoritative state is
  #   <main checkout>/kubernetes/infra/env-pool/terraform.tfstate
  # exactly like every other module. The spike's state (applied from a scratchpad clone, 2026-09-01)
  # was never handed over and is lost; talos-env-node-1 was re-adopted via imports.tf. When applying
  # from a git worktree, point init at that file so a second state is never created:
  #   tofu -chdir=kubernetes/infra/env-pool init -backend-config="path=C:/Users/chifo/work/home/ailab/kubernetes/infra/env-pool/terraform.tfstate"
  # Back the file up to kubernetes/infra/_out/ before every apply (it embeds the Talos client key
  # and, via remote state, the cluster machine secrets — never commit it, never paste it).
  backend "local" {}
}
