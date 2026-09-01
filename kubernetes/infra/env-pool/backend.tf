terraform {
  # Local state, separate root module — env-pool VMs can be built/destroyed without ever planning
  # the CPs (infra/), agent-nodes, dev-workers, runners, or LXCs (CLAUDE.md CP-safety rules). The
  # one link back to infra/ is READ-ONLY (terraform_remote_state, talos.tf).
  # NOTE first apply ran from a scratchpad clone (worktree-isolated session could not write the
  # main checkout); the state file must be moved WITH this directory into the main checkout after
  # merge — local state is a plain file, `tofu init` again at the new path and verify a no-op plan.
  backend "local" {}
}
