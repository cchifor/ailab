output "env_node_vms" {
  description = "Talos env-pool worker VMs (name => host node / vmid / IP)."
  value = {
    for k, v in var.env_nodes : k => {
      node = v.node_name
      vmid = v.vm_id
      ip   = v.ip
    }
  }
}
