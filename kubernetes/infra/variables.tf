# ---- Proxmox connection (same token as the storage layer works) ----
variable "pve_endpoint" { type = string }
variable "pve_api_token" {
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

# ---- Talos / Kubernetes versions ----
variable "talos_version" {
  type    = string
  default = "v1.11.2"
}
variable "kubernetes_version" {
  type    = string
  default = "v1.31.4"
}

# ---- Cluster networking ----
variable "cluster_name" {
  type    = string
  default = "ai"
}
variable "cluster_vip" {
  description = "Shared control-plane VIP (k8s API endpoint)"
  type        = string
  default     = "192.168.0.40"
}
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
variable "nameservers" {
  type    = list(string)
  default = ["1.1.1.1", "9.9.9.9"]
}

# ---- Storage for VM disks / images ----
variable "vm_datastore" {
  description = "Proxmox datastore for Talos VM disks (per-node local NVMe)"
  type        = string
  default     = "local-lvm"
}
variable "image_datastore" {
  description = "Proxmox datastore that holds the downloaded Talos image"
  type        = string
  default     = "local"
}

# ---- Control-plane VM definitions (one per physical host) ----
variable "control_planes" {
  description = "Talos control-plane VMs (one per Proxmox host)"
  type = map(object({
    host_node = string # Proxmox node name (pvesh)
    vm_id     = number
    ip        = string
    cores     = number
    memory    = number # MiB
    disk_gb   = number
  }))
  # memory 32768 (32 GiB). The planned 32->24 GiB downsize (to free RAM for the ai-llm GPU
  # LXC) proved unnecessary: with the Vulkan backend the model lives in the iGPU VRAM heap,
  # so the LXC uses ~0.5 GiB system RAM and coexists with a 32 GiB CP VM (~33 GiB free).
  # Downsize to 24576 only if heavyweight (120B/122B) GTT spill needs the headroom — see
  # docs/runbooks/ai-host-setup.md. Talos has no memory hotplug; a change reboots the VM
  # (roll one node at a time, 3-CP HA tolerates one down).
  default = {
    cp1 = { host_node = "ai-node1", vm_id = 4001, ip = "192.168.0.41", cores = 8, memory = 32768, disk_gb = 40 }
    cp2 = { host_node = "ai-node2", vm_id = 4002, ip = "192.168.0.42", cores = 8, memory = 32768, disk_gb = 40 }
    cp3 = { host_node = "ai-node3", vm_id = 4003, ip = "192.168.0.43", cores = 8, memory = 32768, disk_gb = 40 }
  }
}

# Talos system extensions baked into the image (VMs; AI/GPU is a separate LXC, not here)
variable "talos_extensions" {
  type    = list(string)
  default = ["siderolabs/qemu-guest-agent", "siderolabs/iscsi-tools", "siderolabs/util-linux-tools"]
}
