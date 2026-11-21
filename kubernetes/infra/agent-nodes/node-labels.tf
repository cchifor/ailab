# ── Agent-pool node identity (label + taint), applied CLUSTER-SIDE ────────────────────────────────
# NodeRestriction blocks a WORKER kubelet from self-registering arbitrary ailab.io/* labels + custom
# taints, so machine.nodeLabels/nodeTaints in worker.yaml.tftpl are silently dropped on these worker
# nodes (they work on CPs, which aren't restricted). The agentforge workloads select the pool via
# `nodeSelector: ailab.io/agent-pool=true` + tolerate `dedicated=agent:NoSchedule` — so the label +
# taint MUST exist or nothing schedules. Apply them declaratively from the admin kubeconfig (infra/
# remote state), gated on the worker join. Idempotent + adopts an already-labeled node.
#
# This file was applied from the ops checkout on 2026-09-07 but never committed, so the state held
# these resources while main did not declare them: a plan from main would have destroyed them.
# Committed on 2026-10-21, with only `tofu fmt` applied. On Talos 1.14.2 the nodes'
# talos.dev/owned-labels and owned-taints annotations show that Talos now manages the same label and
# taint too (both write the same value). The sandbox-runtime labels in talos.tf rely on that, since
# Talos itself already writes the extensions.talos.dev/* labels on these workers.
provider "kubernetes" {
  config_path = data.terraform_remote_state.infra.outputs.kubeconfig_path
}

# ailab.io/relay-worker-pool=false: the Relay researcher infrastructure agent selects the agent pool
# with it (docs/relay-workers/researcher-installation-definition.json controlPlacement). Relay's
# installation preparation requires the agent's nodeSelector to set some key of the worker placement
# (relay-worker-1: ailab.io/relay-worker-pool=true, relay-workers.tf) to a different value, and this
# key does that without pinning the agent to one hostname. Apply it BEFORE the agent Deployment that
# selects it rolls out, or the agent Pod stays Pending (runbook relay-researcher-installation.md, I7).
# Relay workers are a separate map (var.relay_worker_nodes) and keep their Talos-owned "true".
resource "kubernetes_labels" "agent_pool" {
  for_each    = var.agent_nodes
  api_version = "v1"
  kind        = "Node"
  metadata { name = "talos-${each.value.hostname}" }
  labels = {
    "ailab.io/agent-pool"        = "true"
    "ailab.io/relay-worker-pool" = "false"
  }
  depends_on = [talos_machine_configuration_apply.worker]
}

resource "kubernetes_node_taint" "agent" {
  for_each = var.agent_nodes
  metadata { name = "talos-${each.value.hostname}" }
  taint {
    key    = "dedicated"
    value  = "agent"
    effect = "NoSchedule"
  }
  field_manager = "agent-nodes-tofu"
  force         = true
  depends_on    = [talos_machine_configuration_apply.worker]
}
