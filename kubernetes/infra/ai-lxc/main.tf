###############################################################################
# AI LLM appliance — one privileged GPU LXC per Proxmox node.
# Runs llama.cpp (Vulkan/RADV) on the Strix Halo iGPU (gfx1151), serving an
# OpenAI-compatible API on :8080. Models come from the shared QNAP NFS bind mount.
# Provider: bpg/proxmox ~> 0.109 (native device_passthrough + host-path mount_point).
###############################################################################

# Debian 13 LXC template, downloaded ONCE to the shared QNAP NFS datastore (which is
# already content-typed for vztmpl and mounted on all nodes), so every node's container
# can reference the same file. Do NOT set decompression_algorithm — PVE consumes the
# .tar.zst directly.
resource "proxmox_virtual_environment_download_file" "debian_tmpl" {
  content_type = "vztmpl"
  datastore_id = var.template_datastore
  node_name    = var.template_download_node
  url          = var.lxc_template_url
  file_name    = var.lxc_template_file
  overwrite    = false
}

resource "proxmox_virtual_environment_container" "ai_llm" {
  for_each  = var.ai_llm_nodes
  node_name = each.value.node_name
  vm_id     = each.value.vm_id
  tags      = ["lxc", "gpu", "llama-cpp", "amd", "ai-llm"]

  unprivileged  = false # privileged: clean root access to the NFS bind mount + host GPU devices
  start_on_boot = true

  features {
    nesting = true # systemd-in-CT
  }

  # Host OOM fence. See variables.tf (lxc_memory_mib) for the carve-dependent sizing rationale.
  memory {
    dedicated = var.lxc_memory_mib
    swap      = 0 # OOM-kill inside the CT rather than thrash the host
  }

  cpu {
    cores = var.lxc_cores
  }

  disk {
    datastore_id = var.lxc_rootfs_datastore
    size         = var.lxc_rootfs_gb
  }

  operating_system {
    template_file_id = proxmox_virtual_environment_download_file.debian_tmpl.id
    type             = "debian"
  }

  # GPU passthrough — PVE auto-resolves major:minor and generates the cgroup2 device-allow
  # rule + bind mount. renderD128 (226:128) is the Vulkan/RADV render node; kfd is the
  # AMDKFD compute node (kept for optional ROCm tooling, harmless for Vulkan-only).
  device_passthrough {
    path = "/dev/dri/renderD128"
    gid  = var.render_gid
    mode = "0660"
  }
  device_passthrough {
    path = "/dev/kfd"
    gid  = var.render_gid
    mode = "0660"
  }

  # Shared model store: host NFS path -> /models in the CT (bind mount; never set `size`).
  mount_point {
    volume    = var.models_host_path
    path      = "/models"
    read_only = false
    backup    = false # don't vzdump an NFS-backed share
  }

  # Optional local-NVMe model cache -> /models-local (managed local-lvm volume, sized per node). Present
  # only where ai_llm_nodes[].model_cache_gb > 0 (node2/node3): staging the idle-unloaded heavyweight
  # here makes llama-swap cold loads ~7-15x faster than reading the GGUF over NFS. node1 gets none, so
  # its LXC is unchanged. backup=false: models are re-stageable, never vzdump ~60-76 GiB of weights.
  dynamic "mount_point" {
    for_each = try(each.value.model_cache_gb, 0) > 0 ? [each.value.model_cache_gb] : []
    content {
      volume = var.model_cache_datastore
      size   = "${mount_point.value}G"
      path   = "/models-local"
      backup = false
    }
  }

  network_interface {
    name   = "eth0"
    bridge = var.bridge
  }

  initialization {
    hostname = each.value.hostname
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
  }
}
