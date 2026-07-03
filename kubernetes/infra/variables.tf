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
    host_node    = string # Proxmox node name (pvesh)
    vm_id        = number
    ip           = string
    host_ip      = string # the Proxmox host's vmbr0 IP — next-hop for the TB storage route (WS2)
    storage_tier = string # "thunderbolt" | "ethernet" — node label so fast-storage workloads prefer TB nodes (WS2)
    cores        = number
    memory       = number # MiB
    disk_gb      = number
  }))
  # Per-node memory (MiB). cp2 32->24 / cp3 32->28 GiB (2026-07-02), then cp1 32->24 GiB (2026-07-03),
  # all downsized to free host RAM for the co-located dev-worker VMs (which OOM-thrashed at their 2 GiB
  # balloon floor under host oversubscription; node1 needed the same once its dev-worker floor was
  # raised to 8 GiB, leaving node1 at ~5.6 GiB swap). Safe: measured CP working set is only ~8-10 GiB
  # (24h peak <=10.4 GiB; the ~20-24 GiB `qm` "used" is mostly reclaimable guest page cache). cp3 stays
  # 28 (node3 is lighter: 1 runner, no registry LXC). Talos has no memory hotplug, so a change reboots
  # the VM — roll ONE node at a time (3-CP HA tolerates one down), graceful-stop via `talosctl shutdown`
  # (ACPI/`qm shutdown` does NOT stop Talos), and verify `talosctl etcd status` 3/3 in-sync between nodes.
  # See docs/runbooks/ai-host-setup.md.
  default = {
    cp1 = { host_node = "ai-node1", vm_id = 4001, ip = "192.168.0.41", host_ip = "192.168.0.2", storage_tier = "thunderbolt", cores = 8, memory = 24576, disk_gb = 40 }
    cp2 = { host_node = "ai-node2", vm_id = 4002, ip = "192.168.0.42", host_ip = "192.168.0.3", storage_tier = "thunderbolt", cores = 8, memory = 24576, disk_gb = 40 }
    cp3 = { host_node = "ai-node3", vm_id = 4003, ip = "192.168.0.43", host_ip = "192.168.0.4", storage_tier = "ethernet", cores = 8, memory = 28672, disk_gb = 40 }
  }
}

# WS2 (Thunderbolt CSI): the QNAP storage service IP on the TB/storage fabric. Each VM reaches it via
# a /32 route through its own Proxmox host (host_ip), which forwards + SNATs over Thunderbolt. The
# route is harmless until CSI is cut over to this IP (deferred). See docs/decisions/0011 + the runbook.
variable "storage_service_ip" {
  type    = string
  default = "10.55.0.254"
}

# Talos system extensions baked into the image (VMs; AI/GPU is a separate LXC, not here)
variable "talos_extensions" {
  type    = list(string)
  default = ["siderolabs/qemu-guest-agent", "siderolabs/iscsi-tools", "siderolabs/util-linux-tools"]
}
