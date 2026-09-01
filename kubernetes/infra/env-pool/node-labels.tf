# ── Env-pool node identity (label + taint), applied CLUSTER-SIDE ─────────────────────────────────
# NodeRestriction drops worker-kubelet self-registered ailab.io/* labels + custom taints (same trap
# as agent-nodes/node-labels.tf), so the authoritative label/taint lives here. Pool workloads select
# `ailab.io/env-pool=true` + tolerate `dedicated=env:NoSchedule`; the taint keeps everything else
# (and the pool keeps itself off AgentForge's agent nodes, whose dedicated=agent taint it does not
# tolerate — ordering enforced by construction, per the plan).
provider "kubernetes" {
  config_path = var.kubeconfig_path
}

resource "kubernetes_labels" "env_pool" {
  for_each    = var.env_nodes
  api_version = "v1"
  kind        = "Node"
  metadata { name = "talos-${each.value.hostname}" }
  labels      = { "ailab.io/env-pool" = "true" }
  depends_on  = [talos_machine_configuration_apply.worker]
}

resource "kubernetes_node_taint" "env" {
  for_each = var.env_nodes
  metadata { name = "talos-${each.value.hostname}" }
  taint {
    key    = "dedicated"
    value  = "env"
    effect = "NoSchedule"
  }
  field_manager = "env-pool-tofu"
  force         = true
  depends_on    = [talos_machine_configuration_apply.worker]
}
