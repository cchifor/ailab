###############################################################################
# Private OCI registry (Zot) — one unprivileged Proxmox LXC, OUT of the K8s cluster.
# Stable LAN endpoint registry.chifor.me:443. Anonymous pull (Talos nodes, over a
# publicly-trusted LE cert) + authenticated push (CI). Configured by the registry_zot
# Ansible role (just registry). Provider: bpg/proxmox ~> 0.109.
###############################################################################

# Debian 13 LXC template, downloaded to the shared QNAP NFS datastore. Distinct file_name from
# the ai-lxc module so a `tofu destroy` here can't delete that module's template.
resource "proxmox_virtual_environment_download_file" "debian_tmpl" {
  content_type = "vztmpl"
  datastore_id = var.template_datastore
  node_name    = var.template_download_node
  url          = var.lxc_template_url
  file_name    = var.lxc_template_file
  overwrite    = false
}

resource "proxmox_virtual_environment_container" "registry" {
  node_name     = var.registry_lxc.node_name
  vm_id         = var.registry_lxc.vm_id
  tags          = ["lxc", "registry", "zot", "platform"]
  unprivileged  = true # no host devices / bind mounts needed -> run unprivileged for safety
  start_on_boot = true

  features {
    nesting = true # systemd + the zot service inside the CT
  }

  memory {
    dedicated = var.lxc_memory_mib
    swap      = 0
  }

  cpu {
    cores = var.lxc_cores
  }

  # Root filesystem (OS only; image data is on the mp0 data disk below).
  disk {
    datastore_id = var.lxc_rootfs_datastore
    size         = var.lxc_rootfs_gb
  }

  # Allocated data volume for the OCI image store -> /var/lib/registry (mp0). NOT a bind mount,
  # so it grows online later: `pct resize ${var.registry_lxc.vm_id} mp0 +NG` or bump data_gb +
  # `tofu apply` (grow-only; needs free space on the datastore).
  mount_point {
    volume = var.data_datastore
    size   = "${var.data_gb}G"
    path   = "/var/lib/registry"
  }

  operating_system {
    template_file_id = proxmox_virtual_environment_download_file.debian_tmpl.id
    type             = "debian"
  }

  network_interface {
    name   = "eth0"
    bridge = var.bridge
  }

  initialization {
    hostname = var.registry_lxc.hostname
    ip_config {
      ipv4 {
        address = "${var.registry_lxc.ip}/${var.network_prefix}"
        gateway = var.gateway
      }
    }
    dns {
      domain  = var.dns_domain
      servers = var.nameservers
    }
    # Inject the Ansible SSH key into the CT root account (Proxmox writes it to
    # /root/.ssh/authorized_keys); the debian-standard template ships+starts sshd.
    user_account {
      keys = [trimspace(var.ssh_public_key)]
    }
  }

  # The mp0 data volume holds the ONLY copy of the pushed/cached images (no backup/replication yet —
  # see docs/runbooks/registry-cache.md). Guard against an accidental `tofu destroy` (or a
  # replacement-forcing change) wiping it. Remove this block deliberately to rebuild the LXC.
  lifecycle {
    prevent_destroy = true
  }
}

# The registry's NEW home on ai-node3 (the zot move, 2026-11-21; docs/runbooks/registry-cache.md "Moving the
# registry"). Created STOPPED and never started by tofu: the cutover copies CT 5004's rootfs and store into it
# (host-to-host rsync from read-only views, then a cold final pass), accepts the copy, stops 5004 and starts this
# one, which takes over 192.168.0.36. Same spec as `registry`; its own vmid and MAC.
#   - started/start_on_boot false: tofu must never start it while 5004 owns .36. The move's follow-up PR flips them
#     (and sets started/start_on_boot false on `registry`) only AFTER the cutover; until then no registry-lxc apply.
#   - net0 gets link_down=1 and the CT a 'mounted' lock right after creation (b-newct-n3.sh): bpg cannot express
#     link_down (its `enabled = false` DELETES the interface) and ignores the key, so neither shows as drift.
#   - prevent_destroy: this volume becomes the only live copy of the registry at the cutover.
resource "proxmox_virtual_environment_container" "registry_n3" {
  node_name     = var.registry_lxc_n3.node_name
  vm_id         = var.registry_lxc_n3.vm_id
  tags          = ["lxc", "registry", "zot", "platform"]
  unprivileged  = true # the default idmap: guest uid 999 (zot) = host 100999, as on CT 5004
  started       = false
  start_on_boot = false

  features {
    nesting = true
  }

  memory {
    dedicated = var.lxc_memory_mib
    swap      = 0
  }

  cpu {
    cores = var.lxc_cores
  }

  disk {
    datastore_id = var.lxc_rootfs_datastore
    size         = var.lxc_rootfs_gb
  }

  mount_point {
    volume = var.data_datastore
    size   = "${var.data_gb}G"
    path   = "/var/lib/registry"
  }

  operating_system {
    template_file_id = proxmox_virtual_environment_download_file.debian_tmpl.id
    type             = "debian"
  }

  network_interface {
    name   = "eth0"
    bridge = var.bridge
  }

  initialization {
    hostname = var.registry_lxc_n3.hostname
    ip_config {
      ipv4 {
        address = "${var.registry_lxc_n3.ip}/${var.network_prefix}"
        gateway = var.gateway
      }
    }
    dns {
      domain  = var.dns_domain
      servers = var.nameservers
    }
    user_account {
      keys = [trimspace(var.ssh_public_key)]
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
