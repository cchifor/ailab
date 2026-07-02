###############################################################################
# Interactive dev-worker VMs — one per Proxmox node.
# Ubuntu 24.04 boxes that run Claude Code + Codex inside tmux for interactive,
# persistent agent development (the ailab port of the homelab "claude-worker" VMs).
# Guest config is applied by the ansible role `dev_worker` (just dev-workers).
# Provider: bpg/proxmox ~> 0.109 (api_token + ssh, same as the runners + Talos modules).
###############################################################################

# Ubuntu 24.04 LTS (noble) cloud image, downloaded ONCE to the shared qnap-nfs datastore
# (mounted on all nodes), so every node's VM imports the same file. DISTINCT filename from the
# runners module's copy so a destroy of either module can't delete the other's image (see variables).
resource "proxmox_virtual_environment_download_file" "ubuntu_cloud" {
  content_type = "import" # VM disk import_from requires the source in the "import" content type (NOT iso)
  datastore_id = var.image_datastore
  node_name    = var.image_download_node
  url          = var.ubuntu_cloud_image_url
  file_name    = var.ubuntu_cloud_image_file
  overwrite    = false
}

resource "proxmox_virtual_environment_vm" "dev_worker" {
  for_each = var.dev_worker_nodes

  name      = each.value.hostname
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["vm", "dev", "agent", "claude-code", "ailab"]

  # The minimal Ubuntu cloud image does NOT ship qemu-guest-agent, so leave the agent disabled:
  # with it enabled, bpg would block `apply` waiting for an agent that never reports, then time out.
  # The static IP comes from cloud-init (below), not the agent. The dev_worker role installs +
  # enables qemu-guest-agent on the guest; flip this to true afterward if you want PVE<->agent info.
  agent {
    enabled = false
  }
  stop_on_destroy = true

  cpu {
    cores = var.dev_worker_cores
    type  = "host" # homogeneous CPUs; no live migration (dev workers are rebuildable)
  }

  # Ballooning: VM floats between floating (per-node idle floor) and dedicated (load ceiling), so idle
  # dev workers return RAM to the host. Important on the memory-tight Strix Halo nodes (GPU VRAM
  # carve + a Talos CP VM per node) — see docs/runbooks/dev-workers.md. The floor is PER-NODE
  # (each.value.floating) because host headroom differs after the 2026-07-02 CP downsize — see variables.tf.
  memory {
    dedicated = var.dev_worker_memory_mib
    floating  = each.value.floating
  }

  scsi_hardware = "virtio-scsi-single"

  # Root disk (scsi0): imported from the Ubuntu cloud image; cloud-init growpart expands it.
  disk {
    datastore_id = var.vm_datastore
    import_from  = proxmox_virtual_environment_download_file.ubuntu_cloud.id
    interface    = "scsi0"
    size         = var.dev_worker_rootfs_gb
    iothread     = true
    discard      = "on"
  }

  # Data disk (scsi1): blank raw volume (no import_from). The dev_worker role partitions, mkfs's,
  # and mounts it at /workspace (and relocates docker's data-root there), keeping agent/build churn
  # off the small OS disk. bpg keys disks by `interface`, not block order, so scsi0/scsi1 are stable.
  disk {
    datastore_id = var.vm_datastore
    interface    = "scsi1"
    size         = var.dev_worker_workspace_gb
    iothread     = true
    discard      = "on"
  }

  network_device {
    bridge = var.bridge
  }

  # Static IP + the ansible SSH key, both via cloud-init on first boot. The `c4` user is created by
  # Proxmox cloud-init as a passwordless sudoer with this key (same mechanism the runners module
  # uses for `ubuntu`), so `ssh c4@<ip>` and `just dev-workers` (ansible) work on first boot with
  # no manual step. The dev_worker role does NOT recreate c4; it only augments its groups.
  initialization {
    datastore_id = var.vm_datastore
    ip_config {
      ipv4 {
        address = "${each.value.ip}/${var.network_prefix}"
        gateway = var.gateway
      }
    }
    dns {
      domain  = var.dns_domain
      servers = var.nameservers
    }
    user_account {
      username = "c4" # interactive sudo user (ansible_user), matches the homelab claude-worker
      keys     = [var.dev_worker_ssh_public_key]
    }
  }

  operating_system {
    type = "l26"
  }

  lifecycle {
    # Avoid churn after first boot (cloud-init only applies once); ignore image_datastore drift.
    ignore_changes = [initialization]
  }
}
