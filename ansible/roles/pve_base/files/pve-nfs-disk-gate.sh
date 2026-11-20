#!/usr/bin/env bash
# Hold this node's onboot guest start until the NFS storage holding an onboot VM's disk is ready, for a
# bounded time. Runs as an ExecStartPre of pve-guests.service (drop-in 10-ailab-nfs-disk-gate.conf).
# Deployed by the pve_base role (tag nfs_disk_gate); docs/runbooks/node-maintenance.md "Boot-order guard".
#
# WHY: since 2026-11-20 the Talos control-plane disks (VMs 4001-4003) live on qnap-nfs (ADR 0043).
# pve-guests starts the onboot guests ONCE per boot and never retries, and PVE refuses to start a VM whose
# storage is offline ("storage 'qnap-nfs' is not online"). The hosts reach the NAS over the storage link,
# which comes up during boot, and after a power cut the NAS served NFS ~6 min after the hosts (2026-10-30).
# Without this gate the CP stays stopped until someone starts it by hand.
#
# Per gated storage (GATE_STORAGES) that holds a disk of an onboot VM on this node, against ONE elapsed-time
# budget (WAIT_S) shared by all of them, it waits until the storage is:
#   1. mounted: /proc/self/mountinfo shows an NFS mount exactly at the storage's path. This reads procfs only,
#      never stats the mount point, so it cannot hang. pvestatd mounts the storage once the server answers;
#      while it is not mounted, the gate asks PVE to activate it (`pvesm status --storage`), as `qm start` would;
#   2. writable: a file of this node under <path>/.pve-nfs-disk-gate/ is written, fsync'ed (an NFS COMMIT)
#      and read back;
#   3. serving the disks: the first 4 KiB of every such disk image is read, so an empty export fails.
# Every probe runs in the background and is ABANDONED, never waited for, when it overruns: I/O on a hard NFS
# mount can block in D state past SIGKILL.
#
# It never fails pve-guests. Exit 0 = ready, or nothing on this node to wait for. Exit 1 = the budget ran out,
# or the settings name a storage it cannot gate; the drop-in's "-" prefix ignores the status, and the WARNING
# is in the journal at priority warning. After a timeout pve-guests starts the guests as it always did, the
# gated VMs included. PVE refuses to start one while its storage is offline, and starts it if the storage came
# back meanwhile; the defaults in the role explain why the gate does not skip them.
set -u

NODE="${NODE:-$(hostname)}"
CONF_DIR="${CONF_DIR:-/etc/pve/nodes/$NODE/qemu-server}"
STORAGE_CFG="${STORAGE_CFG:-/etc/pve/storage.cfg}"
MOUNTINFO="${MOUNTINFO:-/proc/self/mountinfo}"
PVESM="${PVESM:-pvesm}"
GATE_STORAGES="${GATE_STORAGES:-qnap-nfs}"
GATE_FSTYPES="${GATE_FSTYPES:-nfs nfs4 cifs smb3}"
PROBE_DIR="${PROBE_DIR:-.pve-nfs-disk-gate}"
WAIT_S="${WAIT_S:-600}"     # elapsed, across storages
STEP_S="${STEP_S:-5}"       # between checks
PROBE_S="${PROBE_S:-10}"    # one read or write probe
ACTIVATE_S="${ACTIVATE_S:-30}"  # one `pvesm status`: PVE's rpcinfo check alone may take 10 s, then the mount

for v in WAIT_S STEP_S PROBE_S ACTIVATE_S; do
  [[ "${!v}" =~ ^[0-9]+$ ]] || { echo "pve-nfs-disk-gate: $v=${!v} is not a number of seconds; not waiting"; exit 1; }
done

log() { echo "pve-nfs-disk-gate: $*"; }
# systemd reads a leading <4> on a service's output line as priority warning (SyslogLevelPrefix=yes).
warn() { echo "${JOURNAL_STREAM:+<4>}pve-nfs-disk-gate: WARNING: $*"; }

