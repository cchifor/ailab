resource "proxmox_virtual_environment_vm" "cp" {
  for_each = var.control_planes

  name      = "talos-${each.key}"
  vm_id     = each.value.vm_id
  node_name = each.value.host_node
  pool_id   = "ailab"
  tags      = ["talos", "k8s", "controlplane"]

  agent {
    enabled = true
  }
  stop_on_destroy = true

  cpu {
    cores = each.value.cores
    type  = "host" # homogeneous CPUs; no live migration (Talos VMs are rebuildable)
  }

  memory {
    dedicated = each.value.memory # no hotplug (Talos limitation)
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    import_from  = "${var.image_datastore}:import/talos-${var.talos_version}-nocloud-amd64.raw"
    interface    = "scsi0"
    size         = each.value.disk_gb
    file_format  = "raw"
    iothread     = true
    discard      = "on"
  }

  network_device {
    bridge = var.bridge
  }

  # Static IP on first boot via nocloud cloud-init (Talos nocloud reads this);
  # the machine-config also pins it + adds the control-plane VIP.
  initialization {
    datastore_id = var.vm_datastore
    ip_config {
      ipv4 {
        address = "${each.value.ip}/${var.network_prefix}"
        gateway = var.gateway
      }
    }
  }

  operating_system {
    type = "l26"
  }

  serial_device {} # Talos prefers a serial console

  lifecycle {
    ignore_changes = [initialization] # avoid churn after first boot
  }
}
