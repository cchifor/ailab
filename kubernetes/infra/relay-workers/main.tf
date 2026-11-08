###############################################################################
# Dedicated Talos RELAY-WORKER node pool: Talos WORKERS that JOIN the existing `ai` cluster (VIP .40),
# NOT a new cluster. The join reuses the existing machine_secrets from infra/ remote state (talos.tf).
#
# Built for the Relay infrastructure-workers plan (Relay docs/plans/2026-11-08-infrastructure-workers)
# and its ailab feasibility spike (docs/runbooks/relay-worker-kubevirt-feasibility.md, K0). New work:
# it reuses no retired name, label, namespace or vmid of the test-env pool (ADR 0037) or AgentForge
# (ADR 0041).
#
# The VM block mirrors agent-nodes/main.tf (nocloud raw import, serial console, guest agent, no
# cloud-init user_account: Talos reads only the nocloud static IP and hostname). Plain schematic, the
# one agent-node-1/2 run: the Talos 1.14 KubeVirt guide lists no extension or kernel module, and KVM is
# in the Talos kernel. A gVisor-capable schematic for the pod.gvisor tier is a later, separate change.
###############################################################################

resource "proxmox_virtual_environment_vm" "relay_worker" {
  for_each = var.relay_worker_nodes

  name      = "talos-${each.key}"
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["talos", "k8s", "worker", "relay-worker"]

  agent {
    enabled = true # Talos ships qemu-guest-agent (an extension in the plain schematic)
  }
  stop_on_destroy = true

  cpu {
    cores = each.value.cores
    # type=host is REQUIRED: KubeVirt needs /dev/kvm inside this worker, which only exists when the
    # host's SVM passes through (and `kvm_amd nested=1` on the host: `just nested-virt-verify`).
    type = "host"
  }

  memory {
    # FIXED, no balloon. KubeVirt guests need a stable floor, Talos has no memory hotplug, and the
    # kubelet's allocatable is computed once from MemTotal: ballooning would shrink real memory under
    # a scheduler that still counts it.
    dedicated = each.value.memory_mib
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

  # Static IP on first boot via nocloud; the machine config (relay-worker.yaml.tftpl) pins it too.
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
    # initialization: cloud-init applies once; renumbering is the three-edit procedure in
    # docs/network-plan.md. disk[0].import_from: create-only, and its path carries talos_version,
    # which tracks the RUNNING Talos, so a version bump must not show as a disk diff.
    ignore_changes = [initialization, disk[0].import_from]
  }
}
