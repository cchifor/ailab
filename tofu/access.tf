# A resource pool to group AI-lab guests/storage. The tofu user + API token themselves are
# bootstrapped manually (chicken-and-egg) — see docs/runbooks/00-access-prereqs.md.
resource "proxmox_virtual_environment_pool" "ailab" {
  pool_id = "ailab"
  comment = "AI lab resources (managed by OpenTofu)"
}
