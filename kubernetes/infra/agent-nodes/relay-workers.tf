# Separate, opt-in pool: no resource in this file iterates var.agent_nodes.
# Merge does not allocate a VM: the operator supplies relay-workers.tfvars.example
# only after the DHCP/edge rollout in docs/runbooks/relay-worker-capacity.md.
variable "relay_worker_nodes" {
  description = "Dedicated Relay workers, separately approved and applied. Empty until operator rollout."
  type = map(object({
    node_name  = string
    vm_id      = number
    ip         = string
    host_ip    = string
    cores      = number
    memory_mib = number
    disk_gb    = number
  }))
  default = {}
  validation {
    condition = alltrue([for k, v in var.relay_worker_nodes :
      k == "relay-worker-1" && v.node_name == "ai-node3" && v.vm_id == 4304 &&
      v.ip == "192.168.0.52" && v.host_ip == "192.168.0.4" &&
      v.cores == 12 && v.memory_mib == 24576 && v.disk_gb == 200
    ])
    error_message = "Only the measured ai-node3 pilot (4304, .52, 12 CPU/24 GiB/200 GiB) is approved by this capacity proposal; widening requires a reviewed change."
  }
}

locals {
  relay_worker_schematic = local.schematics.relay_workers
  relay_worker_image     = "talos-${var.talos_version}-relay-workers-${local.relay_worker_schematic}.raw"
  relay_worker_patches = {
    for k, v in var.relay_worker_nodes : k => templatefile("${path.module}/machine-config/relay-worker.yaml.tftpl", {
      hostname           = k
      node_ip            = v.ip
      prefix             = var.network_prefix
      gateway            = var.gateway
      nameservers        = jsonencode(var.nameservers)
      host_ip            = v.host_ip
      storage_service_ip = var.storage_service_ip
      install_image      = "factory.talos.dev/nocloud-installer/${local.relay_worker_schematic}:${var.talos_version}"
    })
  }
}

resource "proxmox_virtual_environment_vm" "relay_worker" {
  for_each  = var.relay_worker_nodes
  name      = each.key
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["talos", "k8s", "worker", "relay-worker"]
  agent { enabled = true }
  stop_on_destroy = true
  cpu {
    cores = each.value.cores
    type  = "host"
  }
  memory { dedicated = each.value.memory_mib }
  scsi_hardware = "virtio-scsi-single"
  disk {
    datastore_id = var.vm_datastore
    import_from  = "${var.image_datastore}:import/${local.relay_worker_image}"
    interface    = "scsi0"
    size         = each.value.disk_gb
    file_format  = "raw"
    iothread     = true
    discard      = "on"
  }
  network_device { bridge = var.bridge }
  initialization {
    datastore_id = var.vm_datastore
    ip_config {
      ipv4 {
        address = "${each.value.ip}/${var.network_prefix}"
        gateway = var.gateway
      }
    }
  }
  operating_system { type = "l26" }
  serial_device {}
  lifecycle {
    prevent_destroy = true # an omitted opt-in file must never silently remove live capacity
    ignore_changes  = [initialization, disk[0].import_from]
    precondition {
      condition = alltrue([for v in values(var.agent_nodes) :
        v.vm_id != each.value.vm_id && v.ip != each.value.ip
      ]) && !contains(keys(var.agent_nodes), each.key)
      error_message = "Relay worker identity must not overlap the protected agent pool."
    }
  }
}

resource "talos_machine_configuration_apply" "relay_worker" {
  for_each                    = var.relay_worker_nodes
  client_configuration        = data.terraform_remote_state.infra.outputs.client_configuration
  machine_configuration_input = data.talos_machine_configuration.worker.machine_configuration
  node                        = each.value.ip
  config_patches              = [local.relay_worker_patches[each.key]]
  depends_on                  = [proxmox_virtual_environment_vm.relay_worker]
}

resource "kubernetes_labels" "relay_worker_pool" {
  for_each    = var.relay_worker_nodes
  api_version = "v1"
  kind        = "Node"
  metadata { name = each.key }
  labels = {
    "ailab.io/relay-worker-pool" = "true"
    "ailab.io/runtime-gvisor"    = "true"
  }
  depends_on = [talos_machine_configuration_apply.relay_worker]
}

resource "kubernetes_node_taint" "relay_worker" {
  for_each = var.relay_worker_nodes
  metadata { name = each.key }
  taint {
    key    = "dedicated"
    value  = "relay-worker"
    effect = "NoSchedule"
  }
  field_manager = "relay-workers-tofu"
  force         = true
  depends_on    = [talos_machine_configuration_apply.relay_worker]
}

output "relay_worker_vms" {
  description = "Dedicated Relay worker capacity; runtime qualification is independent."
  value = { for k, v in var.relay_worker_nodes : k => {
    node         = v.node_name
    vmid         = v.vm_id
    ip           = v.ip
    schematic    = local.relay_worker_schematic
    staged_image = local.relay_worker_image
  } }
}

# Dedicated class; it cannot select the existing protected agent nodes.
resource "kubernetes_manifest" "relay_gvisor" {
  count = length(var.relay_worker_nodes) > 0 ? 1 : 0
  manifest = {
    apiVersion = "node.k8s.io/v1"
    kind       = "RuntimeClass"
    metadata   = { name = "relay-gvisor" }
    handler    = "runsc"
    # Initial estimates from the existing class; measure during qualification.
    overhead = { podFixed = { cpu = "80m", memory = "24Mi" } }
    scheduling = {
      nodeSelector = {
        "ailab.io/relay-worker-pool" = "true"
        "ailab.io/runtime-gvisor"    = "true"
      }
      tolerations = [{
        key      = "dedicated"
        operator = "Equal"
        value    = "relay-worker"
        effect   = "NoSchedule"
      }]
    }
  }
  depends_on = [kubernetes_labels.relay_worker_pool, kubernetes_node_taint.relay_worker]
}
