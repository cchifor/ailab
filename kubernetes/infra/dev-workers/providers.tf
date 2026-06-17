# Authenticates with the tofu API token (same as the Talos infra/ + runners modules), NOT root@pam.
# These are plain QEMU VMs — no LXC device passthrough / bind mounts — so the root@pam-only
# restriction that forces the ai-lxc module onto a password does NOT apply here. The ssh{} block
# is still required: bpg opens an SSH session to the target node to import the cloud image disk
# (qemu-img), exactly as the Talos VM + runners modules do.
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
