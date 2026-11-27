#!/usr/bin/env bash
# Start this node's onboot guests that pve-guests could not start because their NFS storage was not
# there yet. Deployed by the pve_nfs_guest_autostart role.
#
# WHY: Proxmox starts onboot guests ONCE per boot and never retries. When the QNAP serves NFS after
# pve-guests has run, such a guest stays down until someone starts it by hand:
#   - containers with `mpN: /mnt/pve/qnap-nfs/...`: lxc.hook.pre-start fails (the bind source does not
#     exist). 2026-10-30: both LLM containers (5002, 5003) stayed down for two hours.
#   - VMs with a disk on an NFS storage: `qm start` fails "storage 'qnap-nfs' is not online". The
#     Talos control planes (4001-4003) live there since 2026-11-20 (ADR 0043). pve-nfs-disk-gate holds
#     pve-guests for at most 10 min; on 2026-11-27 the NAS was powered on after the hosts and all three
#     CPs stayed stopped.
#
# Once per boot, after pve-guests.service:
#   1. The candidates are fixed ONCE, up front: guests on THIS node that have `onboot: 1`, are not
#      templates or HA-managed, depend on NFS (a container mount point sourced under /mnt/pve/, or a VM
#      disk on a storage of type nfs in storage.cfg), and are STOPPED right after startall -- i.e.
#      failed boot starts. One that is running now, or that an operator stops later, is never touched.
#   2. For each, wait until its sources are directories (a container's bind sources; for a VM, its
#      images/<vmid> directory, which exists only while the share is mounted), against ONE elapsed-time
#      budget shared by all of them, then re-check onboot / HA / stopped immediately before the start:
#      the wait can take long and the operator's intent may have changed meanwhile.
#
# A stat on a hard NFS mount that is still coming up can block past SIGTERM, and in D state past
# SIGKILL too, so each probe runs in the background and is ABANDONED, not waited for, when it
# overruns. It never stops anything. Exit 1 if a candidate was left stopped or a status query failed,
# so the unit shows failed.
set -u

NODE="${NODE:-$(hostname)}"
CONF_DIR="${CONF_DIR:-/etc/pve/nodes/$NODE/lxc}"
VM_CONF_DIR="${VM_CONF_DIR:-/etc/pve/nodes/$NODE/qemu-server}"
STORAGE_CFG="${STORAGE_CFG:-/etc/pve/storage.cfg}"
HA_RESOURCES="${HA_RESOURCES:-/etc/pve/ha/resources.cfg}"
PCT="${PCT:-pct}"
QM="${QM:-qm}"
PROBE_CMD="${PROBE_CMD:-test -d}"   # test seam; word-split on purpose
MOUNT_PREFIX="${MOUNT_PREFIX:-/mnt/pve/}"
# Elapsed, across guests, counted from the end of pve-guests. The NAS came back ~6 min after the hosts
# on 2026-10-30, ~10 min on 2026-11-27 (powered on by hand); an hour leaves room for a slow power-on.
WAIT_S="${WAIT_S:-3600}"
STEP_S="${STEP_S:-10}"
PROBE_S="${PROBE_S:-5}"

log() { echo "pve-nfs-guest-autostart: $*"; }

# The live section only: snapshot sections ([name]) repeat mount points that are not in effect.
live_config() { awk '/^\[/{exit} {print}' "$1"; }

onboot() { live_config "$1" | grep -qx 'onboot: 1'; }

# $1 = ct|vm, $2 = id
ha_managed() { [ -r "$HA_RESOURCES" ] && grep -qE "^$1: *$2([[:space:]]|$)" "$HA_RESOURCES"; }

conf_of() { if [ "$1" = ct ]; then echo "$CONF_DIR/$2.conf"; else echo "$VM_CONF_DIR/$2.conf"; fi; }

label() { if [ "$1" = ct ]; then echo "CT $2"; else echo "VM $2"; fi; }

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

