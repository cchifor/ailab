# ---- Proxmox connection (same tofu API token as the Talos infra/ + runners modules) ----
variable "pve_endpoint" { type = string }
variable "pve_api_token" {
  type      = string
  sensitive = true
}
variable "pve_insecure" {
  type    = bool
  default = true
}
variable "pve_ssh_username" {
  type    = string
  default = "root"
}
variable "pve_ssh_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519"
}

# ---- Network (mgmt LAN; matches the Talos + ai-lxc + runners modules) ----
variable "gateway" {
  type    = string
  default = "192.168.0.1"
}
variable "network_prefix" {
  type    = number
  default = 24
}
variable "bridge" {
  type    = string
  default = "vmbr0"
}
variable "dns_domain" {
  type    = string
  default = "lan"
}
variable "nameservers" {
  type    = list(string)
  default = ["1.1.1.1", "9.9.9.9"]
}

# ---- Storage ----
variable "vm_datastore" {
  description = "Datastore for the dev-worker VM disks (per-node local NVMe)."
  type        = string
  default     = "local-lvm"
}
variable "image_datastore" {
  description = <<-EOT
    Datastore that holds the downloaded Ubuntu cloud image. qnap-nfs is shared + mounted on all
    nodes (like the runners module), so the image downloads ONCE and every node's VM imports the
    same file. Its 'import' content type must be enabled (Datacenter -> Storage -> qnap-nfs ->
    Content) — VM disk import_from rejects an 'iso'-typed source.
  EOT
  type        = string
  default     = "qnap-nfs"
}
variable "image_download_node" {
  description = "Node that performs the one-time cloud-image download to the shared datastore."
  type        = string
  default     = "ai-node1"
}

# ---- Ubuntu 24.04 LTS (noble) cloud image ----
# NOTE: this is a qcow2 (.img), NOT xz-compressed, so bpg's download_file imports it directly
# (unlike the Talos factory image, which is xz and must be staged by a script — see infra/image.tf).
variable "ubuntu_cloud_image_url" {
  type    = string
  default = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}
variable "ubuntu_cloud_image_file" {
  # DISTINCT filename from the runners module's copy on the SAME shared qnap-nfs datastore, so a
  # `tofu destroy` on either module cannot delete the image out from under the other. Pin a dated
  # copy so a silent upstream re-publish of "current" doesn't change the base out from under a
  # rebuild. MUST end in .qcow2/.raw (not .img): PVE's "import" content type validates by extension.
  type    = string
  default = "noble-server-cloudimg-amd64-20260616-devworker.qcow2"
}

# ---- Dev-worker VM sizing ----
# Interactive Claude Code + Codex + Docker dev boxes. BALLOONED memory (floating < dedicated): idle
# workers sit near floating and only inflate to dedicated under load — important because the Strix
# Halo nodes carve up to ~64 GiB for GPU VRAM and already run a 32 GiB (hard) Talos CP VM, the
# ai-llm LXC, and a runner VM. See docs/runbooks/dev-workers.md for the per-node RAM budget.
variable "dev_worker_cores" {
  type    = number
  default = 8
}
variable "dev_worker_memory_mib" {
  description = "Max VM memory (MiB) — the ceiling the balloon can inflate to under load."
  type        = number
  # 16 GiB default ceiling. Genuinely reachable via ballooning: the rarely-used heavyweight LLMs on
  # node2/node3 are behind llama-swap (idle-unload), so once a node's model is unloaded pvestatd can
  # inflate a busy worker up to this ceiling. See docs/runbooks/dev-workers.md.
  # Since the testpool went live (2026-09-01) the heavy compose stacks (L/XL/Playwright) lease kata
  # envs via `tep` instead of running on the worker, so this ceiling is oversized: the busiest
  # worker's 10-day peak was 7.9 GiB (dw4, measured 2026-09-01, pre-pool load included). dev-worker-6
  # runs a 12 GiB POC via the per-worker memory_mib override in dev_worker_nodes; a fleet-wide
  # reduction follows once the POC has soaked. See docs/runbooks/dev-workers.md.
  default = 16384
}
variable "dev_worker_memory_floating_mib" {
  description = <<-EOT
    Default balloon FLOOR (MiB) — the shared baseline (mirrors the runners module); dw1/dw4
    override it to 12288 via memory_floating_mib in dev_worker_nodes (node1 mitigation, see the
    note there). Low by design (4 GiB): with the heavyweight
    LLMs now idle-unloaded via llama-swap, ballooning works, so the floor only has to cover an
    idle/light worker (~2-3 GiB) with margin and the balloon inflates on demand toward
    dev_worker_memory_mib. 4 GiB is also what lets a node hold its on-demand heavyweight (~59/71 GiB)
    AND 2 workers-at-floor at once (node3: 71 + cp3 28 + runner 10 + 2*4 = 117 < 125 GiB). During a
    rare heavyweight session the co-located workers are pinned near this floor (light use only).
    See docs/runbooks/dev-workers.md and docs/runbooks/ai-model-swap.md.
  EOT
  type        = number
  default     = 4096
}
variable "dev_worker_rootfs_gb" {
  description = "Root disk (scsi0) size in GiB; cloud-init growpart expands the root fs to fill it."
  type        = number
  default     = 40
}
variable "dev_worker_workspace_gb" {
  description = "Blank data disk (scsi1) size in GiB; Ansible partitions/mkfs/mounts it at /workspace."
  type        = number
  default     = 128
}

