#!/usr/bin/env bash
# node-fstrim.sh <mountpoint>... - a chunked, IO-pressure-guarded, deadline-bound fstrim of mounted
# filesystems, in place (no mount of its own). The nightly Talos node trim runs it in a CronJob pod per node
# on the host's /var (kubernetes/apps/node-fstrim); any Linux guest on a thin volume can run it the same way.
#
# WHY. The guests' disks are thin LVs on each Proxmox host's local-lvm, with discard passed down to a QLC
# NVMe that a Talos control plane's etcd shares. A filesystem that frees blocks without discarding them
# leaves them allocated in the thin pool: Talos mounts /var (XFS) without `discard` and nothing trimmed it,
# so on 2026-11-20 about 195 GiB were freed but still allocated across the seven Talos nodes. One fstrim of
# a whole filesystem is a single discard burst on the etcd drive; this trims CHUNK_GB at a time.
#
# HOW. Per mount point, in order: the filesystem's size from statfs, then chunks of CHUNK_GB:
#   fstrim -v -m <MINLEN> -o <offset> -l <CHUNK> <mnt>
# The LAST chunk has no -l: it runs to the filesystem's real end. statfs understates the FITRIM address
# space (XFS leaves out its internal log, ext4 its metadata), and an offset past the end is EINVAL, so the
# tail is covered without knowing its size. -m 64K by default: the thin pool's chunk is 64 KiB, and a
# smaller discard cannot free one.
# Before each chunk: the stop file, the deadline (DEADLINE = start + RUN_MAX_S; no chunk starts unless it can
# end by then at SEC_PER_GB), and IO pressure (`some avg10` in PSI_FILE above PSI_MAX: wait, PAUSE_MAX_S at a
# time, up to PSI_WAIT_MAX_S; unreadable pressure refuses to trim). After each chunk it rests as long as the
# chunk took (between PAUSE_MIN_S and PAUSE_MAX_S): a chunk the thin pool had nothing to drop for returns at
# once and costs a 2 s rest, a heavy one is followed by as long a break. A chunk already running is never cut
# short: the caller allows for one (the pod's terminationGracePeriodSeconds, a unit's TimeoutStopSec).
#
# XFS has no memory of what it trimmed: every run discards every free extent again. The thin pool drops what
# is already unmapped quickly, so only what was freed since the last run costs the NVMe anything; the bytes
# fstrim reports are the free space it walked, not what the pool reclaimed (that is visible only on the host).
#
# Settings (environment): CHUNK_GB 4, MINLEN 64K, PAUSE_MIN_S 2, PAUSE_MAX_S 30, PSI_MAX 45, PSI_WAIT_MAX_S
# 240, PSI_FILE /proc/pressure/io, RUN_MAX_S 360, SEC_PER_GB 30, STOP_FILE /run/node-fstrim.stop, LOCK_FILE
# /run/node-fstrim.lock, TEXTFILE_DIR (empty: no beacon; else node_fstrim.prom there for node_exporter).
#
# Exit codes:
#   0   done, or stopped early by the deadline (a partial run: the next night covers the rest; logged
#       `partial`) or the stop file
#   1   an argument is not a mount point, or its size is unreadable; or the lock file cannot be opened
#   2   usage or invalid settings
#   3   IO pressure stayed above PSI_MAX for PSI_WAIT_MAX_S, or until the deadline, before a chunk
#   4   IO pressure unreadable
#   5   an fstrim failed
#   7   another run holds LOCK_FILE
#   143/130  SIGTERM/SIGINT (after the in-flight chunk finished)
set -uo pipefail

CHUNK_GB="${CHUNK_GB:-4}"
MINLEN="${MINLEN:-64K}"
PAUSE_MIN_S="${PAUSE_MIN_S:-2}"
PAUSE_MAX_S="${PAUSE_MAX_S:-30}"
PSI_MAX="${PSI_MAX:-45}"
PSI_WAIT_MAX_S="${PSI_WAIT_MAX_S:-240}"
PSI_FILE="${PSI_FILE:-/proc/pressure/io}"
RUN_MAX_S="${RUN_MAX_S:-360}"
SEC_PER_GB="${SEC_PER_GB:-30}"
STOP_FILE="${STOP_FILE:-/run/node-fstrim.stop}"
LOCK_FILE="${LOCK_FILE:-/run/node-fstrim.lock}"
TEXTFILE_DIR="${TEXTFILE_DIR:-}"
GiB=1073741824

log() { echo "$(date -u +%FT%TZ) node-fstrim: $*"; }
now_s() { date +%s; }
posint() { [[ "${1:-}" =~ ^[1-9][0-9]{0,6}$ ]]; }

