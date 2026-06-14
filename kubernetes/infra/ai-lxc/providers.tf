# Separate root module => separate state, so the AI appliance can be built/destroyed
# without ever planning the Talos control-plane VMs.
#
# IMPORTANT: authenticates as root@pam (password), NOT the tofu API token. Proxmox
# restricts LXC *device passthrough* and *bind mounts* to root@pam — the check is a
# literal `authuser eq 'root@pam'`, which API tokens never satisfy (403 otherwise).
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
