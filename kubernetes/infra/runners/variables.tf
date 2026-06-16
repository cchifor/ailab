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
    same file. Its 'iso' content type must be enabled (Datacenter -> Storage -> qnap-nfs).
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
  type    = string
  default = "noble-server-cloudimg-amd64-20260616.img"
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
  type    = number
  default = 24576
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

# ---- One runner VM per physical host (fault isolation) ----
# IPs .47/.48/.49 are free + inside the static-reserved block (.2-.50, outside the router DHCP pool
# that bit the AI LXCs at .51-.53). vmids 4101-4103 don't collide (Talos 4001-4003, AI LXC 5001-5003).
variable "runner_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  }))
  default = {
    "gha-runner-1" = { node_name = "ai-node1", vm_id = 4101, ip = "192.168.0.47", hostname = "gha-runner-1" }
    "gha-runner-2" = { node_name = "ai-node2", vm_id = 4102, ip = "192.168.0.48", hostname = "gha-runner-2" }
    "gha-runner-3" = { node_name = "ai-node3", vm_id = 4103, ip = "192.168.0.49", hostname = "gha-runner-3" }
  }
}
