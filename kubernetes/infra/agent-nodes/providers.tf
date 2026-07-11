# Authenticates with the tofu API token (same as the Talos infra/ + runners + dev-workers modules),
# NOT root@pam. These are plain QEMU VMs (Talos guests) — no LXC device passthrough — so the
# root@pam-only restriction that forces the ai-lxc module onto a password does NOT apply. The ssh{}
# block is still required: bpg opens an SSH session to the target node to import the Talos nocloud
# disk (qemu-img), exactly as the Talos VM + dev-workers modules do.
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
