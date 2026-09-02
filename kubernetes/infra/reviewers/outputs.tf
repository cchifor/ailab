output "reviewers" {
  value = { for k, v in var.reviewer_nodes : k => "${v.hostname} = ${v.ip} (vmid ${v.vm_id}, ${v.node_name})" }
}
