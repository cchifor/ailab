#!/usr/bin/env bash
# Start this node's onboot containers that pve-guests could not start because a /mnt/pve/
# bind-mount source was not there yet. Deployed by the pve_nfs_guest_autostart role.
#
# WHY: the Proxmox hosts and the QNAP share one power feed. After a power cut they boot together
# and pve-guests runs before the NAS serves NFS, so a container with `mpN: /mnt/pve/qnap-nfs/...`
# fails lxc.hook.pre-start (exit 2: the bind source does not exist) and PVE never retries it. On
# 2026-10-30 both LLM containers (5002, 5003) stayed down for two hours that way.
#
# Once per boot, after pve-guests.service:
#   1. The candidates are fixed ONCE, up front: containers on THIS node that have `onboot: 1`, a
#      mount point sourced under /mnt/pve/, are not HA-managed, and are STOPPED right after
#      startall -- i.e. failed boot starts. One that is running now, or that an operator stops
#      later, is never touched.
#   2. For each, wait until its sources are directories, against ONE elapsed-time budget shared
#      by all of them, then re-check onboot / HA / stopped immediately before `pct start`: the wait
#      can take minutes and the operator's intent may have changed meanwhile.
#
# A stat on a hard NFS mount that is still coming up can block past SIGTERM, and in D state past
# SIGKILL too, so each probe runs in the background and is ABANDONED, not waited for, when it
# overruns. It never stops anything. Exit 1 if a candidate was left stopped or a `pct status`
# failed, so the unit shows failed.
set -u

CONF_DIR="${CONF_DIR:-/etc/pve/nodes/$(hostname)/lxc}"
HA_RESOURCES="${HA_RESOURCES:-/etc/pve/ha/resources.cfg}"
PCT="${PCT:-pct}"
PROBE_CMD="${PROBE_CMD:-test -d}"   # test seam; word-split on purpose
MOUNT_PREFIX="${MOUNT_PREFIX:-/mnt/pve/}"
WAIT_S="${WAIT_S:-1200}"   # elapsed, across containers: the NAS came back ~6 min after the hosts
STEP_S="${STEP_S:-10}"
PROBE_S="${PROBE_S:-5}"

log() { echo "pve-nfs-guest-autostart: $*"; }

# The live section only: snapshot sections ([name]) repeat mount points that are not in effect.
live_config() { awk '/^\[/{exit} {print}' "$1"; }

onboot() { live_config "$1" | grep -qx 'onboot: 1'; }

ha_managed() { [ -r "$HA_RESOURCES" ] && grep -qE "^ct: *$1([[:space:]]|$)" "$HA_RESOURCES"; }

# Mount point sources under MOUNT_PREFIX (compared literally), from either spelling PVE accepts:
#   mpN: <source>,mp=/x     and     mpN: mp=/x,volume=<source>
nfs_sources() {
  local line src item
  local -a items
  while IFS= read -r line; do
    [[ "$line" =~ ^mp[0-9]+:[[:space:]]*(.*)$ ]] || continue
    src=""
    IFS=',' read -ra items <<< "${BASH_REMATCH[1]}"
    for item in "${items[@]}"; do
      case "$item" in
        volume=*) src=${item#volume=} ;;
        *=*) ;;
        *) [ -n "$src" ] || src=$item ;;
      esac
    done
    [[ "$src" == "$MOUNT_PREFIX"* ]] && printf '%s\n' "$src"
  done
  return 0
}

# Prints the container's state; fails (and says why, on stderr: callers capture stdout) if
# `pct status` itself failed.
ct_state() {
  local out
  if ! out=$("$PCT" status "$1" 2>&1); then
    log "CT $1: pct status failed: $out" >&2
    return 1
  fi
  awk '{print $2}' <<< "$out"
}

source_ready() {
  local pid i
  # stdio detached, so an abandoned probe cannot pin the unit's journal pipe either.
  ( $PROBE_CMD "$1" ) </dev/null >/dev/null 2>&1 &
  pid=$!
  for ((i = 0; i < PROBE_S * 10; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      return
    fi
    sleep 0.1
  done
  kill -9 "$pid" 2>/dev/null
  return 1   # abandoned: never `wait` on a child that may be unkillable
}

start=$SECONDS
deadline=$((start + WAIT_S))
rc=0
candidates=()
shopt -s nullglob
for conf in "$CONF_DIR"/*.conf; do
  ctid=$(basename "$conf" .conf)
  onboot "$conf" || continue
  [ -n "$(live_config "$conf" | nfs_sources)" ] || continue
  if ha_managed "$ctid"; then
    log "CT $ctid: HA-managed, left to ha-manager"
    continue
  fi
  state=$(ct_state "$ctid") || { rc=1; continue; }
  [ "$state" = stopped ] && candidates+=("$ctid")
done

for ctid in "${candidates[@]}"; do
  conf="$CONF_DIR/$ctid.conf"
  mapfile -t srcs < <( [ -f "$conf" ] && live_config "$conf" | nfs_sources )
  missing=""
  for src in "${srcs[@]}"; do
    until source_ready "$src"; do
      if [ "$SECONDS" -ge "$deadline" ]; then missing="$src"; break; fi
      sleep "$STEP_S"
    done
    [ -z "$missing" ] || break
  done
  if [ -n "$missing" ]; then
    log "CT $ctid: $missing still missing after ${WAIT_S}s; NOT starting it"
    rc=1
    continue
  fi

  if ! { [ -f "$conf" ] && onboot "$conf"; }; then
    log "CT $ctid: no longer onboot; left stopped"
    continue
  fi
  if ha_managed "$ctid"; then
    log "CT $ctid: now HA-managed, left to ha-manager"
    continue
  fi
  state=$(ct_state "$ctid") || { rc=1; continue; }
  if [ "$state" != stopped ]; then
    log "CT $ctid: now $state; nothing to do"
    continue
  fi
  if "$PCT" start "$ctid"; then
    log "CT $ctid: started (its /mnt/pve source was ready after $((SECONDS - start))s)"
  else
    log "CT $ctid: pct start failed"
    rc=1
  fi
done
exit "$rc"
