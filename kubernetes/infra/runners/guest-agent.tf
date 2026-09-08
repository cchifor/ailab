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
# Failure handling: the provisioner is allowed to FAIL (no on_failure=continue). On failure — transient
# PVE error, or applied from a shell without /bin/sh+curl (e.g. Windows terraform) — Terraform taints
# this terraform_data resource, so the NEXT `apply` re-runs it (correct retry). We deliberately do NOT
# swallow the error: on_failure=continue would mark the step done-in-state and NEVER retry, silently
# leaving agent=false while Ansible later fails on the missing virtio-serial channel. The VMs themselves
# are already created (separate resources); only this step retries. The documented manual
# `qm set <vmid> --agent enabled=1 && qm reboot` remains the fallback, and `agent` stays in the VM's
# lifecycle.ignore_changes so the enable isn't reverted. Canonical apply path is WSL/Linux.
###############################################################################

resource "terraform_data" "enable_guest_agent" {
  for_each = var.runner_nodes

  # Re-run when the VM is (re)created — ties to the VM's real id, not just its vm_id number.
  triggers_replace = [proxmox_virtual_environment_vm.runner[each.key].id]

  provisioner "local-exec" {
    # `bash`, resolved via PATH -- NOT a hardcoded "/bin/sh". 2026-09-07: an apply from the estate's
    # MANDATED platform failed here with
    #     exec: "/bin/sh": executable file not found in %PATH%
    # because tofu.exe is a native Windows binary and cannot exec a POSIX absolute path. CLAUDE.md
    # requires running tofu on Windows (the providers are windows_amd64 and WSL has no internet), so
    # "canonical apply path is WSL/Linux" was never reconcilable with how this estate actually
    # applies. `bash` resolves on both: Git Bash ships C:\Program Files\Git\usr\bin\bash.exe on
    # Windows, and bash is standard on Linux. The script below is portable POSIX either way.
    interpreter = ["bash", "-c"]
    environment = {
      PVE_ENDPOINT = var.pve_endpoint
      PVE_TOKEN    = var.pve_api_token # user@realm!tokenid=secret — same as the provider api_token
      PVE_NODE     = each.value.node_name
      VMID         = tostring(each.value.vm_id)
      PVE_K        = var.pve_insecure ? "-k" : ""
    }
    command = <<-EOT
      set -eu
      # Strip any trailing slash: pve_endpoint may end in "/" (the bpg provider normalizes it, but a
      # raw curl would build "…:8006//api2/…" → PVE returns HTTP 500 on the double slash).
      EP="$${PVE_ENDPOINT%/}"
      # Read the CURRENT agent setting first, so this provisioner is idempotent.
      # 2026-09-07: adopting the five out-of-band runners (4106-4110) into state makes this resource
      # create/replace for VMs that are ALREADY running with agent=1. The PUT below is a no-op for
      # those, but the reboot was NOT: it would have bounced five live CI runners to re-apply a
      # setting they already had. A provisioner whose only job is "enable the agent" has no business
      # rebooting a VM on which it changed nothing.
      # Read the whole config document, then extract the agent value WITHOUT splitting on commas.
      # PVE's `agent` is an option STRING, not a bool: it can be "1", or "enabled=1", or a compound
      # like "fstrim_cloned_disks=0,enabled=1". An earlier version of this guard piped through
      # `tr ',' '\n'` and matched any "1", which is wrong in BOTH directions -- the compound form
      # loses the enabled= token (so an already-enabled agent got rebooted, the exact thing this
      # guard exists to prevent), and "enabled=0,fstrim_cloned_disks=1" would have matched and
      # skipped. Capture the quoted value whole, or the bare numeric form.
      CFG="$(curl -fsS $PVE_K "$EP/api2/json/nodes/$PVE_NODE/qemu/$VMID/config" \
        -H "Authorization: PVEAPIToken=$PVE_TOKEN")" || {
        echo "FATAL: could not read config for $PVE_NODE/$VMID -- refusing to touch it" >&2
        exit 1
      }
      # An unreadable/!JSON response must ABORT, never fall through to the reboot branch: a transient
      # API error is not evidence that the agent is disabled.
      case "$CFG" in
        *'"data"'*) : ;;
        *) echo "FATAL: unexpected config response for $PVE_NODE/$VMID" >&2; exit 1 ;;
      esac
      CUR="$(printf '%s' "$CFG" | sed -n 's/.*"agent"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
      if [ -z "$CUR" ]; then
        CUR="$(printf '%s' "$CFG" | sed -n 's/.*"agent"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
      fi
      curl -fsS $PVE_K -X PUT "$EP/api2/json/nodes/$PVE_NODE/qemu/$VMID/config" \
        -H "Authorization: PVEAPIToken=$PVE_TOKEN" --data-urlencode "agent=1" >/dev/null
      # Only the FIRST enable needs the cold reboot: PVE attaches the virtio-serial channel the guest
      # agent needs on a full stop/start, which a soft ACPI reboot would not do. If the agent was
      # already on, that channel is already attached and a reboot buys nothing.
      # Interpret the option string independently of ordering. PVE treats a leading bare "1" as
      # enabled (agent: 1 / agent: 1,fstrim_cloned_disks=1), and otherwise the explicit enabled= key
      # decides. An ABSENT agent field leaves CUR empty -> not enabled -> enable + reboot, which is
      # the correct behaviour for a freshly created VM.
      case "$CUR" in
        *enabled=1*) AGENT_ON=yes ;;
        *enabled=0*) AGENT_ON=no ;;
        1|1,*)       AGENT_ON=yes ;;
        *)           AGENT_ON=no ;;
      esac
      if [ "$AGENT_ON" = yes ]; then
        echo "guest agent already enabled on $PVE_NODE/$VMID (agent=$CUR) - skipping reboot"
      else
        curl -fsS $PVE_K -X POST "$EP/api2/json/nodes/$PVE_NODE/qemu/$VMID/status/reboot" \
          -H "Authorization: PVEAPIToken=$PVE_TOKEN" >/dev/null
      fi
    EOT
  }
}
