output "qnap_nfs_storage_id" {
  description = "Proxmox storage ID for the QNAP NFS export"
  value       = proxmox_storage_nfs.qnap.id
}

output "qnap_nfs_mount_hint" {
  description = "Where/how to verify the mount"
  value       = "Mounted at /mnt/pve/${var.nfs_storage_id} on ${join(", ", var.pve_nodes)} — verify with `pvesm status`."
}

output "ailab_pool" {
  description = "Proxmox resource pool for AI-lab guests"
  value       = proxmox_virtual_environment_pool.ailab.pool_id
}
