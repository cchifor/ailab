# Registers the QNAP NFS export as cluster-wide Proxmox datacenter storage.
#
# Prereqs:
#   1. Storage network is up (ansible: `just net`) so nodes can reach var.qnap_nfs_server.
#   2. QNAP ZFS pool + NFS export exist (docs/runbooks/qnap-storage-setup.md).
#
# Proxmox auto-mounts this at /mnt/pve/<id> on each listed node.
resource "proxmox_storage_nfs" "qnap" {
  id      = var.nfs_storage_id
  server  = var.qnap_nfs_server
  export  = var.qnap_nfs_export
  nodes   = var.pve_nodes
  content = var.qnap_nfs_content
  options = var.qnap_nfs_options
}
