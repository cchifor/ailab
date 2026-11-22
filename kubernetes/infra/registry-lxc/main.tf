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

# The registry: CT 5005 on ai-node3 since the zot move (2026-11-21; docs/runbooks/registry-cache.md "Moving the
# registry"). The old CT 5004 on ai-node1 was archived to qnap-nfs and destroyed on 2026-11-22 (registry-cache.md,
# "The 5004 archive"); its resource is gone from this module.
#   - started: documentation only (ignore_changes below): bpg refreshes `started` from the CT's live status, so a
#     plan would otherwise stop it. start_on_boot is applied.
#   - The template: debian_tmpl lives on the SHARED qnap-nfs datastore (var.template_datastore; storage.cfg
#     `nfs: qnap-nfs`, content vztmpl, nodes ai-node1,ai-node2,ai-node3), so its id resolves on ai-node3 too,
#     although template_download_node downloaded it on another node.
#   - prevent_destroy: this volume is the only live copy of the registry.
resource "proxmox_virtual_environment_container" "registry_n3" {
  node_name     = var.registry_lxc_n3.node_name
  vm_id         = var.registry_lxc_n3.vm_id
  tags          = ["lxc", "registry", "zot", "platform"]
  unprivileged  = true # the default idmap: guest uid 999 (zot) = host 100999
  started       = true # the live registry since the 2026-11-21 cutover; documentation only (ignore_changes)
  start_on_boot = true # tofu applies this one

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

  # ignore_changes: bpg refreshes `started` from the CT's live status, so once the cutover has started this CT a
  # plan would otherwise stop it. A create still honours started = false.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [started]
  }
}
