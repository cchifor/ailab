# Authenticates with the tofu API token (same as the Talos infra/, agent-nodes, runners and
# dev-workers modules), NOT root@pam: these are plain QEMU VMs (Talos guests) with no device
# passthrough. The ssh{} block is required: bpg opens an SSH session to the target node to import the
# Talos nocloud disk (qemu-img), exactly as the agent-nodes module does.
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
