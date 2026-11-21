# The ACTIVE registry: CT 5005 on ai-node3 since the zot move (2026-11-21). The `registry_*` outputs follow the live
# endpoint; the old resource keeps its binding to CT 5004 (no node_name or vm_id change on it), reported as retired.
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
# CT 5004 on ai-node1: stopped, onboot 0, net0 link_down=1 and locked by hand; kept as the pre-cutover copy until
# the owner approves decommissioning it.
output "registry_retired_vmid" {
  value = var.registry_lxc.vm_id
}
output "registry_retired_node" {
  value = var.registry_lxc.node_name
}
