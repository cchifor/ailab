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

# P2 image-factory sync (mirrors infra/outputs.tf). Feed schematic_id + the staged filename to
# scripts/stage-talos-image.sh so the Kata/gVisor image on each node matches this config:
#   SCHEMATIC=$(tofu -chdir=kubernetes/infra/agent-nodes output -raw schematic_id) \
#   FILE=$(tofu -chdir=kubernetes/infra/agent-nodes output -raw agent_image_file) \
#     scripts/stage-talos-image.sh
output "schematic_id" {
  description = "Talos Image Factory schematic ID for the agent pool (Kata + gVisor + base extensions)."
  value       = talos_image_factory_schematic.agent.id
}

output "talos_disk_image_url" {
  description = "Factory nocloud disk-image URL for the agent-pool schematic (xz; staged per node)."
  value       = data.talos_image_factory_urls.agent.urls.disk_image
}

output "agent_image_file" {
  description = "Staged nocloud image basename on local:import for the agent pool (distinct from the CP image)."
  value       = local.agent_image_file
}
