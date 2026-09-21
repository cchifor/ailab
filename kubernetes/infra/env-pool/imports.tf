# Adopt the live env node into this module's state. The spike (2026-09-01) applied this module from
# a scratchpad clone whose state was never handed over and is confirmed lost (plans/
# 2026-09-20-env-pool-root-cause-followup-plan.md, "What exploration established"), so the VM is
# imported rather than transferred. bpg import id = <node_name>/<vm_id>. Inert once the address is
# in state (cloudflare/imports.tf precedent) — keep it: a future `tofu state rm` of the VM would
# otherwise re-create 4401 on the next apply instead of re-adopting it.
import {
  to = proxmox_virtual_environment_vm.env["env-node-1"]
  id = "ai-node2/4401"
}
