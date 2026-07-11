# ---- Proxmox connection (same tofu API token as the Talos infra/ + runners + dev-workers modules) ----
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

# ---- Network (mgmt LAN; matches the Talos + ai-lxc + runners + dev-workers modules) ----
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
  description = "Datastore for the agent-node VM disks (per-node local NVMe)."
  type        = string
  default     = "local-lvm"
}
variable "image_datastore" {
  description = <<-EOT
    Datastore that holds the STAGED Talos nocloud image (talos-<ver>-nocloud-amd64.raw). Same as the
    CP module (infra/variables.tf) = `local`, NOT qnap-nfs: the factory image is xz and must be staged
    per node by scripts/stage-talos-image.sh (bpg cannot decompress xz). Stage BEFORE apply.
  EOT
  type        = string
  default     = "local"
}

# ---- Talos / Kubernetes identity — MUST match the LIVE cluster exactly (workers join it) ----
# These mirror infra/variables.tf. A mismatch (wrong version/endpoint) makes the worker fail to join.
variable "talos_version" {
  type    = string
  default = "v1.11.2"
}

# ---- Talos system extensions baked into the AGENT-POOL boot image (P2) ----
# Superset of the CP base set (infra/variables.tf: qemu-guest-agent for the QEMU guest agent that
# main.tf's `agent{enabled=true}` needs, + iscsi-tools/util-linux-tools for parity/CSI) PLUS the two
# sandbox runtimes:
#   - siderolabs/kata-containers → registers the containerd handler `kata` (QEMU microVM; the real
#     security boundary). Needs /dev/kvm inside the worker ⇒ cpu.type=host (main.tf) + `kvm_amd
#     nested=1` on the Proxmox host (operator prereq, docs/runbooks/agent-nodes.md) + the
#     vhost_net/vhost_vsock kernel modules (machine-config/worker.yaml.tftpl).
#   - siderolabs/gvisor → registers the containerd handler `runsc` (user-space; the compute-only /
#     no-KVM fallback runtime).
# Both are OFFICIAL Image Factory extensions (verified on factory.talos.dev for v1.11.2). The runtime
# handlers are auto-registered by the extensions — no CRI config patch is required; only the
# RuntimeClasses (kubernetes/apps/infrastructure/agentforge-runtimeclasses/) point at these handlers.
#
# ── SWITCH back to the PLAIN P1 pool: remove kata-containers + gvisor from this list (leave the CP
#    base set), then re-stage the image. The schematic ID changes → a plain agent image is baked under
#    the same filename; the pool still boots, just without the Kata/gVisor handlers. ──
variable "talos_extensions" {
  type = list(string)
  default = [
    "siderolabs/qemu-guest-agent",
    "siderolabs/iscsi-tools",
    "siderolabs/util-linux-tools",
    "siderolabs/kata-containers",
    "siderolabs/gvisor",
  ]
}
variable "kubernetes_version" {
  type    = string
  default = "v1.31.4"
}
variable "cluster_name" {
  type    = string
  default = "ai"
}
variable "cluster_vip" {
  description = "Shared control-plane VIP (k8s API endpoint) the worker registers against."
  type        = string
  default     = "192.168.0.40"
}

# ---- Agent-node VM sizing ----
# Dedicated Kata-capable Talos worker pool (AgentForge v2). NO ballooning: the memory floor is fixed
# because Kata microVMs (P2) want a stable RAM reservation. cpu.type=host (main.tf) is REQUIRED so
# nested SVM passes through for /dev/kvm (Kata). P1 ships PLAIN workers (no Kata yet); the sizing is
# already set for the P2 sandbox footprint (~512 MiB–1 GiB per job, see the plan's analyzer note).
variable "agent_node_cores" {
  type    = number
  default = 8
}
variable "agent_node_memory_mib" {
  description = "Dedicated VM memory (MiB). Tune to the per-node host RAM budget (see docs/runbooks/agent-nodes.md + dev-workers.md)."
  type        = number
  default     = 16384
}
variable "agent_node_disk_gb" {
  description = "Root disk (scsi0) size in GiB; imported from the Talos nocloud raw image."
  type        = number
  default     = 60
}

# ---- Talos agent-worker VMs (one per physical host) ----
# Extends the CLAUDE.md inventory: vmid band 4301-4303 (free — CPs 4001-4003, runners 4101-4105,
# dev-workers 4201-4206, AI LXC 5001-5003, registry 5004), IPs .14-.16 (consecutive, inside the
# .2-.50 static reserve, below the router DHCP pool at .51 — no router change). Placement one-per-node
# for fault isolation. NOTE: cloud-init/nocloud sets the IP at create and
# lifecycle.ignore_changes=[initialization] means editing `ip` here is DOCUMENTATION ONLY once booted.
variable "agent_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  }))
  default = {
    "agent-node-1" = { node_name = "ai-node1", vm_id = 4301, ip = "192.168.0.14", hostname = "agent-node-1" }
    "agent-node-2" = { node_name = "ai-node2", vm_id = 4302, ip = "192.168.0.15", hostname = "agent-node-2" }
    "agent-node-3" = { node_name = "ai-node3", vm_id = 4303, ip = "192.168.0.16", hostname = "agent-node-3" }
  }
}
