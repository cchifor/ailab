resource "talos_machine_secrets" "this" {
  talos_version = var.talos_version
}

data "talos_machine_configuration" "cp" {
  cluster_name       = var.cluster_name
  cluster_endpoint   = "https://${var.cluster_vip}:6443"
  machine_type       = "controlplane"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  talos_version      = var.talos_version
  kubernetes_version = var.kubernetes_version
}

locals {
  cp_patches = {
    for k, v in var.control_planes : k => templatefile("${path.module}/machine-config/controlplane.yaml.tftpl", {
      node_ip     = v.ip
      prefix      = var.network_prefix
      gateway     = var.gateway
      vip         = var.cluster_vip
      nameservers = jsonencode(var.nameservers)
    })
  }
}

resource "talos_machine_configuration_apply" "cp" {
  for_each = var.control_planes

  client_configuration        = talos_machine_secrets.this.client_configuration
  machine_configuration_input = data.talos_machine_configuration.cp.machine_configuration
  node                        = each.value.ip
  config_patches              = [local.cp_patches[each.key]]

  depends_on = [proxmox_virtual_environment_vm.cp]
}

resource "talos_machine_bootstrap" "this" {
  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = var.control_planes["cp1"].ip
  endpoint             = var.control_planes["cp1"].ip

  depends_on = [talos_machine_configuration_apply.cp]
}

data "talos_client_configuration" "this" {
  cluster_name         = var.cluster_name
  client_configuration = talos_machine_secrets.this.client_configuration
  nodes                = [for _, v in var.control_planes : v.ip]
  endpoints            = [for _, v in var.control_planes : v.ip]
}

resource "talos_cluster_kubeconfig" "this" {
  client_configuration = talos_machine_secrets.this.client_configuration
  node                 = var.control_planes["cp1"].ip
  endpoint             = var.control_planes["cp1"].ip

  depends_on = [talos_machine_bootstrap.this]
}

# Note: talos_cluster_health is intentionally omitted here — nodes stay NotReady until Cilium
# (CNI) is installed, which would make a health-wait block. Verify health after Cilium.
