# The registry: CT 5005 on ai-node3 since the zot move (2026-11-21). The old CT 5004 was destroyed on 2026-11-22.
output "registry_ip" {
  value = var.registry_lxc_n3.ip
}
output "registry_vmid" {
  value = var.registry_lxc_n3.vm_id
}
output "registry_hostname" {
  value = var.registry_lxc_n3.hostname
}
output "registry_node" {
  value = var.registry_lxc_n3.node_name
}
output "registry_n3_vmid" {
  value = var.registry_lxc_n3.vm_id
}
output "registry_n3_node" {
  value = var.registry_lxc_n3.node_name
}
