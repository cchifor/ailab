###############################################################################
# Dedicated PR-reviewer VMs — the automatic review bots' home, isolated from the
# dev workers so feature work and reviews never contend (operator-directed,
# 2026-09-02). Guest config: ansible/reviewers.yml. Provider auth: same tofu API
# token as the dev-workers/runners/Talos modules.
###############################################################################

resource "proxmox_virtual_environment_download_file" "ubuntu_cloud" {
  content_type = "import"
  datastore_id = var.image_datastore
  node_name    = "ai-node3" # both reviewers live here; download once on the target node
  url          = var.ubuntu_cloud_image_url
  file_name    = var.ubuntu_cloud_image_file
  overwrite    = false
}

resource "proxmox_virtual_environment_vm" "reviewer" {
  for_each = var.reviewer_nodes

  name      = each.value.hostname
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["vm", "reviewer", "llm", "ailab"]

  # Minimal cloud image ships no qemu-guest-agent (same as dev-workers): leave disabled
  # or bpg blocks apply waiting on an agent that never reports.
  agent {
    enabled = false
  }
  stop_on_destroy = true

  cpu {
    cores = var.reviewer_cores
    type  = "host"
  }

  # FIXED memory, no balloon: tiny steady footprint, and fixed sizing keeps the node
  # budget arithmetic honest after the 2026-09-01 balloon-floor incident on node2.
  memory {
    dedicated = var.reviewer_memory_mib
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    import_from  = proxmox_virtual_environment_download_file.ubuntu_cloud.id
    interface    = "scsi0"
    size         = var.reviewer_rootfs_gb
    iothread     = true
    discard      = "on"
  }

  network_device {
    bridge = var.bridge
  }

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
      username = "c4" # same admin user as the dev workers; ansible_user
      keys     = [var.reviewer_ssh_public_key]
    }
  }

  operating_system {
    type = "l26"
  }

  lifecycle {
    ignore_changes = [initialization]
  }
}
