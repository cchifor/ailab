# Worker JOIN — reuse the EXISTING cluster PKI (do NOT mint a new talos_machine_secrets here).
#
# The machine_secrets + client_configuration that bootstrapped the CPs live only in the CP root
# module's state (infra/terraform.tfstate, resource talos_machine_secrets.this). This module reads
# them READ-ONLY via terraform_remote_state so the worker config is signed by the SAME CA and the
# node joins the live `ai` cluster. Prereq: infra/ must expose the two sensitive outputs
# (machine_secrets, client_configuration) — added to infra/outputs.tf — and have been `tofu apply`-d
# once so they are present in its state (adding outputs changes no infrastructure; the CPs are not
# touched). See ADR 0019 (Option B) + the spec §1.2.
data "terraform_remote_state" "infra" {
  backend = "local"
  config = {
    path = "${path.module}/../terraform.tfstate"
  }
}

# Worker machine-config data source. Differs from the CP data source (infra/talos.tf) ONLY by
# machine_type = "worker"; secrets come from remote state, not a local resource.
data "talos_machine_configuration" "worker" {
  cluster_name       = var.cluster_name
  cluster_endpoint   = "https://${var.cluster_vip}:6443"
  machine_type       = "worker"
  machine_secrets    = data.terraform_remote_state.infra.outputs.machine_secrets
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
}

locals {
  worker_patches = {
    for k, v in var.agent_nodes : k => templatefile("${path.module}/machine-config/worker.yaml.tftpl", {
      node_ip     = v.ip
      prefix      = var.network_prefix
      gateway     = var.gateway
      nameservers = jsonencode(var.nameservers)
    })
  }
}

# Apply the worker config to each node. No talos_machine_bootstrap / talos_cluster_kubeconfig — the
# cluster is already bootstrapped; a worker only needs configuration_apply. The node registers
# against the VIP and becomes Ready once Cilium schedules its agent DaemonSet onto the new node.
resource "talos_machine_configuration_apply" "worker" {
  for_each = var.agent_nodes

  client_configuration        = data.terraform_remote_state.infra.outputs.client_configuration
  machine_configuration_input = data.talos_machine_configuration.worker.machine_configuration
  node                        = each.value.ip
  config_patches              = [local.worker_patches[each.key]]

  depends_on = [proxmox_virtual_environment_vm.agent]
}