# ---- SSH public key seeded into the cloud-init `c4` user, so Ansible can reach the guest ----
# Non-secret. Default = the same control-node key the inventory uses (inventory/hosts.yml:
# ansible_ssh_private_key_file ~/.ssh/id_ed25519). Override via TF_VAR_dev_worker_ssh_public_key.
variable "dev_worker_ssh_public_key" {
  type    = string
  default = ""
}

# ---- TWO dev-worker VMs per physical host (was one) ----
# The base spec is shared: cores + dev_worker_memory_mib (ceiling) + dev_worker_memory_floating_mib
# (floor) are module-wide scalars; this map carries identity (node/vmid/ip/hostname) plus two
# OPTIONAL per-worker sizing overrides (memory_floating_mib and memory_mib, both documented below).
# Placement stays one-more-per-node
# (fault isolation): dw1/4 -> node1, dw2/5 -> node2, dw3/6 -> node3.
# IPs: consecutive .8-.13 (free static block, inside the .2-.50 reserve, below the router DHCP pool at
# .51 — no router change needed). vmids 42xx band (4201-4206) don't collide (Talos 4001-4003, runners
# 4101-4105, AI LXC 5001-5003, registry 5004). NOTE: cloud-init sets the IP at create and
# lifecycle.ignore_changes=[initialization] means editing `ip` here is DOCUMENTATION ONLY — the live IP
# was changed in-guest (netplan), see docs/runbooks/dev-workers.md. The 2nd worker per node fits because
# the rarely-used heavyweight LLMs on node2/node3 are idle-unloaded (llama-swap) — see ai-model-swap.md.
#
# memory_floating_mib is an OPTIONAL per-worker override of the uniform floor. It exists for exactly
# one reason and should shrink back to nothing once that reason is gone:
#
# The uniform 4 GiB floor above assumes ballooning WORKS — that a busy worker inflates toward the
# 16 GiB ceiling. On ai-node1 it does not. node1 carries 6 VMs plus the ai-llm-1 LXC (96 GiB limit)
# plus the registry LXC on 125 GiB of RAM and sits at ~88% used, which is above PVE's 80% auto-balloon
# threshold, so every balloonable guest there is pinned at its floor permanently. Measured 2026-08-12,
# /proc/vmstat across the fleet:
#
#   worker         node   floor    pswpout        pgmajfault
#   dev-worker-1   node1  12 GiB   ~40M / 13d     230.1M     <- panicked 2026-08-11 22:32
#   dev-worker-4   node1  12 GiB   3.1M / 10d       2.0M
#   dev-worker-2   node2  (float)  70k / 21d      150k
#   dev-worker-3   node3  16 GiB   0               11k
#
# So somebody had already raised node1's two workers to a 12 GiB floor BY HAND, live, to keep them off
# the swap cliff — and because `memory` was not in lifecycle.ignore_changes, the next plain
# `tofu apply` would have silently pulled both back down to 4 GiB on the one node that cannot afford
# it. Codifying the override makes that impossible and makes the mitigation reviewable, which
# ignore_changes would not: ignoring the block would stop tofu managing memory at all and hide the
# next divergence too.
#
# THIS IS A MITIGATION, NOT THE FIX. The fix is node1 capacity — cap ai-llm-1's 96 GiB limit or move a
# guest to node3 (a rebuild, not a live migration: per-node local-lvm and cpu: host). When that lands,
# delete these two overrides and let all six share the uniform floor again.
variable "dev_worker_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
    # null => use the uniform dev_worker_memory_floating_mib.
    memory_floating_mib = optional(number)
    # null => use the uniform dev_worker_memory_mib ceiling. Set on dev-worker-6 only: the 12 GiB
    # downsize POC, viable since heavy compose stacks moved to testpool leases (see the note on
    # dev_worker_memory_mib above). Hand-applied 2026-09-01 (`qm set 4206 --memory 12288` + reboot),
    # so the first apply after merge is a no-op for the VM.
    memory_mib = optional(number)
  }))
  default = {
    "dev-worker-1" = { node_name = "ai-node1", vm_id = 4201, ip = "192.168.0.8", hostname = "dev-worker-1", memory_floating_mib = 12288 }
    "dev-worker-2" = { node_name = "ai-node2", vm_id = 4202, ip = "192.168.0.9", hostname = "dev-worker-2" }
    "dev-worker-3" = { node_name = "ai-node3", vm_id = 4203, ip = "192.168.0.10", hostname = "dev-worker-3" }
    "dev-worker-4" = { node_name = "ai-node1", vm_id = 4204, ip = "192.168.0.11", hostname = "dev-worker-4", memory_floating_mib = 12288 }
    "dev-worker-5" = { node_name = "ai-node2", vm_id = 4205, ip = "192.168.0.12", hostname = "dev-worker-5" }
    "dev-worker-6" = { node_name = "ai-node3", vm_id = 4206, ip = "192.168.0.13", hostname = "dev-worker-6", memory_mib = 12288 }
  }
}
