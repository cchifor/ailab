terraform {
  # Local state (kubernetes/infra/relay-workers/terraform.tfstate, gitignored). A root module of its
  # own, NOT a map inside agent-nodes/: that state holds the three agent nodes that run Relay and the
  # llm-router, so no apply here can plan, replace or re-configure them. The same separation the
  # runners/dev-workers/registry modules use. The one link back to infra/ is READ-ONLY: talos.tf reads
  # infra/'s state via terraform_remote_state to reuse the existing cluster machine_secrets.
  backend "local" {}
}
