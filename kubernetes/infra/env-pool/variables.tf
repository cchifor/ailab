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
# talos_version = the RUNNING Talos (images; future installs). Upgraded live 2026-10-09 with
# scripts/talos-upgrade-node.sh (1.11.2 -> 1.11.6 -> 1.12.12 -> 1.13.11 -> 1.14.2); kubernetes_version
# via `talosctl upgrade-k8s` (1.31.4 -> 1.32.13 -> 1.33.13).
variable "talos_version" {
  type    = string
  default = "v1.14.2"
}
variable "kubernetes_version" {
  type    = string
  default = "v1.34.12" # live since the 2026-10-09 hop (upgrade-k8s + per-node kubelet roll); bump right after every hop
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

# ---- Kata debug evidence (plans/2026-09-20-env-pool-root-cause-followup-plan.md, T2) ----
variable "kata_debug" {
  description = <<-EOT
    DEFAULT false since 2026-10-09: the embedded machine-config/kata/configuration.toml is a verbatim copy
    of the kata 3.20 extension's config and does NOT match the kata 3.26+ extension of Talos 1.12+.
    Re-enable only after replacing it with the CURRENT extension's configuration.toml.
    true = deliver machine.files that (1) raise containerd to [debug] level and point the `kata`
    runtime at /var/etc/kata-containers/configuration.toml (machine-config/cri-20-customization.part),
    (2) install that file as a verbatim copy of the extension's config (machine-config/kata/
    configuration.toml) and (3) install the config.d/10-debug.toml drop-in. false renders the
    pre-2026-09-21 machine config byte-for-byte (the adoption/rollback setting). Any change of this
    value is a boot-time file change: tofu apply in "staged" mode, then `talosctl reboot`
    (docs/runbooks/env-pool.md). Every drop-in that may exist on a node is an explicit entry in
    worker.yaml.tftpl — a file under machine-config/ alone ships nothing.
  EOT
  type        = bool
  default     = false # 2026-10-09: the 3.20 verbatim config does not match the kata 3.26+ extension of Talos 1.12+;
  # removed live (staged) before the 1.12.12 reboot, kata-env verified on 1.12/1.13/1.14. Re-enable only with a
  # configuration.toml copied from the CURRENT extension.
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
# apply_mode (talos_machine_configuration_apply, provider 0.11): a RUNNING env node uses "staged" —
# tofu writes the new machine config to the STATE partition and the operator reboots via talosctl at
# an announced window; tofu never reboots or live-edits the pool's node. "no_reboot" is the adoption
# setting (Talos refuses, with the diff in the error, anything that would need a reboot). A brand-new
# node's FIRST apply must be "auto" (maintenance mode accepts only auto|reboot|try) and is flipped to
# "staged" right after. staged_if_needing_reboot is excluded on purpose: it silently falls back to
# "auto" (= may reboot) whenever the plan-time dry-run cannot reach the node.
variable "env_nodes" {
  type = map(object({
    node_name  = string
    vm_id      = number
    ip         = string
    host_ip    = string
    hostname   = string
    apply_mode = optional(string, "auto")
  }))
  default = {
    "env-node-1" = { node_name = "ai-node2", vm_id = 4401, ip = "192.168.0.37", host_ip = "192.168.0.3", hostname = "env-node-1", apply_mode = "staged" }
  }
  validation {
    condition     = alltrue([for n in values(var.env_nodes) : contains(["auto", "reboot", "no_reboot", "staged"], n.apply_mode)])
    error_message = "env_nodes[*].apply_mode must be one of auto, reboot, no_reboot, staged (staged_if_needing_reboot is deliberately excluded)."
  }
}
