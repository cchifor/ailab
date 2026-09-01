###############################################################################
# Dedicated Talos ENV-POOL — Talos worker VM(s) for the leasable test-environment
# pool (plans/2026-09-01-test-env-pool-k8s-plan.md). Labelled ailab.io/env-pool,
# tainted dedicated=env. Talos WORKERS that JOIN the existing `ai` cluster
# (VIP .40) — NOT a new cluster; the join reuses the existing cluster
# machine_secrets via infra/ remote state (talos.tf), exactly like agent-nodes/
# (ADR 0019 Option B). Structurally a clone of agent-nodes/main.tf.
#
# This pool is deliberately SEPARATE from the AgentForge agent-nodes pool: test
# environments must never co-tenant AgentForge's sandbox nodes (user-set hard
# boundary). Spike/pool pods select `ailab.io/env-pool=true` and tolerate only
# `dedicated=env`, so they are unschedulable anywhere else by construction.
#
# Image: REUSES the agent-nodes P2 image (talos-v1.11.2-agent-nocloud-amd64.raw,
# schematic 0839748e… = qemu-guest-agent + iscsi-tools + util-linux-tools +
# kata-containers + gvisor), already staged on each PVE host's `local` datastore
# by scripts/stage-talos-image.sh during the agent-nodes P2 rollout. Same
# extensions are exactly what the env pool needs (kata + iscsi).
###############################################################################

resource "proxmox_virtual_environment_vm" "env" {
  for_each = var.env_nodes

  name      = "talos-${each.key}"
  vm_id     = each.value.vm_id
  node_name = each.value.node_name
  pool_id   = "ailab"
  tags      = ["talos", "k8s", "worker", "env-pool"]

  agent {
    enabled = true # Talos ships qemu-guest-agent (baked in the image)
  }
  stop_on_destroy = true

  cpu {
    cores = var.env_node_cores
    # type=host REQUIRED: Kata's microVMs need /dev/kvm inside this worker
    # (nested SVM passthrough; kvm_amd nested=1 is already enabled on the hosts
    # for the agent-nodes pool). Homogeneous CPUs; no live migration.
    type = "host"
  }

  memory {
    # NO balloon: Kata microVMs want a stable RAM floor (Talos has no hotplug).
    # SPIKE SIZING NOTE (2026-09-01): 16384 MiB, not the plan's 28 GiB `big`
    # target — live MemAvailable on all three hosts measured ~24.7-25.0 GiB, so
    # a 28 GiB fixed guest fits nowhere today. 16 GiB proves every spike-1
    # question and leaves ~9 GiB host margin; the big-flavor node is gated on
    # freeing host RAM (companion plan 2026-09-01-dynamic-dev-infra-plan.md).
    dedicated = var.env_node_memory_mib
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_datastore
    # The `-agent-` image is the P2 kata+gvisor image staged for agent-nodes;
    # verified present on ai-node2 (local:import/, 4.45 GB) before first apply.
    import_from = "${var.image_datastore}:import/talos-${var.talos_version}-agent-nocloud-amd64.raw"
    interface   = "scsi0"
    size        = var.env_node_disk_gb
    file_format = "raw"
    iothread    = true
    discard     = "on"
  }

  network_device {
    bridge = var.bridge
  }

  # Static IP on first boot via nocloud (Talos reads only the IP config).
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
