terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.109" # same pin as the Talos (infra/), ai-lxc, and runners modules
    }
  }
}
