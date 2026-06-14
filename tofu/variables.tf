# ---- Proxmox connection ----
variable "pve_endpoint" {
  description = "Proxmox API endpoint, e.g. https://192.168.0.2:8006/"
  type        = string
}

variable "pve_api_token" {
  description = "API token in the form 'user@realm!tokenid=secret'"
  type        = string
  sensitive   = true
}

variable "pve_insecure" {
  description = "Skip TLS verification (self-signed PVE cert)"
  type        = bool
  default     = true
}

variable "pve_ssh_username" {
  description = "SSH user for provider operations that need SSH"
  type        = string
  default     = "root"
}

variable "pve_ssh_key_path" {
  description = "Path to the SSH private key used by the provider"
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "pve_nodes" {
  description = "Proxmox node names that should mount the shared storage"
  type        = list(string)
  default     = ["pve-node1", "pve-node2", "pve-node3"]
}

# ---- QNAP NFS storage (fill from docs/runbooks/qnap-storage-setup.md) ----
variable "nfs_storage_id" {
  description = "Proxmox storage ID for the QNAP NFS export"
  type        = string
  default     = "qnap-nfs"
}

variable "qnap_nfs_server" {
  description = "Storage service IP of the QNAP (10.55.0.254, or LAN fallback 192.168.1.225)"
  type        = string
  default     = "10.55.0.254"
}

variable "qnap_nfs_export" {
  description = "NFS export path on the QNAP (confirm exact path on the device)"
  type        = string
  default     = "/share/pve-nfs"
}

variable "qnap_nfs_content" {
  description = "Proxmox content types stored on this NFS"
  type        = list(string)
  default     = ["images", "iso", "vztmpl", "backup"]
}

variable "qnap_nfs_options" {
  description = "NFS mount options"
  type        = string
  default     = "vers=4.1"
}
