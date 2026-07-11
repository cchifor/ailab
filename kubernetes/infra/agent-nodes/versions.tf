terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111" # same pin as the Talos (infra/), ai-lxc, runners, and dev-workers modules
    }
    # Worker JOIN reuses the EXISTING cluster PKI: the talos provider here only renders the worker
    # machine-config + applies it. The machine_secrets themselves come from infra/ remote state
    # (talos-agent join reuses talos_machine_secrets.this) — see talos.tf. Pin == infra/versions.tf.
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.11"
    }
  }
}
