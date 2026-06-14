# ---- Proxmox connection ----
# root@pam password auth (NOT the tofu token): LXC device passthrough + bind mounts are
# root@pam-only. The password is the node root password (see .env NODE_ROOT_PASSWORD).
variable "pve_endpoint" { type = string }
variable "pve_username" {
  type    = string
  default = "root@pam"
}
variable "pve_password" {
  type      = string
  sensitive = true
}
variable "pve_insecure" {
  type    = bool
  default = true
}
variable "pve_ssh_username" {
  type    = string
  default = "root"
}
variable "pve_ssh_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519"
}

# ---- Network (mgmt LAN; matches the Talos cluster module) ----
variable "gateway" {
  type    = string
  default = "192.168.0.1"
}
variable "network_prefix" {
  type    = number
  default = 24
}
variable "bridge" {
  type    = string
  default = "vmbr0"
}
variable "dns_domain" {
  type    = string
  default = "lan"
}
variable "nameservers" {
  type    = list(string)
  default = ["1.1.1.1", "9.9.9.9"]
}

# ---- GPU device group: host `render` gid (verified = 993 on all 3 nodes) ----
# device_passthrough remaps the device-node gid inside the CT to this value so the
# non-root `llama` user (member of a gid-993 group, see provision.sh) can open it.
variable "render_gid" {
  type    = number
  default = 993
}

# ---- Model store: QNAP NFS path on the host, bind-mounted to /models in the CT ----
variable "models_host_path" {
  type    = string
  default = "/mnt/pve/qnap-nfs/models"
}

# ---- LXC sizing ----
variable "lxc_memory_mib" {
  description = <<-EOT
    Hard memory cap (MiB, host OOM fence). 16 GiB suits the daily-driver model on the
    CURRENT 64 GB BIOS VRAM carve: weights are GPU-resident (firmware-reserved VRAM, NOT
    charged to this cgroup), so the CT only needs host RAM for the llama-server process.
    Raise toward ~96 GiB ONLY after reducing the BIOS carve + raising GTT for the 120B/122B
    models (GTT spill is pageable system RAM and can be charged here). See
    docs/runbooks/ai-host-setup.md.
  EOT
  type    = number
  default = 24576
}
variable "lxc_cores" {
  type    = number
  default = 16
}
variable "lxc_rootfs_datastore" {
  type    = string
  default = "local-lvm"
}
variable "lxc_rootfs_gb" {
  description = "Root filesystem size (GiB). Models live on the NFS bind mount, not here."
  type        = number
  default     = 24
}
variable "template_datastore" {
  description = "Datastore for the LXC template. qnap-nfs is shared + already content-typed for vztmpl, so the template downloads once for all nodes."
  type        = string
  default     = "qnap-nfs"
}
variable "template_download_node" {
  description = "Node that performs the one-time template download to the shared datastore."
  type        = string
  default     = "ai-node1"
}

# ---- Debian 13 LXC template (filename confirmed via `pveam available` 2026-06-14) ----
variable "lxc_template_url" {
  type    = string
  default = "http://download.proxmox.com/images/system/debian-13-standard_13.1-2_amd64.tar.zst"
}
variable "lxc_template_file" {
  type    = string
  default = "debian-13-standard_13.1-2_amd64.tar.zst"
}

# ---- One privileged GPU LXC per physical host ----
# IPs .51/.52/.53 are free: nodes are .2/.3/.4, CP VIP .40, Talos VMs .41/.42/.43.
variable "ai_llm_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  }))
  default = {
    "ai-llm-1" = { node_name = "ai-node1", vm_id = 5001, ip = "192.168.0.51", hostname = "ai-llm-1" }
    "ai-llm-2" = { node_name = "ai-node2", vm_id = 5002, ip = "192.168.0.52", hostname = "ai-llm-2" }
    "ai-llm-3" = { node_name = "ai-node3", vm_id = 5003, ip = "192.168.0.53", hostname = "ai-llm-3" }
  }
}
