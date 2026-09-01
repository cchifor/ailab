# ---- Proxmox connection (same tofu API token as infra/ + runners + dev-workers + agent-nodes) ----
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

# ---- Cross-module wiring ----
variable "infra_state_path" {
  description = <<-EOT
    Absolute path to the CP root module's local state (kubernetes/infra/terraform.tfstate) — read
    READ-ONLY for machine_secrets/client_configuration (worker join, ADR 0019 Option B). A variable
    (not the agent-nodes relative "../terraform.tfstate") because this module may be applied from a
    checkout that is not a sibling of the state-holding checkout.
  EOT
  type        = string
}
variable "kubeconfig_path" {
  description = "Absolute path to the admin kubeconfig used to apply the cluster-side label/taint (node-labels.tf)."
  type        = string
}

# ---- Network ----
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

# ---- Storage ----
variable "vm_datastore" {
  type    = string
  default = "local-lvm"
}
variable "image_datastore" {
  description = "Datastore holding the staged Talos agent (kata+gvisor) nocloud raw image."
  type        = string
  default     = "local"
}

# ---- Talos / Kubernetes identity — MUST match the LIVE cluster ----
variable "talos_version" {
  type    = string
  default = "v1.11.2"
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
  type    = string
  default = "192.168.0.40"
}
variable "storage_service_ip" {
  type    = string
  default = "10.55.0.254"
}

# ---- Env-node sizing ----
variable "env_node_cores" {
  type    = number
  default = 8
}
variable "env_node_memory_mib" {
  description = <<-EOT
    Dedicated VM memory (MiB), no balloon. 16384 for the spike node (live host MemAvailable was
    ~25 GiB/node on 2026-09-01 — the plan's 28 GiB big-flavor node needs host RAM freed first; see
    the spike-sizing note in main.tf).
  EOT
  type        = number
  default     = 16384
}
variable "env_node_disk_gb" {
  type    = number
  default = 60
}

# ---- Env-pool VMs ----
# vmid band 44xx (free: CPs 40xx, runners 41xx, dev-workers 42xx, agent-nodes 43xx, LXCs 50xx).
# IP .37: inside the .2-.50 static reserve; verified free 2026-09-01 (live ping/ARP negative,
# cloudlab repo negative — network-plan.md's ".20-.35 free" is STALE, cloudlab took .20-.22/.26/.28).
variable "env_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    host_ip   = string
    hostname  = string
  }))
  default = {
    "env-node-1" = { node_name = "ai-node2", vm_id = 4401, ip = "192.168.0.37", host_ip = "192.168.0.3", hostname = "env-node-1" }
  }
}
