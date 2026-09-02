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
variable "vm_datastore" {
  type    = string
  default = "local-lvm"
}
variable "image_datastore" {
  description = "Shared datastore for the cloud image (mounted on all nodes)."
  type        = string
  default     = "qnap-nfs"
}
variable "ubuntu_cloud_image_url" {
  type    = string
  default = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}
# DISTINCT filename from the dev-workers/runners copies, so destroying either module can
# never delete this one's image. MUST end in .qcow2/.raw (not .img): PVE's "import"
# content type validates by extension (same note as the dev-workers module).
variable "ubuntu_cloud_image_file" {
  type    = string
  default = "noble-server-cloudimg-amd64-20260902-reviewer.qcow2"
}
variable "reviewer_ssh_public_key" {
  type    = string
  default = ""
}

# Two dedicated PR-reviewer VMs (plan: agentforge plans/2026-09-02-ai-pr-review-plan.md;
# operator-directed isolation 2026-09-02: review must never contend with feature work on
# the dev workers). BOTH on ai-node3 by operator decision — node2 is saturated (the dw5
# swap-death incident) and node1 is the worst-committed host; reviewer downtime is
# tolerable because the reviewbot reconciler heals missed events after an outage.
# Small FIXED memory (no balloon): the workload is a python service + one headless LLM
# CLI run at a time. IPs .24/.25 verified free 2026-09-02 (the .20-.22 block is the cloud
# GPU cluster; .14-.19/.23 answered ping). vmids: 45xx band — verified free against
# /cluster/resources, NOT against prose. The module originally claimed 4301/4302 from a
# stale enumeration; those belonged to talos-agent-node-1/2, and the apply DESTROYED both
# production agent nodes (2026-09-02 incident). Always check the live cluster for vmid
# claims: pvesh get /cluster/resources --type vm.
variable "reviewer_nodes" {
  type = map(object({
    node_name = string
    vm_id     = number
    ip        = string
    hostname  = string
  }))
  default = {
    "reviewer-1" = { node_name = "ai-node3", vm_id = 4501, ip = "192.168.0.24", hostname = "reviewer-1" }
    "reviewer-2" = { node_name = "ai-node3", vm_id = 4502, ip = "192.168.0.25", hostname = "reviewer-2" }
  }
}
variable "reviewer_cores" {
  type    = number
  default = 2
}
variable "reviewer_memory_mib" {
  type    = number
  default = 4096
}
variable "reviewer_rootfs_gb" {
  type    = number
  default = 25
}