in_list() { local x=$1 y; shift; for y; do [ "$x" = "$y" ] && return 0; done; return 1; }

# The live section only: snapshot sections ([name]) repeat drives that are not in effect.
live_config() { awk '/^\[/{exit} {print}' "$1"; }

# Volume IDs of the drives a VM starts with (ideN, sataN, scsiN, virtioN, efidisk0, tpmstate0; not unusedN):
# the item without a key, or the file= item. "none" and absolute paths (passthrough devices) are skipped.
DRIVE_RE='^(ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+:[[:space:]]*(.*)$'
drive_volids() {
  local line volid item
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
    case "$volid" in "" | none | /*) continue ;; esac
    [[ "$volid" == *:* ]] && printf '%s\n' "$volid"
  done
  return 0
}

# A VM disk image's path relative to its directory-type storage: <vmid>/<file>, or a linked clone's
# <basevmid>/<basefile>/<vmid>/<file>, lives in images/<vmid>/<file>. Other volumes (an ISO) print nothing.
image_relpath() {
  if [[ "$1" =~ ^([0-9]+/[^/]+)$ ]] || [[ "$1" =~ ^[0-9]+/[^/]+/([0-9]+/[^/]+)$ ]]; then
    printf 'images/%s\n' "${BASH_REMATCH[1]}"
  fi
}

# "<type> <path>" of storage $1 in storage.cfg ("<type>: <id>" headers, indented "<key> <value>" lines).
storage_type_path() {
  awk -v id="$1" '
    /^[^[:space:]#]/ { cur = ($2 == id); if (cur) { typ = $1; sub(/:$/, "", typ) } next }
    cur && $1 == "path" { path = $2 }
    END { if (typ != "") print typ, path }
  ' "$STORAGE_CFG"
}

# The filesystem type mounted exactly at $1 (the topmost mount there), from procfs: no stat, cannot hang.
mount_fstype() {
  awk -v mp="$1" '{ for (i = 7; i <= NF; i++) if ($i == "-") { if ($5 == mp) t = $(i + 1); break } } END { print t }' "$MOUNTINFO"
}

write_probe() {  # $1 = the storage path
  local d="$1/$PROBE_DIR" tok
  tok="$NODE $(date -u +%Y-%m-%dT%H:%M:%SZ) $$"
  mkdir -p "$d" && printf '%s\n' "$tok" > "$d/$NODE" && sync "$d/$NODE" && [ "$(cat "$d/$NODE")" = "$tok" ]
}

read_probe() { dd if="$1" of=/dev/null bs=4096 count=1 status=none; }

# probe LIMIT CMD...: run CMD in the background for at most LIMIT seconds, capped by the shared deadline.
# 0 = it finished in time and succeeded. TIMED_OUT=1 when it was abandoned (or the budget was already spent).
probe() {
  local limit=$1 pid i
  shift
  TIMED_OUT=""
  [ "$((deadline - SECONDS))" -ge "$limit" ] || limit=$((deadline - SECONDS))
  if [ "$limit" -le 0 ]; then TIMED_OUT=1; return 1; fi
  # stdio detached, so an abandoned probe cannot hold the unit's journal stream either.
  ("$@") < /dev/null > /dev/null 2>&1 &
  pid=$!
  for ((i = 0; i < limit * 10; i++)); do
    if ! kill -0 "$pid" 2> /dev/null; then
      wait "$pid"
      return
    fi
    sleep 0.1
  done
  kill -9 "$pid" 2> /dev/null
  TIMED_OUT=1
  return 1  # abandoned: never `wait` on a child that may be unkillable
}

# check_storage ID PATH: WHY="" when the storage is ready, else what is missing.
check_storage() {
  local sid=$1 path=$2 fstype img
  fstype=$(mount_fstype "$path")
  if ! in_list "$fstype" $GATE_FSTYPES && [ -z "${activate_hung[$sid]:-}" ]; then
    probe "$ACTIVATE_S" "$PVESM" status --storage "$sid"
    [ -z "$TIMED_OUT" ] || activate_hung[$sid]=1  # leave it to pvestatd from now on
    fstype=$(mount_fstype "$path")
  fi
  if ! in_list "$fstype" $GATE_FSTYPES; then
    WHY="not mounted at $path${fstype:+ (the mount there is $fstype)}"
    return 1
  fi
  if ! probe "$PROBE_S" write_probe "$path"; then
    WHY="the write probe under $path/$PROBE_DIR $(outcome)"
    return 1
  fi
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    if ! probe "$PROBE_S" read_probe "$path/$img"; then
      WHY="reading $path/$img $(outcome)"
      return 1
    fi
  done <<< "${imgs[$sid]:-}"
  WHY=""
}

outcome() { if [ -n "$TIMED_OUT" ]; then echo "timed out"; else echo failed; fi; }

# ---- What this node's onboot VMs need, fixed once, up front ----
declare -A vms=() imgs=() activate_hung=()
shopt -s nullglob
for conf in "$CONF_DIR"/*.conf; do
  vmid=$(basename "$conf" .conf)
  live=$(live_config "$conf")
  grep -qx 'onboot: 1' <<< "$live" || continue
  grep -qx 'template: 1' <<< "$live" && continue
  while IFS= read -r volid; do
    sid=${volid%%:*}
    # shellcheck disable=SC2086  # GATE_STORAGES is a word list
    in_list "$sid" $GATE_STORAGES || continue
    [[ " ${vms[$sid]:-} " == *" $vmid "* ]] || vms[$sid]="${vms[$sid]:-}${vms[$sid]:+ }$vmid"
    img=$(image_relpath "${volid#*:}")
    [ -z "$img" ] || imgs[$sid]="${imgs[$sid]:-}${imgs[$sid]:+$'\n'}$img"
  done < <(drive_volids <<< "$live")
done

if [ "${#vms[@]}" -eq 0 ]; then
  log "no onboot VM on $NODE has a disk on: $GATE_STORAGES; nothing to wait for"
  exit 0
fi

start=$SECONDS
deadline=$((start + WAIT_S))
rc=0
for sid in $GATE_STORAGES; do
  [ -n "${vms[$sid]:-}" ] || continue
  read -r stype spath <<< "$(storage_type_path "$sid")"
  if [ -z "${stype:-}" ]; then
    warn "$sid is not in $STORAGE_CFG: not waiting for it (VM ${vms[$sid]})"
    rc=1
    continue
  fi
  case "$stype" in
    nfs | cifs) ;;
    *)
      warn "$sid is a $stype storage, not NFS or CIFS: not waiting for it (VM ${vms[$sid]})"
      rc=1
      continue
      ;;
  esac
  spath=${spath:-/mnt/pve/$sid}
  log "waiting up to $((deadline - SECONDS))s for $sid at $spath (VM ${vms[$sid]})"
  # A check starts only with budget left, so WHY is always the outcome of a check that ran.
  while ! check_storage "$sid" "$spath"; do
    left=$((deadline - SECONDS))
    [ "$left" -gt 0 ] || break
    sleep "$((left < STEP_S ? left : STEP_S))"
    [ "$((deadline - SECONDS))" -gt 0 ] || break
  done
  if [ -z "$WHY" ]; then
    log "$sid ready after $((SECONDS - start))s: mounted, writable, serving the disks of VM ${vms[$sid]}"
  else
    warn "$sid not ready after $((SECONDS - start))s: $WHY. Handing over to pve-guests anyway: PVE refuses" \
      "to start VM ${vms[$sid]} while $sid is offline. If one stays stopped, start it with 'qm start <vmid>'" \
      "once the share is back (docs/runbooks/node-maintenance.md, \"Boot-order guard\")."
    rc=1
  fi
done
exit "$rc"
