# Worker JOIN — reuse the EXISTING cluster PKI (do NOT mint talos_machine_secrets here).
# Identical mechanism to agent-nodes/talos.tf (ADR 0019 Option B): read the CP root module's
# state READ-ONLY for machine_secrets + client_configuration so the worker config is signed
# by the SAME CA and the node joins the live `ai` cluster.
data "terraform_remote_state" "infra" {
  backend = "local"
  config = {
    path = var.infra_state_path
  }
}

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
    for k, v in var.env_nodes : k => templatefile("${path.module}/machine-config/worker.yaml.tftpl", {
      node_ip            = v.ip
      prefix             = var.network_prefix
      gateway            = var.gateway
      nameservers        = jsonencode(var.nameservers)
      host_ip            = v.host_ip
      storage_service_ip = var.storage_service_ip
    })
  }
}

# The cluster is already bootstrapped; a worker only needs configuration_apply.
resource "talos_machine_configuration_apply" "worker" {
  for_each = var.env_nodes

  client_configuration        = data.terraform_remote_state.infra.outputs.client_configuration
  machine_configuration_input = data.talos_machine_configuration.worker.machine_configuration
  node                        = each.value.ip
  config_patches              = [local.worker_patches[each.key]]

  depends_on = [proxmox_virtual_environment_vm.env]
}
