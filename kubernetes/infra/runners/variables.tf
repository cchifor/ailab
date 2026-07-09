# ---- Proxmox connection (same tofu API token as the Talos infra/ + storage modules) ----
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

# ---- Network (mgmt LAN; matches the Talos + ai-lxc modules) ----
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
  description = "Datastore for the runner VM disks (per-node local NVMe)."
  type        = string
  default     = "local-lvm"
}
variable "image_datastore" {
  description = <<-EOT
    Datastore that holds the downloaded Ubuntu cloud image. qnap-nfs is shared + mounted on all
    nodes (like the ai-lxc template), so the image downloads ONCE and every node's VM imports the
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
  # Pin a dated copy so a silent upstream re-publish of "current" doesn't change the base out from
  # under a rebuild. Refresh deliberately (bump the date) when you want a newer image.
  # MUST end in .qcow2/.raw (not .img): the Ubuntu cloud image is qcow2 data, and PVE's "import"
  # content type validates by extension and rejects .img.
  type    = string
  default = "noble-server-cloudimg-amd64-20260616.qcow2"
}

# ---- Runner VM sizing ----
# Bigger than the cramped Hyper-V pool (4 vCPU / 1-8 GiB dyn): the platform CI runs heavy
# docker-compose e2e stacks + Buildx + k6. The runner *service* cgroup is capped at MemoryMax=10G
# (ansible role, to satisfy platform's runner-health canary); compose containers run under dockerd
# (outside that cgroup), so 24 GiB leaves ~14 GiB for them + the OS. 120 GiB disk + the role's
# reclaim/daemon.json GC fix the disk-fill problem the Hyper-V VMs hit. Hosts have ample headroom
# (~66 cores / ~119 GiB free each after Talos + AI LXC).
variable "runner_cores" {
  type    = number
  default = 8
}
variable "runner_memory_mib" {
  description = "Max VM memory (MiB) — the ceiling the balloon can inflate to under CI load."
  type        = number
  default     = 24576
}
variable "runner_memory_floating_mib" {
  description = <<-EOT
    Min VM memory (MiB) = the virtio-balloon floor. floating < dedicated enables ballooning: idle
    runners release RAM back toward this value, and the balloon deflates on demand up to
    runner_memory_mib under load. Independent of the runner service's systemd MemoryMax=10G (a cgroup
    cap, set in the ansible role) — the canary checks that, not the balloon. Set equal to
    runner_memory_mib to disable ballooning.

    Was 12 GiB (fixing cchifor/platform#620: at ~80% host RAM, pvestatd ballooned idle runners to
    1-2 GiB and CI jobs OOM-killed at exit 137). Now 10 GiB — the runner-service systemd cgroup cap
    (MemoryMax=10G): the observed CI-job working set peaks ~7 GiB (idle ~1-2 GiB, busy ~10% of the
    time), so 10 GiB still covers the peak while the balloon inflates to 24 GiB under load, and it
    frees ~2 GiB/runner (~10 GiB cluster-wide) now that the host RAM is no longer dominated by the
    pinned heavyweight LLMs (llama-swap idle-unload — docs/runbooks/ai-model-swap.md). DO NOT go below
    10 GiB: that would let pvestatd squeeze the guest under the runner's own cgroup cap and re-open
    #620. Validate under a two-heavy-jobs-on-one-host CI run before relying on the freed RAM.
  EOT
  type        = number
  default     = 10240 # 10 GiB balloon floor = runner-service MemoryMax cap; was 12288 — see cchifor/platform#620
}
variable "runner_rootfs_gb" {
  type    = number
  default = 120
}

# ---- SSH public key seeded into the cloud-init `ubuntu` user, so Ansible can reach the guest ----
# Non-secret. Default = the same control-node key the inventory uses (inventory/hosts.yml:
# ansible_ssh_private_key_file ~/.ssh/id_ed25519). Override via TF_VAR_runner_ssh_public_key.
variable "runner_ssh_public_key" {
  type    = string
  default = ""
}

# ---- Runner VMs — 5 active: node1 ×2, node2 ×2, node3 ×1 (node3's 2nd is deferred) ----
# See docs/decisions/0013-ci-self-hosted-runners.md (Update 2026-07-01) + the plan
# plans/2026-07-01-scale-ci-runners-to-6-plan.md. Every runner draws identical sizing from the shared
# runner_* vars above; only node_name/vm_id/ip/hostname differ (that identity is what proves "same
# config"). IPs .47/.48/.49 (runners 1-3) + .33/.34 (runners 4-5) sit inside the static-reserved block
# (.2-.50, outside the router DHCP pool that bit the AI LXCs at .51-.53). vmids 4101-4105 don't collide
# (Talos 4001-4003, dev-workers 4201-4203, AI LXC 5001-5003, registry 5004).
#
# ci-runner-6 (node3, .35 / vmid 4106) is RESERVED but DEFERRED: measured 2026-07-01, node3 has no room
# for a second runner while its qwen3.5-122b LLM is loaded (~7.8 GiB non-reclaimable RSS; the LXC runs
# swap=0). Uncomment to deploy after reducing node3's iGPU VRAM carve (or the Talos CP allocation) — the
# .19 IP + vmid 4106 stay reserved for it. See ADR 0013.
variable "runner_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  }))
  default = {
    # Consecutive IPs .14-.18 (.19 reserved for the deferred runner-6). cloud-init sets the IP at create
    # and lifecycle.ignore_changes=[initialization] makes editing `ip` here DOCUMENTATION ONLY — the live
    # IPs were changed in-guest via netplan (see docs/runbooks/ci-runners.md).
    "ci-runner-1" = { node_name = "ai-node1", vm_id = 4101, ip = "192.168.0.14", hostname = "ci-runner-1" }
    "ci-runner-2" = { node_name = "ai-node2", vm_id = 4102, ip = "192.168.0.15", hostname = "ci-runner-2" }
    "ci-runner-3" = { node_name = "ai-node3", vm_id = 4103, ip = "192.168.0.16", hostname = "ci-runner-3" }
    "ci-runner-4" = { node_name = "ai-node1", vm_id = 4104, ip = "192.168.0.17", hostname = "ci-runner-4" }
    "ci-runner-5" = { node_name = "ai-node2", vm_id = 4105, ip = "192.168.0.18", hostname = "ci-runner-5" }
    # DEFERRED — node3's second runner; uncomment after freeing node3 RAM (see header + ADR 0013):
    # "ci-runner-6" = { node_name = "ai-node3", vm_id = 4106, ip = "192.168.0.19", hostname = "ci-runner-6" }
  }
}
