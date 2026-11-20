#!/usr/bin/env bash
# Pressure-guarded, chunked fstrim of one LXC volume (plans/2026-10-07-etcd-leader-churn-plan.md step E).
#
#   lxc-fstrim-chunked.sh <ctid> <rootfs|mpN>
#   e.g. lxc-fstrim-chunked.sh 5004 mp0     (the Zot registry LXC's data volume on ai-node1, weekly)
#   The nightly sweep of every running container (role pve_base, files/pve-lxc-fstrim.sh) calls it once
#   per local-lvm volume, with a DEADLINE.
#
# WHY. The host's fstrim.timer never reaches LXC mount points. The registry LXC's thin volume on
# ai-node1 grew to 377 GB allocated for 170 GB in use, and the extra thin-pool fill measurably slows
# the shared consumer QLC NVMe that also carries cp1's etcd WAL. Zot's hourly GC keeps freeing blobs,
# so the space comes back only if something trims it. `pct fstrim` does the whole volume in one
# discard burst; this trims CHUNK_GB at a time.
#
# HOST-SIDE ONLY.
# - Nothing from inside the container is ever executed. An earlier version ran df/fstrim via
#   `nsenter -m`, i.e. the CONTAINER's binaries and libraries with host-root credentials. Codex
#   impl-review, 2026-10-07: container root could swap them for host-root code execution.
# - Instead the mpN volume is resolved with `pct config` + `pvesm path`, and its ext4 geometry is
#   read with `tune2fs`. FITRIM addresses the full block range, which df understates by the metadata
#   overhead.
# - The volume is mounted privately on the host (nosuid,nodev,noexec). The kernel shares the
#   superblock with the container's own mount. The host's fstrim then trims that mount.
# - It waits while host IO pressure (some avg10) is above PSI_MAX, refuses to run when pressure is
#   unreadable, and pauses PAUSE_S between chunks (PAUSE_IDLE_S after a chunk that trimmed 0 bytes: ext4
#   skips the block groups it has trimmed since the mount and seen no free in since, so such a chunk sent
#   no discard at all. The default is PAUSE_S, i.e. no difference).
# - DEADLINE (Unix seconds, optional): no chunk starts unless it can end by then at SEC_PER_GB, and a
#   pressure wait ends there too. The run then stops cleanly (exit 8). A chunk already running is never
#   cut short; the caller's unit allows for one (TimeoutStartSec, TimeoutStopSec).
# - CURSOR_FILE (optional): where the run stopped, so the next run continues there instead of at chunk 1.
#   ext4's trimmed-group flags only skip groups nothing was freed in since, so without it a volume whose
#   first chunks keep churning could use up every night's DEADLINE and never reach its tail. A pass runs
#   from chunk 1 to the last across as many runs as it takes; the file is written on every exit before the
#   last chunk (deadline, stop file, signal, failure) and removed when the pass reaches it. A cursor for
#   another geometry starts a new pass. Without it (the registry's weekly unit) every run starts at chunk 1.
#
# Exit codes:
#   0 = done, or stopped by STOP_FILE (checked before each chunk and during pressure waits)
#   1 = rootfs/mount point/volume not resolvable, not a block device, or not ext2/3/4; mount failed
#   2 = usage or invalid settings
#   3 = IO pressure stayed above PSI_MAX for PSI_WAIT_MAX_S
#   4 = IO pressure unreadable
#   5 = an fstrim failed
#   6 = the run succeeded but the host-side mount could not be released (the volume stays mounted)
#   143/130 = stopped by SIGTERM/SIGINT (after the in-flight trim/sleep finished and the mount was released)
#   7 = another run holds the lock (LOCK_FILE)
#   8 = DEADLINE reached before the whole volume was covered (stopped cleanly, mount released)
# Deploy (ai-node1, via scripts/node-ssh.py): docs/runbooks/registry-cache.md, "Thin-volume trim".
set -uo pipefail

