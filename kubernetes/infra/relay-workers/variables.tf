# ---- Proxmox connection (same tofu API token as infra/, agent-nodes, runners, dev-workers) ----
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

# ---- Network (mgmt LAN; matches the Talos + agent-nodes modules) ----
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
  description = "Datastore for the relay-worker VM disks (per-node local NVMe thin pool). Never qnap-*: ADR 0037."
  type        = string
  default     = "local-lvm"
}
variable "image_datastore" {
  description = <<-EOT
    Datastore that holds the STAGED Talos nocloud image (talos-<ver>-nocloud-amd64.raw), as for the CPs
    and agent nodes. The factory image is xz, which bpg cannot decompress, so stage it on the target
    node with scripts/stage-talos-image.sh BEFORE apply (TALOS_VER=<talos_version>; its default
    SCHEMATIC is the plain one these nodes run).
  EOT
  type        = string
  default     = "local"
}

# ---- Talos / Kubernetes identity: MUST match the LIVE cluster exactly (workers join it) ----
# Mirrors infra/variables.tf and agent-nodes/variables.tf. Bump talos_version in all three modules in
# the same change as a Talos upgrade, and kubernetes_version right after each upgrade-k8s hop
# (docs/runbooks/talos-upgrade.md).
variable "talos_version" {
  type    = string
  default = "v1.14.2"
}
variable "kubernetes_version" {
  type    = string
  default = "v1.35.9"
}
variable "talos_config_contract" {
  # The Talos version the machine CONFIG is generated for, NOT the running Talos. Identical in infra,
  # agent-nodes and here (and in talos_machine_secrets state); moving it is a reviewed migration
  # (docs/runbooks/talos-upgrade.md).
  type    = string
  default = "v1.11.2"
  validation {
    condition     = var.talos_config_contract == "v1.11.2"
    error_message = "talos_config_contract must stay v1.11.2 (the contract in state); moving it is a reviewed migration - see docs/runbooks/talos-upgrade.md."
  }
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


# ---- The relay-worker node pool (ailab #1675, Relay plan docs/plans/2026-11-08-infrastructure-workers) ----
# Dedicated Talos workers for Relay-managed workers: lightweight roles as pods, the developer role as
# KubeVirt VMs (pod-network masquerade only). Labelled ailab.io/relay-worker-pool=true and tainted
# dedicated=relay-worker:NoSchedule, with NO ailab.io/agent-pool label, so Relay and llm-router pods
# (they tolerate only dedicated=agent) can never land here, and nothing here lands on the agent pool.
#
# One node per Proxmox host when the pool grows: VMs are host-bound, so a host's RAM can only feed a
# node on that host, and per-host nodes keep the failure domain equal to the host.
#
# Sizing is per node, FIXED (no balloon: KubeVirt guests need a stable floor and the kubelet's
# allocatable is static). relay-worker-1 = 12 vCPU / 24 GiB / 200 GiB on ai-node3, the only host with
# room (7-day minimum MemAvailable 2026-11-08: ai-node1 2.5 GiB, ai-node2 10.4 GiB, ai-node3
# 33.6 GiB). 24 GiB leaves ai-node3 about 9.6 GiB at that minimum; 32 GiB would have left 1.6 GiB.
#
# vmid band 4304-4306 (next to agent-nodes 4301-4303). Addresses come from docs/network-plan.md ONLY,
# and every node IP must also be in kubernetes/edge/hostnames.yaml trusted_sources.exclude: pod and VM
# egress is SNATed to the node IP, and the edge would otherwise trust worker code as a LAN (or, at
# .52 and up, an admin) source.
#
# `ip` is consumed by cloud-init at create (ignored after first boot) AND by the machine-config
# template, so keep it equal to the live address. Renumbering takes the three edits in
# docs/network-plan.md.
variable "relay_worker_nodes" {
  type = map(object({
    node_name  = string
    vm_id      = number
    ip         = string
    cores      = number
    memory_mib = number
    disk_gb    = number
  }))
  default = {
    "relay-worker-1" = {
      node_name  = "ai-node3"
      vm_id      = 4304
      ip         = "192.168.0.52"
      cores      = 12
      memory_mib = 24576
      disk_gb    = 200
    }
  }
  validation {
    condition     = alltrue([for k, v in var.relay_worker_nodes : v.vm_id >= 4304 && v.vm_id <= 4306])
    error_message = "relay-worker vm_ids live in the 4304-4306 band (docs/network-plan.md, CLAUDE.md inventory)."
  }
  validation {
    condition     = alltrue([for k in keys(var.relay_worker_nodes) : can(regex("^relay-worker-[1-9][0-9]*$", k))])
    error_message = "Map keys are relay-worker-<n> (never a retired testpool/env-node/agentforge name). The key names the VM, talos-<key>, which Proxmox's cloud-init metadata makes the Talos hostname and Kubernetes node name."
  }
  validation {
    condition     = length(distinct([for v in values(var.relay_worker_nodes) : v.node_name])) == length(var.relay_worker_nodes)
    error_message = "At most one relay-worker node per Proxmox host: the host is the failure domain."
  }
  validation {
    condition     = alltrue([for k, v in var.relay_worker_nodes : v.memory_mib >= 16384 && v.memory_mib % 1024 == 0 && v.cores >= 4 && v.disk_gb >= 100])
    error_message = "A relay-worker node needs at least 4 vCPU, 16 GiB (whole GiB) and a 100 GiB disk: one developer VM is 4 vCPU / 8 GiB guest plus launcher overhead and a 30 GiB ephemeral budget."
  }
}
