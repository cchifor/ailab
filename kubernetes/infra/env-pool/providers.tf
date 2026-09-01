# Same auth model as agent-nodes/providers.tf: tofu API token + SSH for the bpg disk import
# (qemu-img on the target node). Plain QEMU VMs — no root@pam requirement.
provider "proxmox" {
  endpoint  = var.pve_endpoint
  api_token = var.pve_api_token
  insecure  = var.pve_insecure

  ssh {
    agent       = false
    username    = var.pve_ssh_username
    private_key = file(pathexpand(var.pve_ssh_key_path))
  }
}

# The talos provider is configured via resources/data sources (no provider block needed).
