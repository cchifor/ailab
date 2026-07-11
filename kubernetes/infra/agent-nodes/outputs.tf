output "agent_node_vms" {
  description = "Talos agent-worker VMs (name => host node / vmid / IP)."
  value = {
    for k, v in var.agent_nodes : k => {
      node = v.node_name
      vmid = v.vm_id
      ip   = v.ip
    }
  }
}
