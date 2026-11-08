# Worker JOIN: reuse the EXISTING cluster PKI (never mint a talos_machine_secrets here, which would
# fork the CA so the node could never join). Same pattern as agent-nodes/talos.tf: infra/'s state is
# read READ-ONLY for machine_secrets, client_configuration and the plain schematic id.
data "terraform_remote_state" "infra" {
  backend = "local"
  config = {
    path = "${path.module}/../terraform.tfstate"
  }
}

data "talos_machine_configuration" "worker" {
  cluster_name       = var.cluster_name
  cluster_endpoint   = "https://${var.cluster_vip}:6443"
  machine_type       = "worker"
  machine_secrets    = data.terraform_remote_state.infra.outputs.machine_secrets
  talos_version      = var.talos_config_contract
  kubernetes_version = var.kubernetes_version
}

locals {
  worker_patches = {
    for k, v in var.relay_worker_nodes : k => templatefile("${path.module}/machine-config/relay-worker.yaml.tftpl", {
      node_ip     = v.ip
      prefix      = var.network_prefix
      gateway     = var.gateway
      nameservers = jsonencode(var.nameservers)
      # The plain schematic (infra/image.tf), the one agent-node-1/2 run. It must equal the schematic of
      # the image staged for the first boot (scripts/stage-talos-image.sh's default), or the first
      # upgrade would silently change the node's extensions.
      install_image = "factory.talos.dev/nocloud-installer/${data.terraform_remote_state.infra.outputs.schematic_id}:${var.talos_version}"
    })
  }
}

# A worker only needs configuration_apply: the cluster is already bootstrapped. The node registers
# against the VIP, tainted from its first registration (Talos turns machine.nodeTaints into the
# kubelet's registerWithTaints), and becomes Ready once Cilium's DaemonSet runs on it.
resource "talos_machine_configuration_apply" "worker" {
  for_each = var.relay_worker_nodes

  client_configuration        = data.terraform_remote_state.infra.outputs.client_configuration
  machine_configuration_input = data.talos_machine_configuration.worker.machine_configuration
  node                        = each.value.ip
  config_patches              = [local.worker_patches[each.key]]

  lifecycle {
    # A replaced VM boots the staged image fresh, in maintenance mode, and this resource's inputs have
    # not changed, so without a trigger it would never re-apply and the node would never join
    # (depends_on only orders; the provider's Read cannot see a lost config). The VM's id is unknown
    # while it is being replaced and unchanged by an in-place update, so only a rebuild re-applies.
    replace_triggered_by = [proxmox_virtual_environment_vm.relay_worker[each.key].id]
  }

  depends_on = [proxmox_virtual_environment_vm.relay_worker]
}
