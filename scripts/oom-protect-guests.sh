#!/usr/bin/env bash
# Bias the host OOM killer per guest importance:
#   * Talos control-plane VMs  -> oom_score_adj -1000 (NEVER kill; an OOM-killed CP risks etcd quorum —
#     this happened 2026-07-08 when a heavyweight LLM + busy runners over-subscribed a node).
#   * GHA-runner + dev-worker VMs -> +750 (rebuildable; make them the PREFERRED victims so a memory
#     crunch sacrifices a re-registerable runner / re-creatable worker instead of a CP or a loaded model).
# The AI-LLM LXCs are left at the default score — protected only relative to the de-prioritised guests,
# so a loaded model tends to survive an OOM (a runner/worker dies first) without the absolute pin CPs get.
#
# Runs on EVERY Proxmox host: VMs are pinned per host, so /run/qemu-server/<vmid>.pid exists only on the
# host actually running that VM; absent ones are skipped. Idempotent — re-applied by
# oom-protect-guests.timer (OnUnitActiveSec) so the bias survives VM restarts + host reboots.
#
# Deploy (per host, via scripts/node-ssh.py): install to /usr/local/sbin, add the .service + .timer,
# `systemctl enable --now oom-protect-guests.timer`. See docs/runbooks/ai-model-swap.md. TODO: fold into
# the pve_base ansible role for full IaC.
set -u

set_adj() { # <adj> <vmid...>
  local adj="$1"; shift
  local vmid pid
  for vmid in "$@"; do
    pid="$(cat "/run/qemu-server/${vmid}.pid" 2>/dev/null)" || continue
    echo "$adj" > "/proc/${pid}/oom_score_adj" 2>/dev/null || true
  done
}

# Talos control planes (cp1/cp2/cp3) — protected.
set_adj -1000 4001 4002 4003
# GHA runners (4101-4105) + dev-workers (4201-4206) — rebuildable, preferred OOM victims.
set_adj 750 4101 4102 4103 4104 4105 4201 4202 4203 4204 4205 4206
