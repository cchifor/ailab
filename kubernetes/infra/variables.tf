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
# talos_version = the RUNNING Talos (images; future installs). Upgraded live 2026-10-09 with
# scripts/talos-upgrade-node.sh (1.11.2 -> 1.11.6 -> 1.12.12 -> 1.13.11 -> 1.14.2); kubernetes_version
# via `talosctl upgrade-k8s` (1.31.4 -> 1.32.13 -> 1.33.13).
variable "talos_version" {
  type    = string
  default = "v1.14.2"
}
variable "kubernetes_version" {
  type    = string
  default = "v1.33.13"
}
variable "talos_config_contract" {
  # The Talos version the machine CONFIG (and talos_machine_secrets) is generated for - NOT the
  # running Talos. Pinned to the value already in state: a different contract regenerates the config
  # with that release's defaults (1.14 adds SecurityProfileConfig workloadIsolation -> sandboxd), and
  # a LOWER one (e.g. "v1.11" = v1.11.0) makes talos_machine_secrets replace the cluster PKI.
  # Moving it is a dated, reviewed migration of its own (plans/2026-10-08-talos-upgrade-program-plan.md, P8).
  type    = string
  default = "v1.11.2"
  validation {
    # Identical in infra, agent-nodes and env-pool (and in talos_machine_secrets state): a drifted copy
    # would render a different config schema for that pool. Move all three together, by plan.
    condition     = var.talos_config_contract == "v1.11.2"
    error_message = "talos_config_contract must stay v1.11.2 (the contract in state); moving it is a reviewed migration - see docs/runbooks/talos-upgrade.md."
  }
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
  # (24h peak <=10.4 GiB; the ~20-24 GiB `qm` "used" is mostly reclaimable guest page cache). cp3 stayed
  # 28 (node3 is lighter: 1 runner, no registry LXC) until the 2026-10-09 resize below made all three
  # equal. Talos has no memory hotplug, so a change reboots
  # the VM — roll ONE node at a time (3-CP HA tolerates one down), graceful-stop via `talosctl shutdown`
  # (ACPI/`qm shutdown` does NOT stop Talos), and verify `talosctl etcd status` 3/3 in-sync between nodes.
  # See docs/runbooks/ai-host-setup.md.
  #
  # cores 8 -> 10 and memory -> 32 GiB on all three (2026-10-09, applied live with `talosctl shutdown`,
  # `qm set`, `qm start`, one CP at a time during the Talos 1.11.6 roll; this default records it).
  # The working set is still ~8-10 GiB; the binding constraint is pod REQUESTS. The 3 CPs requested
  # ~15.7 CPU / 51 Gi, while any two allocated only 14 CPU / ~44 Gi, so draining one CP did not fit
  # (cp2's drain needed 5.2 CPU / 17 Gi into 4.4 / 13 free). Each CP now allocates 9 CPU / 27.8 Gi.
  # Host-RAM check and the per-node record: plans/2026-10-08-talos-upgrade-program-plan.md (ledger).
  # Re-check node1/node2 memory pressure (the dev-worker floors above) if host load grows.
  #
  # disk_gb 40 -> 80 (2026-09-27). /var (EPHEMERAL) was 38.6 GB with 20-27 GB of container images
  # per CP; cp2/cp3 dipped below the kubelet's ~3.86 GB eviction threshold in the week before
  # (DiskPressure), and 13 velero pods were evicted from cp1. local-lvm is thin with 0.57-1.28 TB
  # free per host. Growing is one-way (a disk cannot shrink). Talos grows EPHEMERAL into the new
  # space at BOOT, so the resize only lands after a reboot: roll one CP at a time — talosctl
  # shutdown, `tofu apply -target='proxmox_virtual_environment_vm.cp["cpN"]'` (NOT the machine
  # config resource first: it depends on ALL three VMs, so targeting it pulls every pending resize
  # in), start, then check `talosctl get volumestatus EPHEMERAL` and etcd 3/3.
  default = {
    cp1 = { host_node = "ai-node1", vm_id = 4001, ip = "192.168.0.41", host_ip = "192.168.0.2", storage_tier = "thunderbolt", cores = 10, memory = 32768, disk_gb = 80 }
    cp2 = { host_node = "ai-node2", vm_id = 4002, ip = "192.168.0.42", host_ip = "192.168.0.3", storage_tier = "thunderbolt", cores = 10, memory = 32768, disk_gb = 80 }
    cp3 = { host_node = "ai-node3", vm_id = 4003, ip = "192.168.0.43", host_ip = "192.168.0.4", storage_tier = "ethernet", cores = 10, memory = 32768, disk_gb = 80 }
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
