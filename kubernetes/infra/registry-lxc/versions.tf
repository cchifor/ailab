terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.123.1" # pinned for the 2026-11-21 registry move's reviewed plans; relax afterwards
    }
  }
}
