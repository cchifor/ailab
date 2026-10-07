#!/usr/bin/env bash
# Pressure-guarded, chunked fstrim of one LXC mount point (plans/2026-10-07-etcd-leader-churn-plan.md
# step E).
#
#   lxc-fstrim-chunked.sh <ctid> <mountpoint-inside-the-container>
#   e.g. lxc-fstrim-chunked.sh 5004 /var/lib/registry     (the Zot registry LXC on ai-node1)
#
# WHY. The host's fstrim.timer never reaches LXC mount points. The registry LXC's thin volume on
# ai-node1 grew to 377 GB allocated for 170 GB in use, and the extra thin-pool fill measurably slows
# the shared consumer QLC NVMe that also carries cp1's etcd WAL. Zot's hourly GC keeps freeing blobs,
# so the space comes back only if something trims it. `pct fstrim` does the whole volume in one discard
# burst. This trims CHUNK_GB at a time, inside the container's mount namespace (a host path through
# /proc/<pid>/root fails fstrim's realpath), waiting while host IO pressure is above PSI_MAX and
# pausing PAUSE_S between chunks. The 2026-10-06 one-off run of this logic freed 212 GB and took the
# pool from 71.6% to 59.8%.
#
# Exit: 0 = done, or stopped early by STOP_FILE; 1 = container not running / cannot size the mount;
#       2 = usage; 3 = IO pressure stayed above PSI_MAX for PSI_WAIT_MAX_S (nothing more trimmed).
# Deploy (ai-node1, via scripts/node-ssh.py): docs/runbooks/registry-cache.md, "Thin-volume trim".
set -uo pipefail

CTID="${1:-}"; MP="${2:-}"
[ -n "$CTID" ] && [ -n "$MP" ] || { echo "usage: $0 <ctid> <mountpoint>"; exit 2; }

CHUNK_GB="${CHUNK_GB:-8}"
PAUSE_S="${PAUSE_S:-15}"
PSI_MAX="${PSI_MAX:-45}"                 # host IO "some avg10" percent above which we wait
PSI_WAIT_MAX_S="${PSI_WAIT_MAX_S:-1800}" # give up (exit 3) after waiting this long for one chunk
PSI_FILE="${PSI_FILE:-/proc/pressure/io}"
STOP_FILE="${STOP_FILE:-/run/lxc-fstrim-chunked.stop}"
POOL="${POOL:-pve/data}"

log() { echo "$(date -u +%FT%TZ) lxc-fstrim-chunked[$CTID]: $*"; }
psi() { sed -E 's/.*avg10=([0-9]+).*/\1/;q' "$PSI_FILE"; }
pool() { lvs --noheadings -o data_percent "$POOL" 2>/dev/null | tr -d ' ' || echo "?"; }

pid="$(lxc-info -n "$CTID" -p -H 2>/dev/null)"
[ -n "$pid" ] || { log "container $CTID is not running - nothing trimmed"; exit 1; }
size="$(nsenter -t "$pid" -m -- df -B1 --output=size "$MP" 2>/dev/null | tail -n 1 | tr -d ' ')"
case "$size" in ''|*[!0-9]*) log "cannot size $MP inside $CTID (got '$size')"; exit 1 ;; esac

chunk=$(( CHUNK_GB * 1024 * 1024 * 1024 ))
chunks=$(( (size + chunk - 1) / chunk ))
total=0
log "start: $MP size=$size bytes chunks=$chunks chunk=${CHUNK_GB}GiB pool=$(pool)%"

for (( i = 0; i < chunks; i++ )); do
  if [ -e "$STOP_FILE" ]; then log "stop file $STOP_FILE present - stopping before chunk $i"; break; fi
  waited=0
  while [ "$(psi)" -gt "$PSI_MAX" ]; do
    if [ "$waited" -ge "$PSI_WAIT_MAX_S" ]; then
      log "host IO pressure stayed above ${PSI_MAX}% for ${waited}s - giving up at chunk $i trimmed_total=$total"
      exit 3
    fi
    sleep "$PAUSE_S"; waited=$(( waited + PAUSE_S ))
  done
  off=$(( i * chunk ))
  out="$(nsenter -t "$pid" -m -- fstrim -v -o "$off" -l "$chunk" "$MP" 2>&1)"
  bytes="$(sed -nE 's/.*\(([0-9]+) bytes\) trimmed.*/\1/p' <<< "$out")"
  total=$(( total + ${bytes:-0} ))
  log "chunk=$((i + 1))/$chunks off=$off waited=${waited}s psi=$(psi) ${out#*: }"
  sleep "$PAUSE_S"
done

log "end: trimmed_total=$total pool=$(pool)%"
exit 0
