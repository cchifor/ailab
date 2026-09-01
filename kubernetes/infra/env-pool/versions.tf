terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111" # same pin as infra/, ai-lxc, runners, dev-workers, agent-nodes
    }
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.11" # pin == infra/versions.tf; secrets come from infra/ remote state
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.30"
    }
  }
}
