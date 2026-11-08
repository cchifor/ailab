output "relay_worker_vms" {
  description = "Talos relay-worker VMs (name => host node / vmid / IP / sizing)."
  value = {
    for k, v in var.relay_worker_nodes : k => {
      node       = v.node_name
      vmid       = v.vm_id
      ip         = v.ip
      cores      = v.cores
      memory_mib = v.memory_mib
      disk_gb    = v.disk_gb
    }
  }
}
