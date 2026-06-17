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
  default     = 24576 # 24 GiB; drop to 16384 if a large GPU VRAM carve makes a node tight
}
variable "dev_worker_memory_floating_mib" {
  description = <<-EOT
    Min VM memory (MiB) = the virtio-balloon floor. floating < dedicated enables ballooning: idle
    dev workers release RAM back toward this value and the balloon deflates on demand up to
    dev_worker_memory_mib under load. Set equal to dev_worker_memory_mib to disable ballooning.
  EOT
  type        = number
  default     = 6144 # 6 GiB idle floor; reclaimed toward this under host pressure
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

# ---- One dev-worker VM per physical host (fault isolation) ----
# IPs .50/.51/.52: .50 is the last free address in the static-reserved block (.2-.50); .51/.52 are
# reclaimed by shrinking the router DHCP pool to start at .53 FIRST (see docs/network-plan.md +
# docs/runbooks/dev-workers.md). vmids 4201-4203 don't collide (Talos 4001-4003, runners 4101-4103,
# AI LXC 5001-5003).
variable "dev_worker_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  }))
  default = {
    "dev-worker-1" = { node_name = "ai-node1", vm_id = 4201, ip = "192.168.0.50", hostname = "dev-worker-1" }
    "dev-worker-2" = { node_name = "ai-node2", vm_id = 4202, ip = "192.168.0.51", hostname = "dev-worker-2" }
    "dev-worker-3" = { node_name = "ai-node3", vm_id = 4203, ip = "192.168.0.52", hostname = "dev-worker-3" }
  }
}