[ $# -ge 1 ] || { echo "usage: $0 <mountpoint>...   (settings: see the header)"; exit 2; }
bad=""
posint "$CHUNK_GB" && [ "$CHUNK_GB" -le 1024 ] || bad="CHUNK_GB='$CHUNK_GB' (1-1024)"
[[ "$MINLEN" =~ ^[0-9]+[KMG]?$ ]] || bad="MINLEN='$MINLEN' (bytes, or with K/M/G)"
posint "$PAUSE_MIN_S" || bad="PAUSE_MIN_S='$PAUSE_MIN_S'"
posint "$PAUSE_MAX_S" && [ "$PAUSE_MAX_S" -ge "${PAUSE_MIN_S:-0}" ] 2>/dev/null || bad="PAUSE_MAX_S='$PAUSE_MAX_S' (>= PAUSE_MIN_S)"
posint "$PSI_WAIT_MAX_S" || bad="PSI_WAIT_MAX_S='$PSI_WAIT_MAX_S'"
posint "$RUN_MAX_S" || bad="RUN_MAX_S='$RUN_MAX_S'"
posint "$SEC_PER_GB" || bad="SEC_PER_GB='$SEC_PER_GB'"
[[ "$PSI_MAX" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v m="$PSI_MAX" 'BEGIN { exit !(m >= 0 && m <= 100) }' || bad="PSI_MAX='$PSI_MAX' (0-100)"
[ -z "$bad" ] || { log "invalid setting $bad"; exit 2; }

exec 9>"$LOCK_FILE" || { log "cannot open lock file $LOCK_FILE"; exit 1; }
flock -n 9 || { log "another run holds $LOCK_FILE - not starting a second, overlapping trim"; exit 7; }

start="$(now_s)"
deadline=$(( start + RUN_MAX_S ))
chunk=$(( CHUNK_GB * GiB ))
declare -A TRIMMED=() COMPLETE=()

write_beacon() { # <rc> <reason>
  [ -n "$TEXTFILE_DIR" ] && [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/node_fstrim.prom" tmp end last_ok m
  end="$(now_s)"
  last_ok="$(sed -nE 's/^node_fstrim_last_success_timestamp_seconds ([0-9]+)$/\1/p' "$f" 2>/dev/null | tail -1)"
  [ "$2" = done ] && last_ok="$end"
  tmp="$(mktemp "$TEXTFILE_DIR/.node_fstrim.prom.XXXXXX" 2>/dev/null)" || return 0
  {
    echo "# HELP node_fstrim_last_run_timestamp_seconds When node-fstrim last ended."
    echo "# TYPE node_fstrim_last_run_timestamp_seconds gauge"
    echo "node_fstrim_last_run_timestamp_seconds $end"
    echo "# HELP node_fstrim_last_run_exit_code node-fstrim's last exit code."
    echo "# TYPE node_fstrim_last_run_exit_code gauge"
    echo "node_fstrim_last_run_exit_code $1"
    echo "# HELP node_fstrim_last_run_reason Why the last run ended: done, partial, stopped, failed, terminated."
    echo "# TYPE node_fstrim_last_run_reason gauge"
    echo "node_fstrim_last_run_reason{reason=\"$2\"} 1"
    if [ -n "$last_ok" ]; then
      echo "# HELP node_fstrim_last_success_timestamp_seconds When a run last covered every mount point completely."
      echo "# TYPE node_fstrim_last_success_timestamp_seconds gauge"
      echo "node_fstrim_last_success_timestamp_seconds $last_ok"
    fi
    if [ "${#TRIMMED[@]}" -gt 0 ]; then
      echo "# HELP node_fstrim_trimmed_bytes Bytes the filesystem reported trimmed in the last run (XFS: every free extent, every run)."
      echo "# TYPE node_fstrim_trimmed_bytes gauge"
      for m in "${!TRIMMED[@]}"; do echo "node_fstrim_trimmed_bytes{mountpoint=\"$m\"} ${TRIMMED[$m]}"; done
      echo "# HELP node_fstrim_complete 1 when the last run covered the whole filesystem."
      echo "# TYPE node_fstrim_complete gauge"
      for m in "${!TRIMMED[@]}"; do echo "node_fstrim_complete{mountpoint=\"$m\"} ${COMPLETE[$m]:-0}"; done
    fi
  } > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}
finish() { # <rc> <reason> <message>
  trap - TERM INT
  local m summary=""
  for m in "${!TRIMMED[@]}"; do summary+=" $m=${TRIMMED[$m]}${COMPLETE[$m]:+(complete)}"; done
  log "$3 - exit $1 ($2):${summary:- nothing trimmed}"
  write_beacon "$1" "$2"
  [ -n "${outf:-}" ] && rm -f "$outf"
  exit "$1"
}

# fstrim and sleep run as tracked children, so a SIGTERM/SIGINT reaches the trap at once; the trap waits
# for an in-flight fstrim (it cannot be interrupted mid-call) and then exits through finish.
child=""
on_signal() {
  trap - TERM INT
  log "terminated by signal - waiting for the in-flight ${child:+pid $child }to finish"
  [ -n "$child" ] && wait "$child" 2>/dev/null
  finish "$1" terminated "stopped by signal"
}
run_child() { "$@" & child=$!; wait "$child"; local rc=$?; child=""; return "$rc"; }
trap 'on_signal 143' TERM
trap 'on_signal 130' INT
outf="$(mktemp "${TMPDIR:-/tmp}/node-fstrim.XXXXXX")" || { log "mktemp failed"; exit 1; }

psi() { awk '$1 == "some" { for (i = 2; i <= NF; i++) if ($i ~ /^avg10=[0-9]+(\.[0-9]+)?$/) { sub(/^avg10=/, "", $i); print $i; exit } }' "$PSI_FILE" 2>/dev/null; }
above() { awk -v a="$1" -v m="$PSI_MAX" 'BEGIN { exit !(a > m) }'; }
past_deadline() { [ $(( $(now_s) + CHUNK_GB * SEC_PER_GB )) -gt "$deadline" ]; }

# Every argument is checked before anything is trimmed.
declare -a MNTS=() SIZES=()
for mnt in "$@"; do
  mountpoint -q "$mnt" 2>/dev/null || { log "$mnt is not a mount point"; finish 1 failed "refusing"; }
  read -r blocks bsize < <(stat -f -c '%b %S' "$mnt" 2>/dev/null)
  [[ "${blocks:-}" =~ ^[0-9]+$ && "${bsize:-}" =~ ^[0-9]+$ ]] && [ "$blocks" -gt 0 ] \
    || { log "cannot read the size of $mnt"; finish 1 failed "refusing"; }
  MNTS+=("$mnt"); SIZES+=("$(( blocks * bsize ))")
done

reason=done
for idx in "${!MNTS[@]}"; do
  mnt="${MNTS[$idx]}"; size="${SIZES[$idx]}"
  chunks=$(( (size + chunk - 1) / chunk ))
  TRIMMED[$mnt]=0
  log "start: $mnt size=$size bytes (statfs) chunks=$chunks chunk=${CHUNK_GB}GiB minlen=$MINLEN deadline=+$(( deadline - $(now_s) ))s"
  for (( i = 0; i < chunks; i++ )); do
    [ -e "$STOP_FILE" ] && { log "stop file $STOP_FILE present - stopping before $mnt chunk $((i + 1))"; finish 0 stopped "stop file"; }
    past_deadline && { log "deadline: $mnt stopped before chunk $((i + 1))/$chunks; the next run covers the rest"; reason=partial; break 2; }
    waited=0
    while :; do
      p="$(psi)"
      [ -n "$p" ] || finish 4 failed "IO pressure unreadable from $PSI_FILE - refusing to trim ($mnt chunk $((i + 1)))"
      above "$p" || break
      [ "$waited" -lt "$PSI_WAIT_MAX_S" ] || finish 3 failed "IO pressure stayed above ${PSI_MAX}% (now $p) for ${waited}s - giving up at $mnt chunk $((i + 1))"
      run_child sleep "$PAUSE_MAX_S"; waited=$(( waited + PAUSE_MAX_S ))
      [ -e "$STOP_FILE" ] && finish 0 stopped "stop file during the pressure wait"
      past_deadline && finish 3 failed "IO pressure stayed above ${PSI_MAX}% (now $p) until the deadline - $mnt chunk $((i + 1))"
    done
    off=$(( i * chunk ))
    args=(-v -m "$MINLEN" -o "$off")
    [ $(( i + 1 )) -lt "$chunks" ] && args+=(-l "$chunk") # the last chunk runs to the real end
    t0="$(now_s)"
    if ! run_child fstrim "${args[@]}" "$mnt" > "$outf" 2>&1; then
      finish 5 failed "fstrim failed at $mnt chunk $((i + 1))/$chunks off=$off: $(cat "$outf")"
    fi
    took=$(( $(now_s) - t0 ))
    out="$(cat "$outf")"
    bytes="$(sed -nE 's/.*\(([0-9]+) bytes\) trimmed.*/\1/p' <<< "$out")"
    TRIMMED[$mnt]=$(( ${TRIMMED[$mnt]} + ${bytes:-0} ))
    rest=$took
    [ "$rest" -lt "$PAUSE_MIN_S" ] && rest=$PAUSE_MIN_S
    [ "$rest" -gt "$PAUSE_MAX_S" ] && rest=$PAUSE_MAX_S
    log "$mnt chunk=$((i + 1))/$chunks off=$off took=${took}s waited=${waited}s psi=$p rest=${rest}s ${out#*: }"
    run_child sleep "$rest"
  done
  COMPLETE[$mnt]=1
done

[ "$reason" = done ] && finish 0 done "complete"
finish 0 partial "deadline"
