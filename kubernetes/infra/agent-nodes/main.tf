###############################################################################
# Dedicated Talos AGENT node pool — one Talos worker VM per Proxmox host.
# AgentForge v2 compute pool (labelled ailab.io/agent-pool, tainted dedicated=agent). These are
# Talos WORKERS that JOIN the existing `ai` cluster (VIP .40) — NOT a new cluster. The join reuses
# the existing cluster machine_secrets (read from infra/ remote state in talos.tf); a fresh
# talos_machine_secrets here would fork the PKI and the worker would never join. See ADR 0019 + the
# spec's Option B. Structurally the VM block mirrors dev-workers/main.tf but is Talos-shaped like
# the CP vms.tf (nocloud raw import, serial console, guest agent on, NO cloud-init user_account —
# Talos reads only the nocloud static IP).
#
# P2 = the SAME VMs now boot the Kata/gVisor-enabled Talos image (image.tf schematic:
# siderolabs/kata-containers + siderolabs/gvisor) and enable the vhost_net/vhost_vsock kernel modules
# (machine-config/worker.yaml.tftpl). cpu.type=host (below) + `kvm_amd nested=1` on the Proxmox host
# (operator prereq, docs/runbooks/agent-nodes.md) give the worker /dev/kvm for Kata's QEMU microVM. No
# reshape from P1 — only the boot image + kernel modules changed.
###############################################################################

resource "proxmox_virtual_environment_vm" "agent" {
  for_each = var.agent_nodes

  name      = "talos-${each.key}"
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["talos", "k8s", "worker", "agent"]

  agent {
    enabled = true # Talos ships qemu-guest-agent (siderolabs/qemu-guest-agent extension, baked in the image)
  }
  stop_on_destroy = true

  cpu {
    cores = var.agent_node_cores
    # type=host REQUIRED so the host's nested SVM passes through to the guest — Kata's QEMU microVM
    # (P2) needs /dev/kvm inside this worker, which also requires `kvm_amd nested=1` on the Proxmox
    # host (operator prereq, docs/runbooks/agent-nodes.md). Homogeneous CPUs; no live migration.
    type = "host"
  }

  memory {
    dedicated = var.agent_node_memory_mib # no balloon: Kata microVMs want a stable RAM floor (Talos has no hotplug either)
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    # P2: the agent pool boots its OWN Kata/gVisor-enabled image (image.tf), staged under a distinct
    # "-agent-" basename so it coexists with the plain CP image on the same node's local:import.
    import_from = "${var.image_datastore}:import/${local.agent_image_file}"
    interface   = "scsi0"
    size        = var.agent_node_disk_gb
    file_format = "raw"
    iothread    = true
    discard     = "on"
  }

  network_device {
    bridge = var.bridge
  }

  # Static IP on first boot via nocloud cloud-init (Talos nocloud reads this); the machine-config
  # (worker.yaml.tftpl) also pins it. No user_account/dns — Talos ignores those.
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
