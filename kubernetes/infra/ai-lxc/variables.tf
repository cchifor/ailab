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

# ---- Optional local-NVMe model cache (fast cold loads for on-demand/llama-swap models) ----
# A per-node managed volume mounted at /models-local. Staging a heavyweight GGUF here (from the NFS
# share) cuts cold-load read time ~7-15x vs NFS (~5 min -> ~30-60 s) — see docs/runbooks/ai-model-swap.md.
# Enabled per node via ai_llm_nodes[].model_cache_gb (node2/node3, the llama-swap heavyweights). node1's
# daily driver is pinned/warm, so it is left without one (node1's LXC is untouched). local-lvm is
# thin-provisioned, so the sized volume only consumes what is actually staged.
variable "model_cache_datastore" {
  description = "Datastore for the per-node local model cache (fast NVMe). Thin, so the size is a cap, not a reservation."
  type        = string
  default     = "local-lvm"
}

# ---- LXC sizing ----
variable "lxc_memory_mib" {
  description = <<-EOT
    Hard memory cap (MiB, host OOM fence). ~96 GiB for the small-carve + large-GTT config:
    with a reduced BIOS VRAM carve the model weights live in GTT (pageable system RAM) and
    ARE charged to this cgroup, so the cap must cover the largest model (122B ~72 GiB) +
    headroom. Validated on node2 (2026-07-06): gpt-oss served from GTT charges ~10-35 GiB
    here, no OOM. (On the OLD 64 GiB carve, 24 GiB sufficed — weights were firmware-reserved
    VRAM, NOT charged here.) A cap, not a reservation: harmless on nodes still on the 64 GiB
    carve. See docs/runbooks/ai-host-setup.md.
  EOT
  type        = number
  default     = 98304
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
# IPs .44/.45/.46 (static-reserved block, adjacent to the Talos nodes .41-.43). The original .51-.53
# were abandoned because .51+ is the router's DHCP pool — see the inline note below + docs/network-plan.md.
variable "ai_llm_nodes" {
  type = map(object({
    node_name      = string
    vm_id          = number
    ip             = string
    hostname       = string
    model_cache_gb = optional(number) # >0 => add a /models-local mount on local-lvm for fast cold loads
  }))
  default = {
    # IPs moved from .51-.53 into the static-reserved block (.2-.50, outside the router's DHCP pool) to
    # end an IP conflict — the router had leased .53 to a DHCP client (an MXCHIP IoT device). See
    # docs/runbooks/ai-host-setup.md. Adjacent to the Talos nodes (.41-.43).
    # model_cache_gb: node2/node3 stage their idle-unloaded heavyweight on local NVMe for fast cold
    # loads (llama-swap). node1 (pinned daily driver) has none — leave it untouched.
    "ai-llm-1" = { node_name = "ai-node1", vm_id = 5001, ip = "192.168.0.44", hostname = "ai-llm-1" }
    "ai-llm-2" = { node_name = "ai-node2", vm_id = 5002, ip = "192.168.0.45", hostname = "ai-llm-2", model_cache_gb = 160 }
    "ai-llm-3" = { node_name = "ai-node3", vm_id = 5003, ip = "192.168.0.46", hostname = "ai-llm-3", model_cache_gb = 160 }
  }
}
