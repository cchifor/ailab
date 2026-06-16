output "runner_vms" {
  description = "GitHub Actions runner VMs (name => host node / vmid / IP)."
  value = {
    for k, v in var.runner_nodes : k => {
      node = v.node_name
      vmid = v.vm_id
      ip   = v.ip
    }
  }
}