CTID="${1:-}"; MPKEY="${2:-}"
usage() { echo "usage: $0 <ctid> <rootfs|mpN>   (settings: CHUNK_GB PAUSE_S PAUSE_IDLE_S PSI_MAX PSI_WAIT_MAX_S PSI_FILE STOP_FILE DEADLINE SEC_PER_GB)"; exit 2; }
[[ "$CTID" =~ ^[0-9]+$ && "$MPKEY" =~ ^(rootfs|mp[0-9]+)$ ]] || usage

CHUNK_GB="${CHUNK_GB:-8}"
PAUSE_S="${PAUSE_S:-15}"
PSI_MAX="${PSI_MAX:-45}"                 # host IO "some avg10" percent above which we wait
PSI_WAIT_MAX_S="${PSI_WAIT_MAX_S:-1800}" # give up (exit 3) after waiting this long for one chunk
PAUSE_IDLE_S="${PAUSE_IDLE_S:-$PAUSE_S}" # pause after a chunk that trimmed 0 bytes
DEADLINE="${DEADLINE:-}"                 # Unix seconds; empty = none (the registry's weekly unit)
SEC_PER_GB="${SEC_PER_GB:-30}"           # a chunk's worst case for DEADLINE (measured 17-23 s per GiB)
CURSOR_FILE="${CURSOR_FILE:-}"           # empty = every run starts at chunk 1
PSI_FILE="${PSI_FILE:-/proc/pressure/io}"
STOP_FILE="${STOP_FILE:-/run/lxc-fstrim-chunked.stop}"
MOUNT_BASE="${MOUNT_BASE:-/run}"
POOL="${POOL:-pve/data}"

