###############################################################################
# Enable the QEMU guest agent out-of-band, AFTER create — codifies the previously-manual runbook step
# `qm set <vmid> --agent enabled=1 && qm reboot <vmid>`.
#
# Why out-of-band: the minimal Ubuntu cloud image ships no qemu-guest-agent, so the VM is created with
# `agent { enabled = false }` (main.tf) — enabling it there would make bpg block `apply` waiting for an
# agent that isn't installed yet, then time out. Here we flip the agent config on via the PVE API using
# the SAME api_token as the provider (NO SSH — no key/known_hosts surface, per the 4c decision), then
# cold-reboot so Proxmox attaches the virtio-serial channel the guest agent needs (a PVE `reboot`
# applies pending device changes; a soft ACPI reboot would not). The github_runner Ansible role then
# installs+starts qemu-guest-agent into that channel.
#
# on_failure = continue: this NEVER blocks the VM apply. If the API call fails (transient PVE error, or
# applied from a shell without /bin/sh + curl, e.g. Windows terraform), the VM is still created and the
# documented manual `qm` step remains the fallback; a re-apply retries. `agent` stays in the VM's
# lifecycle.ignore_changes so this enable is not reverted on later applies.
###############################################################################

resource "terraform_data" "enable_guest_agent" {
  for_each = var.runner_nodes

  # Re-run when the VM is (re)created — ties to the VM's real id, not just its vm_id number.
  triggers_replace = [proxmox_virtual_environment_vm.runner[each.key].id]

  provisioner "local-exec" {
    interpreter = ["/bin/sh", "-c"] # canonical apply path is WSL/Linux; on_failure=continue covers others
    on_failure  = continue
    environment = {
      PVE_ENDPOINT = var.pve_endpoint
      PVE_TOKEN    = var.pve_api_token # user@realm!tokenid=secret — same as the provider api_token
      PVE_NODE     = each.value.node_name
      VMID         = tostring(each.value.vm_id)
      PVE_K        = var.pve_insecure ? "-k" : ""
    }
    command = <<-EOT
      set -eu
      curl -fsS $PVE_K -X PUT "$PVE_ENDPOINT/api2/json/nodes/$PVE_NODE/qemu/$VMID/config" \
        -H "Authorization: PVEAPIToken=$PVE_TOKEN" --data-urlencode "agent=1" >/dev/null
      curl -fsS $PVE_K -X POST "$PVE_ENDPOINT/api2/json/nodes/$PVE_NODE/qemu/$VMID/status/reboot" \
        -H "Authorization: PVEAPIToken=$PVE_TOKEN" >/dev/null
    EOT
  }
}
