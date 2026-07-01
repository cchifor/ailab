###############################################################################
# Self-hosted GitHub Actions runner VMs — two per Proxmox node (6-wide pool; see variables.tf).
# Replicates the existing cchifor/platform "self-hosted-hv" ephemeral pool
# (Multipass/Hyper-V Ubuntu 24.04) onto the codified lab as Proxmox QEMU VMs.
# Docker-heavy CI (compose v2.31.0 + buildx + Playwright-in-container + k6).
# Host/runner config is applied by the ansible role `github_runner` (just runners).
# Provider: bpg/proxmox ~> 0.109 (api_token + ssh, same as the Talos infra/ module).
###############################################################################

# Ubuntu 24.04 LTS (noble) cloud image, downloaded ONCE to the shared qnap-nfs datastore
# (mounted on all nodes), so every node's VM imports the same file. The image is qcow2 (.img),
# not xz, so download_file imports it directly (unlike the Talos factory image — see infra/image.tf).
resource "proxmox_virtual_environment_download_file" "ubuntu_cloud" {
  content_type = "import" # VM disk import_from requires the source in the "import" content type (NOT iso)
  datastore_id = var.image_datastore
  node_name    = var.image_download_node
  url          = var.ubuntu_cloud_image_url
  file_name    = var.ubuntu_cloud_image_file
  overwrite    = false
}

resource "proxmox_virtual_environment_vm" "runner" {
  for_each = var.runner_nodes

  name      = each.value.hostname
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["vm", "ci", "github-runner", "ailab"]

  # The minimal Ubuntu cloud image does NOT ship qemu-guest-agent, so leave the agent disabled:
  # with it enabled, bpg would block `apply` waiting for an agent that never reports, then time out.
  # The static IP comes from cloud-init (below), not the agent. The github_runner role installs +
  # enables qemu-guest-agent on the guest; flip this to true afterward if you want PVE<->agent info.
  agent {
    enabled = false
  }
  stop_on_destroy = true

  cpu {
    cores = var.runner_cores
    type  = "host" # homogeneous CPUs; no live migration (runners are rebuildable)
  }

  # Ballooning: VM floats between floating (idle floor, 12 GiB) and dedicated (load ceiling, 24 GiB),
  # so idle runners return RAM to the host. Independent of the runner service's systemd MemoryMax cap.
  # The floor is held at 12 GiB (not the bpg default 1 GiB) because host RAM pressure was ballooning
  # the guests down to 1-2 GiB and OOM-killing CI jobs — see cchifor/platform#620 + variables.tf.
  memory {
    dedicated = var.runner_memory_mib
    floating  = var.runner_memory_floating_mib
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    import_from  = proxmox_virtual_environment_download_file.ubuntu_cloud.id
    interface    = "scsi0"
    size         = var.runner_rootfs_gb # cloud-init growpart expands the root fs to fill this
    iothread     = true
    discard      = "on"
  }

  network_device {
    bridge = var.bridge
  }

  # Static IP + the ansible SSH key, both via cloud-init on first boot. Seeding the key here is
  # what lets `just runners` (ansible) SSH into the guest to install the runner — no manual step.
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
      username = "ubuntu" # default sudo user on the Ubuntu cloud image (ansible_user)
      keys     = [var.runner_ssh_public_key]
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
