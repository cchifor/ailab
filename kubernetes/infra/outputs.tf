# Write kubeconfig + talosconfig to gitignored local files for kubectl/talosctl.
resource "local_sensitive_file" "kubeconfig" {
  content         = talos_cluster_kubeconfig.this.kubeconfig_raw
  filename        = "${path.module}/_out/kubeconfig"
  file_permission = "0600"
}

resource "local_sensitive_file" "talosconfig" {
  content         = data.talos_client_configuration.this.talos_config
  filename        = "${path.module}/_out/talosconfig"
  file_permission = "0600"
}

output "schematic_id" {
  description = "Talos Image Factory schematic ID (keep scripts/stage-talos-image.sh in sync)"
  value       = talos_image_factory_schematic.this.id
}

output "talos_disk_image_url" {
  value = data.talos_image_factory_urls.this.urls.disk_image
}

output "cluster_vip" {
  value = var.cluster_vip
}

output "control_plane_ips" {
  value = [for _, v in var.control_planes : v.ip]
}

output "kubeconfig_path" {
  value = local_sensitive_file.kubeconfig.filename
}

output "talosconfig_path" {
  value = local_sensitive_file.talosconfig.filename
}
