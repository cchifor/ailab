# Authenticates as root@pam (password), same as the ai-lxc module. An allocated mount_point
# volume doesn't strictly require root@pam (no device passthrough / no host bind mount), but we
# reuse the proven LXC connection pattern + the .env NODE_ROOT_PASSWORD that ai-lxc already uses.
provider "proxmox" {
  endpoint = var.pve_endpoint
  username = var.pve_username
  password = var.pve_password
  insecure = var.pve_insecure

  ssh {
    agent       = false
    username    = var.pve_ssh_username
    private_key = file(pathexpand(var.pve_ssh_key_path))
  }
}
