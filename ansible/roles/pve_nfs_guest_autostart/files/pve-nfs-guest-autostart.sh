#!/usr/bin/env bash
# Start this node's onboot containers that pve-guests could not start because a /mnt/pve/
# bind-mount source was not there yet. Deployed by the pve_nfs_guest_autostart role.
#
# WHY: the Proxmox hosts and the QNAP share one power feed. After a power cut they boot together
# and pve-guests runs before the NAS serves NFS, so a container with `mpN: /mnt/pve/qnap-nfs/...`
# fails lxc.hook.pre-start (exit 2: the bind source does not exist) and PVE never retries it. On
# 2026-10-30 both LLM containers (5002, 5003) stayed down for two hours that way.
#
# Once per boot, after pve-guests.service, for each container on THIS node that has `onboot: 1`,
# is STOPPED, and has a mount point sourced under /mnt/pve/: wait (one bounded budget shared by
# all containers) until every such source is a directory, then `pct start` it. The probe has its
# own timeout because a stat on a hard NFS mount that is still coming up can block.
#
# It never stops anything and never touches a running container, one without onboot, or one
# with no /mnt/pve/ mount point -- pve-guests already decided those. Exit 1 if any container was
# left stopped, so the unit shows failed.
set -u

CONF_DIR="${CONF_DIR:-/etc/pve/nodes/$(hostname)/lxc}"
PCT="${PCT:-pct}"
MOUNT_PREFIX="${MOUNT_PREFIX:-/mnt/pve/}"
WAIT_S="${WAIT_S:-1200}"   # total, across containers: the NAS came back ~6 min after the hosts
STEP_S="${STEP_S:-10}"
PROBE_S="${PROBE_S:-5}"

log() { echo "pve-nfs-guest-autostart: $*"; }
source_ready() { timeout "$PROBE_S" test -d "$1"; }

waited=0
rc=0
shopt -s nullglob
for conf in "$CONF_DIR"/*.conf; do
  ctid=$(basename "$conf" .conf)
  # The live section only: snapshot sections ([name]) repeat mount points that are not in effect.
  cfg=$(awk '/^\[/{exit} {print}' "$conf")
  printf '%s\n' "$cfg" | grep -qx 'onboot: 1' || continue
  mapfile -t srcs < <(printf '%s\n' "$cfg" | sed -nE "s#^mp[0-9]+: (${MOUNT_PREFIX}[^,]+).*#\1#p")
  [ "${#srcs[@]}" -gt 0 ] || continue
  state=$("$PCT" status "$ctid" 2>/dev/null | awk '{print $2}')
  [ "$state" = stopped ] || continue

  missing=""
  for src in "${srcs[@]}"; do
    until source_ready "$src"; do
      if [ "$waited" -ge "$WAIT_S" ]; then missing="$src"; break; fi
      sleep "$STEP_S"
      waited=$((waited + STEP_S))
    done
    [ -z "$missing" ] || break
  done
  if [ -n "$missing" ]; then
    log "CT $ctid: $missing still missing after ${WAIT_S}s; NOT starting it"
    rc=1
    continue
  fi
  if "$PCT" start "$ctid"; then
    log "CT $ctid: started (its /mnt/pve source was ready after ${waited}s)"
  else
    log "CT $ctid: pct start failed"
    rc=1
  fi
done
exit "$rc"