log() { echo "$(date -u +%FT%TZ) lxc-fstrim-chunked[$CTID/$MPKEY]: $*"; }
die() { local rc="$1"; shift; log "$*"; exit "$rc"; }
posint() { [[ "$1" =~ ^[1-9][0-9]*$ ]] && [ "${#1}" -le 6 ]; }
posint "$CHUNK_GB"       && [ "$CHUNK_GB" -le 1024 ] || die 2 "invalid CHUNK_GB='$CHUNK_GB' (1-1024)"
posint "$PAUSE_S"        || die 2 "invalid PAUSE_S='$PAUSE_S' (positive integer seconds)"
posint "$PSI_WAIT_MAX_S" || die 2 "invalid PSI_WAIT_MAX_S='$PSI_WAIT_MAX_S' (positive integer seconds)"
posint "$PAUSE_IDLE_S"   || die 2 "invalid PAUSE_IDLE_S='$PAUSE_IDLE_S' (positive integer seconds)"
posint "$SEC_PER_GB"     || die 2 "invalid SEC_PER_GB='$SEC_PER_GB' (positive integer seconds)"
[ -z "$DEADLINE" ] || [[ "$DEADLINE" =~ ^[1-9][0-9]{0,11}$ ]] || die 2 "invalid DEADLINE='$DEADLINE' (Unix seconds)"
[[ "$PSI_MAX" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v m="$PSI_MAX" 'BEGIN { exit !(m >= 0 && m <= 100) }' \
  || die 2 "invalid PSI_MAX='$PSI_MAX' (0-100)"

# Host IO pressure "some avg10" as a decimal; empty output = unreadable.
psi() { awk '$1 == "some" { for (i = 2; i <= NF; i++) if ($i ~ /^avg10=[0-9]+(\.[0-9]+)?$/) { sub(/^avg10=/, "", $i); print $i; exit } }' "$PSI_FILE" 2>/dev/null; }
above() { awk -v a="$1" -v m="$PSI_MAX" 'BEGIN { exit !(a > m) }'; }
pool() { lvs --noheadings -o data_percent "$POOL" 2>/dev/null | tr -d ' ' || echo "?"; }
stopped() { [ -e "$STOP_FILE" ]; }
# True when a chunk started now could not end by DEADLINE at SEC_PER_GB (never true without a DEADLINE).
past_deadline() { [ -n "$DEADLINE" ] && [ $(( $(date +%s) + CHUNK_GB * SEC_PER_GB )) -gt "$DEADLINE" ]; }

# One run at a time (reviewer-claude on #1130): a hand-run supervised trim overlapping the timer's run
# would mount the LV twice and double the discard bursts on cp1's WAL drive. The lock is released when
# this process and its children exit.
LOCK_FILE="${LOCK_FILE:-/run/lxc-fstrim-chunked.lock}"
exec 9>"$LOCK_FILE" || die 1 "cannot open lock file $LOCK_FILE"
flock -n 9 || die 7 "another run holds $LOCK_FILE - not starting a second, overlapping trim"

# --- resolve the volume from host-side metadata only ----------------------------------------------
# The main section only: a snapshot's copy of the key follows a `[name]` line.
volid="$(pct config "$CTID" 2>/dev/null | sed -nE "/^\[/q; s/^${MPKEY}: ([^,]+)(,.*)?$/\1/p")"
[ -n "$volid" ] || die 1 "$MPKEY not found in 'pct config $CTID'"
dev="$(pvesm path "$volid" 2>/dev/null)"
[ -n "$dev" ] || die 1 "pvesm path $volid returned nothing"
[ "$(stat -L -c %F "$dev" 2>/dev/null)" = "block special file" ] || die 1 "$dev ($volid) is not a block device"
geo="$(tune2fs -l "$dev" 2>/dev/null)" || die 1 "tune2fs -l $dev failed (not ext2/3/4?)"
blocks="$(sed -nE 's/^Block count:[[:space:]]+([0-9]+)$/\1/p' <<< "$geo")"
bsize="$(sed -nE 's/^Block size:[[:space:]]+([0-9]+)$/\1/p' <<< "$geo")"
[[ "$blocks" =~ ^[0-9]+$ && "$bsize" =~ ^[0-9]+$ ]] || die 1 "cannot read ext4 geometry of $dev"
size=$(( blocks * bsize ))

mnt="$(mktemp -d "$MOUNT_BASE/lxc-fstrim-$CTID-$MPKEY.XXXXXX")" || die 1 "mktemp under $MOUNT_BASE failed"
mount -o rw,nosuid,nodev,noexec "$dev" "$mnt" || { rmdir "$mnt"; die 1 "mount $dev on $mnt failed"; }
# Explicit cleanup, not a bare `trap 'umount && rmdir'`: an EXIT trap keeps the status that caused the
# exit, so a failed umount (EBUSY) would leave a host-namespace mount of the LV behind while systemd saw
# success (Codex impl-review round 2). Keep the original failure if there was one; otherwise a cleanup
# failure is the run's failure (exit 6). Remove the directory only after a successful unmount.
cleanup() {
  local rc=$?
  rm -f "$outf"
  save_cursor
  if umount "$mnt"; then
    rmdir "$mnt" || log "cleanup: rmdir $mnt failed (unmounted; empty dir left behind)"
  else
    log "cleanup: umount $mnt ($dev) failed - the volume is still mounted on the host; release it by hand: umount $mnt"
    [ "$rc" -eq 0 ] && rc=6
  fi
  exit "$rc"
}
# `systemctl stop` SIGTERMs the whole unit. Bash would run the EXIT trap at once, while an in-flight
# FITRIM (an ioctl, so it cannot die mid-call) still holds the mount: umount gets EBUSY and the mount
# is left behind (Codex review of #1130). fstrim and sleep therefore run as tracked children
# (`run_child`), and a TERM/INT waits for the in-flight one before exiting through cleanup.
child=""
on_signal() {
  trap - TERM INT
  log "terminated by signal - waiting for in-flight ${child:+pid $child }before releasing the mount"
  [ -n "$child" ] && wait "$child" 2>/dev/null
  exit "$1"
}
run_child() { "$@" & child=$!; wait "$child"; local rc=$?; child=""; return "$rc"; }
# Writes or clears CURSOR_FILE (from cleanup, so every exit after the volume is mounted records progress).
cursor_ready=0
save_cursor() {
  [ -n "$CURSOR_FILE" ] && [ "$cursor_ready" = 1 ] || return 0
  if [ "$pass_done" = 1 ]; then rm -f "$CURSOR_FILE"; return 0; fi
  local tmp
  tmp="$(mktemp "$CURSOR_FILE.XXXXXX" 2>/dev/null)" || { log "warning: cannot write $CURSOR_FILE"; return 0; }
  if printf 'next=%s chunks=%s size=%s chunk=%s\n' "$next" "$chunks" "$size" "$chunk" > "$tmp" && mv -f "$tmp" "$CURSOR_FILE"; then
    return 0
  fi
  rm -f "$tmp"; log "warning: cannot write $CURSOR_FILE"
}
outf="$(mktemp "$MOUNT_BASE/lxc-fstrim-out.XXXXXX")" || { umount "$mnt" && rmdir "$mnt"; die 1 "mktemp under $MOUNT_BASE failed"; }
trap cleanup EXIT
trap 'on_signal 143' TERM
trap 'on_signal 130' INT

chunk=$(( CHUNK_GB * 1024 * 1024 * 1024 ))
chunks=$(( (size + chunk - 1) / chunk ))
total=0
# The cursor: `next` is the chunk the next run starts at; pass_done once the last chunk is trimmed.
next=0; pass_done=0
if [ -n "$CURSOR_FILE" ] && [ -r "$CURSOR_FILE" ]; then
  cur="$(head -n 1 "$CURSOR_FILE" 2>/dev/null | head -c 200)"
  if [[ "$cur" =~ ^next=([0-9]+)\ chunks=([0-9]+)\ size=([0-9]+)\ chunk=([0-9]+)$ ]] \
    && [ "${BASH_REMATCH[2]}" = "$chunks" ] && [ "${BASH_REMATCH[3]}" = "$size" ] && [ "${BASH_REMATCH[4]}" = "$chunk" ] \
    && [ "${BASH_REMATCH[1]}" -lt "$chunks" ]; then
    next="${BASH_REMATCH[1]}"
  else
    log "the cursor in $CURSOR_FILE ($cur) does not fit this volume - starting a new pass at chunk 1"
  fi
fi
cursor_ready=1
log "start: $volid ($dev) size=$size bytes chunks=$chunks chunk=${CHUNK_GB}GiB from chunk $((next + 1)) pool=$(pool)%"

for (( i = next; i < chunks; i++ )); do
  next=$i
  stopped && { log "stop file $STOP_FILE present - stopping before chunk $((i + 1)) trimmed_total=$total"; exit 0; }
  past_deadline && die 8 "deadline reached - stopping before chunk $((i + 1))/$chunks trimmed_total=$total"
  waited=0
  while :; do
    p="$(psi)"
    [ -n "$p" ] || die 4 "host IO pressure unreadable from $PSI_FILE - refusing to trim (chunk $((i + 1))) trimmed_total=$total"
    above "$p" "$PSI_MAX" || break
    [ "$waited" -lt "$PSI_WAIT_MAX_S" ] \
      || die 3 "host IO pressure stayed above ${PSI_MAX}% (now $p) for ${waited}s - giving up at chunk $((i + 1)) trimmed_total=$total"
    run_child sleep "$PAUSE_S"; waited=$(( waited + PAUSE_S ))
    stopped && { log "stop file $STOP_FILE present - stopping during the pressure wait before chunk $((i + 1)) trimmed_total=$total"; exit 0; }
    past_deadline && die 8 "deadline reached during the pressure wait before chunk $((i + 1))/$chunks trimmed_total=$total"
  done
  off=$(( i * chunk ))
  if ! run_child fstrim -v -o "$off" -l "$chunk" "$mnt" > "$outf" 2>&1; then
    die 5 "fstrim failed at chunk $((i + 1))/$chunks off=$off: $(cat "$outf") trimmed_total=$total"
  fi
  out="$(cat "$outf")"
  bytes="$(sed -nE 's/.*\(([0-9]+) bytes\) trimmed.*/\1/p' <<< "$out")"
  total=$(( total + ${bytes:-0} ))
  log "chunk=$((i + 1))/$chunks off=$off waited=${waited}s psi=$p ${out#*: }"
  next=$(( i + 1 )); [ "$next" -ge "$chunks" ] && pass_done=1
  if [ "${bytes:-0}" -eq 0 ]; then run_child sleep "$PAUSE_IDLE_S"; else run_child sleep "$PAUSE_S"; fi
done

log "end: trimmed_total=$total pool=$(pool)%"
exit 0
