output "dev_worker_vms" {
  description = "Dev-worker VMs (name => host node / vmid / IP)."
  value = {
    for k, v in var.dev_worker_nodes : k => {
      node = v.node_name
      vmid = v.vm_id
      ip   = v.ip
    }
  }
}
