resource "talos_machine_secrets" "this" {
  talos_version = var.talos_config_contract
}

data "talos_machine_configuration" "cp" {
  cluster_name       = var.cluster_name
  cluster_endpoint   = "https://${var.cluster_vip}:6443"
  machine_type       = "controlplane"
  machine_secrets    = talos_machine_secrets.this.machine_secrets
  talos_version      = var.talos_config_contract
  kubernetes_version = var.kubernetes_version
}

locals {
  cp_patches = {
    for k, v in var.control_planes : k => templatefile("${path.module}/machine-config/controlplane.yaml.tftpl", {
      node_ip            = v.ip
      prefix             = var.network_prefix
      gateway            = var.gateway
      vip                = var.cluster_vip
      nameservers        = jsonencode(var.nameservers)
      host_ip            = v.host_ip # WS2: next-hop for the TB storage /32 route
      storage_service_ip = var.storage_service_ip
      storage_tier       = v.storage_tier                          # WS2: node label for fast-storage workload affinity
      cp_ips             = [for _, c in var.control_planes : c.ip] # etcd ingress allow-list
      install_image      = "factory.talos.dev/nocloud-installer/${talos_image_factory_schematic.this.id}:${var.talos_version}"
    })
  }
}

resource "talos_machine_configuration_apply" "cp" {
  for_each = var.control_planes

  client_configuration        = talos_machine_secrets.this.client_configuration
  machine_configuration_input = data.talos_machine_configuration.cp.machine_configuration
  node                        = each.value.ip
  config_patches              = [local.cp_patches[each.key]]
  # no_reboot (2026-10-07, plans/2026-10-07-etcd-leader-churn-plan.md step B). The default "auto"
  # reboots any node whose change cannot apply live, and this resource fans out to all three CPs in
  # one apply. A non-immediate change would therefore reboot the whole control plane at once and lose
  # etcd quorum. With no_reboot such an apply fails instead. Roll it per node: one controlled
  # `talosctl shutdown` + `qm start` at a time, with etcd 3/3 in sync between them
  # (docs/runbooks/node-maintenance.md).
  apply_mode = "no_reboot"

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
