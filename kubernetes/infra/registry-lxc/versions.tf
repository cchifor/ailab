terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.123" # keep in step with the other bpg/proxmox stacks
    }
  }
}
