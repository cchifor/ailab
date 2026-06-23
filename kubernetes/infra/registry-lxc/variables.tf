# ---- Proxmox connection (root@pam password; see .env NODE_ROOT_PASSWORD, same as ai-lxc) ----
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

# ---- Network (mgmt LAN) ----
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

# ---- LXC sizing ----
# Zot is tiny (a single Go binary). 2 vCPU / 2 GiB is ample; bump if many concurrent pushes.
variable "lxc_cores" {
  type    = number
  default = 2
}
variable "lxc_memory_mib" {
  type    = number
  default = 2048
}
variable "lxc_rootfs_datastore" {
  type    = string
  default = "local-lvm"
}
variable "lxc_rootfs_gb" {
  description = "Root filesystem size (GiB). Registry image data lives on the separate mp0 data disk, not here."
  type        = number
  default     = 16
}

# Registry image store -> mounted at /var/lib/registry (mp0). An ALLOCATED volume (not a bind
# mount), so it grows online later with `pct resize <vmid> mp0 +NG` or by bumping this + tofu apply
# (grow-only; the pool must have free space).
# Bumped 64 -> 192 GiB: at 64 GiB the store filled (no retention pre-fix), 100% full → blob writes
# failed (`blob upload unknown` / `provided digest did not match`) while reads still served, which
# reds cchifor/platform CI builds. 192 GiB gives headroom alongside the new storage.retention
# policy (config.json.j2). Applied live via `pct resize 5004 mp0 192G`; this keeps tofu in sync.
variable "data_datastore" {
  type    = string
  default = "local-lvm"
}
variable "data_gb" {
  type    = number
  default = 192
}

# ---- Debian 13 LXC template (matches ai-lxc). Distinct file_name so a destroy here never deletes
#      the ai-lxc module's shared template out from under it. ----
variable "template_datastore" {
  description = "Datastore for the LXC template. qnap-nfs is shared + content-typed for vztmpl."
  type        = string
  default     = "qnap-nfs"
}
variable "template_download_node" {
  type    = string
  default = "ai-node1"
}
variable "lxc_template_url" {
  type    = string
  default = "http://download.proxmox.com/images/system/debian-13-standard_13.1-2_amd64.tar.zst"
}
variable "lxc_template_file" {
  description = "Distinct from ai-lxc's file_name (registry- prefix) so module destroys don't collide on the shared datastore."
  type        = string
  default     = "registry-debian-13-standard_13.1-2_amd64.tar.zst"
}

# ---- The one registry LXC ----
# IP .36 is free static (the .5-.36 reserve, below the router DHCP pool at .51 — no router change).
# vmid 5004 is the next free LXC id (ai-llm uses 5001-5003).
variable "registry_lxc" {
  type = object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  })
  default = {
    node_name = "ai-node1"
    vm_id     = 5004
    ip        = "192.168.0.36"
    hostname  = "registry"
  }
}

# cloud-init style SSH key so Ansible can reach the CT (the same key tofu uses elsewhere).
variable "ssh_public_key" {
  description = "Public key injected into the CT root account for Ansible access."
  type        = string
}
