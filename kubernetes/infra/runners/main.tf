###############################################################################
# Self-hosted GitHub Actions runner VMs — 5 active: 2 on node1/node2, 1 on node3 (6th reserved; see variables.tf).
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

  # The minimal Ubuntu cloud image does NOT ship qemu-guest-agent, so CREATE with the agent disabled:
  # with it enabled, bpg would block `apply` waiting for an agent that isn't installed yet, then time out.
  # The static IP comes from cloud-init (below), not the agent. The agent is enabled out-of-band right
  # after create by terraform_data.enable_guest_agent (guest-agent.tf) — via the PVE API, replacing the
  # old manual `qm set <vmid> --agent enabled=1 && qm reboot` runbook step (which stays as a fallback).
  # `agent` is in lifecycle.ignore_changes below so that out-of-band enable isn't reverted on re-apply.
  agent {
    enabled = false
  }
  stop_on_destroy = true

  # Never reboot a runner from `tofu apply` by default (bpg's own default is true). A disk cache change is
  # pending until a full stop/start, and an apply that changes every runner's disk would otherwise bounce all
  # of them at once, killing their jobs. This holds for EVERY later non-hotpluggable change too (CPU type,
  # machine, BIOS, disk attributes): after each apply, check `qm pending <vmid>` on every runner and drain +
  # restart the ones with pending config (docs/runbooks/ci-runners.md, "Disk cache mode"). For a planned
  # window where bouncing is fine, apply with -var runner_reboot_after_update=true.
  reboot_after_update = var.runner_reboot_after_update

  cpu {
    cores = var.runner_cores
    type  = "host" # homogeneous CPUs; no live migration (runners are rebuildable)
  }

  # Ballooning: VM floats between floating (idle floor, 12 GiB) and dedicated (load ceiling, 24 GiB),
  # so idle runners return RAM to the host. Independent of the runner service's systemd MemoryMax cap.
  # The floor is held at 12 GiB (not the bpg default 1 GiB) because host RAM pressure was ballooning
  # the guests down to 1-2 GiB and OOM-killing CI jobs — see cchifor/platform#620 + variables.tf.
  # Per-runner floor override (runner_nodes[*].memory_floating_mib) for runners on a host that sits at
  # pvestatd's ~80% line, where the floor IS the working size; see variables.tf.
  memory {
    dedicated = var.runner_memory_mib
    floating  = coalesce(each.value.memory_floating_mib, var.runner_memory_floating_mib)
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    import_from  = proxmox_virtual_environment_download_file.ubuntu_cloud.id
    interface    = "scsi0"
    size         = var.runner_rootfs_gb # cloud-init growpart expands the root fs to fill this
    iothread     = true
    discard      = "on"

    # Host cache mode (var.runner_disk_cache; owner decision 2026-11-19, ADR 0042). "unsafe": QEMU
    # acknowledges the guest's flushes without passing them down, so a runner's fsyncs (test databases,
    # docker, builds) stop being device-wide FLUSH commands on the NVMe it shares with its node's etcd
    # member. A HOST crash can lose a runner's recent writes. Accepted because runner disks hold rebuildable
    # data and no power loss is assumed. QEMU applies a cache change only on a full stop/start
    # (reboot_after_update above).
    cache = var.runner_disk_cache

    # Optional write cap (runner_nodes[*].disk_write_mibps / disk_write_iops; D1 2026-11-17, see
    # variables.tf). Rendered only when a cap is set, so uncapped runners keep a bare drive line.
    # bpg -> Proxmox: write -> mbps_wr, write_burstable -> mbps_wr_max, iops_write -> iops_wr,
    # iops_write_burstable -> iops_wr_max. Burstable = cap, so there is no burst above it (bpg does
    # not expose *_max_length; the burst pool is 1 s). Changes apply live, with no restart.
    dynamic "speed" {
      for_each = each.value.disk_write_mibps != null || each.value.disk_write_iops != null ? [1] : []
      content {
        write                = each.value.disk_write_mibps
        write_burstable      = each.value.disk_write_mibps
        iops_write           = each.value.disk_write_iops
        iops_write_burstable = each.value.disk_write_iops
      }
    }
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
    # `agent` is ignored too: VMs are created with agent=false (above, so apply doesn't hang), but the
    # guest agent is enabled out-of-band post-create (terraform_data.enable_guest_agent, guest-agent.tf).
    # Without this, a later apply reverts agent -> false and breaks qemu-guest-agent.service on every runner.
    # disk[0].import_from is CREATE-ONLY: it names the cloud image the root disk was imported from
    # at first boot. The provider does not read it back, so a VM ADOPTED via `tofu import` has it
    # empty in state while the config supplies it, producing a permanent in-place diff. Applying
    # that diff on a live runner would be a disk-level write to satisfy a field that only has
    # meaning at creation -- not a risk worth taking against a runner holding ~137 GiB of build
    # cache. Ignoring it is the same reasoning that already applies to `initialization`: both are
    # first-boot-only inputs. Added 2026-09-07 while adopting ci-runner-6..10 (4106-4110).
    ignore_changes = [initialization, agent, disk[0].import_from]
    # PVE requires balloon floor <= memory ceiling; fail at plan, naming the runner.
    precondition {
      condition     = coalesce(each.value.memory_floating_mib, var.runner_memory_floating_mib) <= var.runner_memory_mib
      error_message = "runner_nodes[\"${each.key}\"]: effective balloon floor exceeds runner_memory_mib."
    }
  }
}