# "<type> <path>" of storage $1 in storage.cfg ("<type>: <id>" headers, indented "<key> <value>" lines).
storage_type_path() {
  [ -r "$STORAGE_CFG" ] || return 0
  awk -v id="$1" '
    /^[^[:space:]#]/ { cur = ($2 == id); if (cur) { typ = $1; sub(/:$/, "", typ) } next }
    cur && $1 == "path" { path = $2 }
    END { if (typ != "") print typ, path }
  ' "$STORAGE_CFG"
}

# For each disk of a VM (ideN, sataN, scsiN, virtioN, efidiskN, tpmstateN; the item without a key, or
# file=) on a storage of type nfs: that storage's images/<owner vmid> directory. Once per directory.
DRIVE_RE='^(ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+:[[:space:]]*(.*)$'
vm_nfs_sources() {
  local line volid item sid rest stype spath dir seen=" "
  local -a items
  while IFS= read -r line; do
    [[ "$line" =~ $DRIVE_RE ]] || continue
    volid=""
    IFS=',' read -ra items <<< "${BASH_REMATCH[2]}"
    for item in "${items[@]}"; do
      case "$item" in
        file=*) volid=${item#file=} ;;
        *=*) ;;
        *) [ -n "$volid" ] || volid=$item ;;
      esac
    done
    [[ "$volid" == *:* ]] || continue
    sid=${volid%%:*}
    rest=${volid#*:}
    [[ "$rest" =~ ^([0-9]+)/ ]] || continue
    read -r stype spath <<< "$(storage_type_path "$sid")"
    [ "${stype:-}" = nfs ] || continue
    dir="${spath:-$MOUNT_PREFIX$sid}/images/${BASH_REMATCH[1]}"
    [[ "$seen" == *" $dir "* ]] && continue
    seen="$seen$dir "
    printf '%s\n' "$dir"
  done
  return 0
}

sources_of() {  # $1 = ct|vm, $2 = id
  local conf
  conf=$(conf_of "$1" "$2")
  [ -f "$conf" ] || return 0
  if [ "$1" = ct ]; then live_config "$conf" | nfs_sources; else live_config "$conf" | vm_nfs_sources; fi
}

# Prints the guest's state; fails (and says why, on stderr: callers capture stdout) if the status
# query itself failed.
guest_state() {  # $1 = ct|vm, $2 = id
  local out tool=$PCT name=pct
  [ "$1" = vm ] && { tool=$QM; name=qm; }
  if ! out=$("$tool" status "$2" 2>&1); then
    log "$(label "$1" "$2"): $name status failed: $out" >&2
    return 1
  fi
  awk '{print $2}' <<< "$out"
}

guest_start() {  # $1 = ct|vm, $2 = id
  if [ "$1" = ct ]; then "$PCT" start "$2"; else "$QM" start "$2"; fi
}

source_ready() {  # $1 = source, $2 = seconds this probe may take
  local pid i
  # stdio detached, so an abandoned probe cannot pin the unit's journal pipe either.
  ( $PROBE_CMD "$1" ) </dev/null >/dev/null 2>&1 &
  pid=$!
  for ((i = 0; i < $2 * 10; i++)); do
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
for kind in ct vm; do
  if [ "$kind" = ct ]; then dir=$CONF_DIR; else dir=$VM_CONF_DIR; fi
  for conf in "$dir"/*.conf; do
    id=$(basename "$conf" .conf)
    onboot "$conf" || continue
    live_config "$conf" | grep -qx 'template: 1' && continue
    [ -n "$(sources_of "$kind" "$id")" ] || continue
    if ha_managed "$kind" "$id"; then
      log "$(label "$kind" "$id"): HA-managed, left to ha-manager"
      continue
    fi
    state=$(guest_state "$kind" "$id") || { rc=1; continue; }
    [ "$state" = stopped ] && candidates+=("$kind:$id")
  done
done

for cand in "${candidates[@]}"; do
  kind=${cand%%:*}
  id=${cand#*:}
  what=$(label "$kind" "$id")
  conf=$(conf_of "$kind" "$id")
  mapfile -t srcs < <(sources_of "$kind" "$id")
  missing=""
  for src in "${srcs[@]}"; do
    # The deadline is checked BEFORE each probe and caps both the probe and the sleep, so the
    # budget is a real bound: once it has passed, a remaining candidate costs nothing more
    # instead of one more possibly-hung probe each.
    while :; do
      left=$((deadline - SECONDS))
      if [ "$left" -le 0 ]; then missing="$src"; break; fi
      source_ready "$src" "$((left < PROBE_S ? left : PROBE_S))" && break
      left=$((deadline - SECONDS))
      [ "$left" -le 0 ] || sleep "$((left < STEP_S ? left : STEP_S))"
    done
    [ -z "$missing" ] || break
  done
  if [ -n "$missing" ]; then
    log "$what: $missing still missing after ${WAIT_S}s; NOT starting it"
    rc=1
    continue
  fi

  if ! { [ -f "$conf" ] && onboot "$conf"; }; then
    log "$what: no longer onboot; left stopped"
    continue
  fi
  if ha_managed "$kind" "$id"; then
    log "$what: now HA-managed, left to ha-manager"
    continue
  fi
  state=$(guest_state "$kind" "$id") || { rc=1; continue; }
  if [ "$state" != stopped ]; then
    log "$what: now $state; nothing to do"
    continue
  fi
  if guest_start "$kind" "$id"; then
    log "$what: started (its NFS storage was ready after $((SECONDS - start))s)"
  else
    log "$what: start failed"
    rc=1
  fi
done
exit "$rc"
