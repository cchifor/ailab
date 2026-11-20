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
# HOW. Per mount point: the filesystem's size from statfs, then chunks of CHUNK_GB:
#   fstrim -v -m <MINLEN> -o <offset> -l <CHUNK> <mnt>
# The LAST chunk (by offset) has no -l: it runs to the filesystem's real end. statfs understates the FITRIM
# address space (XFS leaves out its internal log, ext4 its metadata), and an offset past the end is EINVAL,
# so the tail is covered without knowing its size. -m 64K by default: the thin pool's chunk is 64 KiB, and a
# smaller discard cannot free one.
# Before each chunk: the stop file, the deadline (DEADLINE = start + RUN_MAX_S; no chunk starts unless it can
# end by then at SEC_PER_GB), and IO pressure (`some avg10` in PSI_FILE above PSI_MAX: wait, PAUSE_MAX_S at a
# time, up to PSI_WAIT_MAX_S and never past the deadline; unreadable pressure refuses to trim). So the
# pressure wait counts against RUN_MAX_S: a run ends by RUN_MAX_S plus at most one chunk in flight. After each
# chunk it rests as long as the chunk took (between PAUSE_MIN_S and PAUSE_MAX_S), except after the very last
# chunk of the run: a chunk the thin pool had nothing to drop for returns at once and costs a 2 s rest, a heavy
# one is followed by as long a break. A chunk already running is never cut short: the caller allows for one
# (the pod's terminationGracePeriodSeconds, a unit's TimeoutStopSec).
#
# PROGRESS ACROSS RUNS (STATE_DIR; empty = stateless, every run starts at the first chunk). XFS has no memory
# of what it trimmed, so a run that the deadline cuts every night would otherwise trim the same prefix every
# night and never reach the tail. With STATE_DIR, per mount point (<key> = the path with / as _):
#   <key>.cursor    the chunk to start at, with the geometry it belongs to; written whenever a run stops before
#                   the end of the mount point (deadline, stop file, signal, failure). A pass runs from the first
#                   chunk to the last, across as many runs as it takes: the next run continues at the cursor, so
#                   every chunk is reached. A cursor for another size or chunk size starts a new pass.
#   <key>.complete  when a pass last reached the last chunk (the cursor is then removed; the next pass starts
#                   at the first chunk, in the next run)
#   <key>.since     when it was first seen, until its first complete pass
# Mount points are taken least recently completed first, so a second mount point is not starved by a first
# one that keeps running out of time. A partial run exits 9 instead of 0 once a mount point has had no
# complete pass for COMPLETE_MAX_AGE_S (7 days by default): progress alone is not success forever.
#
# XFS discards every free extent again on every run. The thin pool drops what is already unmapped quickly, so
# only what was freed since the last run costs the NVMe anything; the bytes fstrim reports are the free space
# it walked, not what the pool reclaimed (that is visible only on the host).
#
# Settings (environment): CHUNK_GB 4, MINLEN 64K, PAUSE_MIN_S 2, PAUSE_MAX_S 30, PSI_MAX 45, PSI_WAIT_MAX_S
# 240, PSI_FILE /proc/pressure/io, RUN_MAX_S 360, SEC_PER_GB 30, STOP_FILE /run/node-fstrim.stop, LOCK_FILE
# /run/node-fstrim.lock, STATE_DIR (empty), COMPLETE_MAX_AGE_S 604800, TEXTFILE_DIR (empty: no beacon; else
# node_fstrim.prom there for node_exporter).
#
# Exit codes:
#   0   done, or stopped early by the deadline (a partial run: the next run continues at the cursor; logged
#       `partial`) or the stop file
#   1   an argument is not a mount point, or its size is unreadable; the lock file cannot be opened; or
#       STATE_DIR is set but not a writable directory
#   2   usage or invalid settings
#   3   IO pressure stayed above PSI_MAX for PSI_WAIT_MAX_S, or until the deadline, before a chunk
#   4   IO pressure unreadable
#   5   an fstrim failed
#   6   a write to STATE_DIR failed (full, read-only, ...): the run's progress could not be recorded, so it is a
#       failure even if the trim went well (a silent restart at chunk 1 every night is what STATE_DIR prevents)
#   7   another run holds LOCK_FILE
#   9   a partial run, and a mount point has had no complete pass for over COMPLETE_MAX_AGE_S (with STATE_DIR)
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
STATE_DIR="${STATE_DIR:-}"
COMPLETE_MAX_AGE_S="${COMPLETE_MAX_AGE_S:-604800}"
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
posint "$COMPLETE_MAX_AGE_S" || bad="COMPLETE_MAX_AGE_S='$COMPLETE_MAX_AGE_S'"
[[ "$PSI_MAX" =~ ^[0-9]+(\.[0-9]+)?$ ]] && awk -v m="$PSI_MAX" 'BEGIN { exit !(m >= 0 && m <= 100) }' || bad="PSI_MAX='$PSI_MAX' (0-100)"
[ -z "$bad" ] || { log "invalid setting $bad"; exit 2; }

exec 9>"$LOCK_FILE" || { log "cannot open lock file $LOCK_FILE"; exit 1; }
flock -n 9 || { log "another run holds $LOCK_FILE - not starting a second, overlapping trim"; exit 7; }
if [ -n "$STATE_DIR" ] && ! { [ -d "$STATE_DIR" ] && [ -w "$STATE_DIR" ]; }; then
  log "STATE_DIR $STATE_DIR is not a writable directory - refusing to run without progress tracking"; exit 1
fi

start="$(now_s)"
deadline=$(( start + RUN_MAX_S ))
chunk=$(( CHUNK_GB * GiB ))
declare -A TRIMMED=() COMPLETE=()

# ---- the state (STATE_DIR) ------------------------------------------------------------------------
key_of() { local k="${1//\//_}"; printf '%s' "${k:-_}"; }
state_get() { [ -n "$STATE_DIR" ] && [ -r "$STATE_DIR/$1" ] && head -n 1 "$STATE_DIR/$1" 2>/dev/null | head -c 200; }
# A failed write sets state_err: the run then ends with exit 6 rather than 0 (finish), so a STATE_DIR that cannot
# take the cursor or the completion time never turns into a silent restart at chunk 1 every night.
state_err=0
state_put() { # <file> <line>: one rename; returns 1 (and sets state_err) when it could not be written
  [ -n "$STATE_DIR" ] || return 0
  local tmp
  tmp="$(mktemp "$STATE_DIR/.$1.XXXXXX" 2>/dev/null)" || { log "cannot write $STATE_DIR/$1"; state_err=1; return 1; }
  if printf '%s\n' "$2" > "$tmp" && mv -f "$tmp" "$STATE_DIR/$1"; then return 0; fi
  rm -f "$tmp"; log "cannot write $STATE_DIR/$1"; state_err=1; return 1
}
state_rm() { [ -n "$STATE_DIR" ] && rm -f "$STATE_DIR/$1"; return 0; }
# The cursor to write if the run stops now: set before each chunk, moved on after it, cleared when the mount
# point is complete. finish() writes it, so every way out (deadline, stop, signal, failure) records progress.
pending_key=""; pending_line=""

write_beacon() { # <rc> <reason>
  [ -n "$TEXTFILE_DIR" ] && [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/node_fstrim.prom" tmp end last_ok m t
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
    echo "# HELP node_fstrim_last_run_reason Why the last run ended: done, partial, stale, stopped, failed, terminated."
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
    if [ -n "$STATE_DIR" ]; then
      echo "# HELP node_fstrim_last_complete_timestamp_seconds When a run last covered every chunk of this mount point (STATE_DIR)."
      echo "# TYPE node_fstrim_last_complete_timestamp_seconds gauge"
      for m in "${MNTS[@]}"; do
        t="$(state_get "$(key_of "$m").complete")"
        [[ "$t" =~ ^[0-9]+$ ]] && echo "node_fstrim_last_complete_timestamp_seconds{mountpoint=\"$m\"} $t"
      done
    fi
  } > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}
finish() { # <rc> <reason> <message>
  trap - TERM INT
  local m summary=""
  [ -n "$pending_key" ] && state_put "$pending_key.cursor" "$pending_line"
  if [ "$state_err" = 1 ] && [ "$1" -eq 0 ]; then
    set -- 6 failed "$3; but a write to $STATE_DIR failed, so the progress was not recorded"
  fi
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
declare -a MNTS=() SIZES=() ORDER=()
trap 'on_signal 143' TERM
trap 'on_signal 130' INT
outf="$(mktemp "${TMPDIR:-/tmp}/node-fstrim.XXXXXX")" || { log "mktemp failed"; exit 1; }

psi() { awk '$1 == "some" { for (i = 2; i <= NF; i++) if ($i ~ /^avg10=[0-9]+(\.[0-9]+)?$/) { sub(/^avg10=/, "", $i); print $i; exit } }' "$PSI_FILE" 2>/dev/null; }
above() { awk -v a="$1" -v m="$PSI_MAX" 'BEGIN { exit !(a > m) }'; }
past_deadline() { [ $(( $(now_s) + CHUNK_GB * SEC_PER_GB )) -gt "$deadline" ]; }

# Every argument is checked before anything is trimmed.
for mnt in "$@"; do
  mountpoint -q "$mnt" 2>/dev/null || { log "$mnt is not a mount point"; finish 1 failed "refusing"; }
  read -r blocks bsize < <(stat -f -c '%b %S' "$mnt" 2>/dev/null)
  [[ "${blocks:-}" =~ ^[0-9]+$ && "${bsize:-}" =~ ^[0-9]+$ ]] && [ "$blocks" -gt 0 ] \
    || { log "cannot read the size of $mnt"; finish 1 failed "refusing"; }
  MNTS+=("$mnt"); SIZES+=("$(( blocks * bsize ))")
done

# Least recently completed first (never completed = 0); ties keep the argument order.
while read -r _ idx; do ORDER+=("$idx"); done < <(
  for idx in "${!MNTS[@]}"; do
    t="$(state_get "$(key_of "${MNTS[$idx]}").complete")"
    [[ "$t" =~ ^[0-9]+$ ]] || t=0
    echo "$t $idx"
  done | sort -n -k1,1 -k2,2
)

reason=done
for pos in "${!ORDER[@]}"; do
  idx="${ORDER[$pos]}"; mnt="${MNTS[$idx]}"; size="${SIZES[$idx]}"; key="$(key_of "$mnt")"
  chunks=$(( (size + chunk - 1) / chunk ))
  TRIMMED[$mnt]=0
  if [ -n "$STATE_DIR" ] && [ -z "$(state_get "$key.complete")" ] && [ -z "$(state_get "$key.since")" ]; then
    state_put "$key.since" "$(now_s)"
  fi
  s=0
  cur="$(state_get "$key.cursor")"
  if [[ "$cur" =~ ^next=([0-9]+)\ chunks=([0-9]+)\ size=([0-9]+)\ chunk=([0-9]+)$ ]] \
    && [ "${BASH_REMATCH[2]}" = "$chunks" ] && [ "${BASH_REMATCH[3]}" = "$size" ] && [ "${BASH_REMATCH[4]}" = "$chunk" ] \
    && [ "${BASH_REMATCH[1]}" -lt "$chunks" ]; then
    s="${BASH_REMATCH[1]}"
  elif [ -n "$cur" ]; then
    log "the cursor for $mnt ($cur) does not fit this filesystem - starting a new pass at chunk 1"
  fi
  log "start: $mnt size=$size bytes (statfs) chunks=$chunks chunk=${CHUNK_GB}GiB minlen=$MINLEN from chunk $((s + 1)) deadline=+$(( deadline - $(now_s) ))s"
  for (( i = s; i < chunks; i++ )); do
    pending_key="$key"; pending_line="next=$i chunks=$chunks size=$size chunk=$chunk"
    [ -e "$STOP_FILE" ] && { log "stop file $STOP_FILE present - stopping before $mnt chunk $((i + 1))"; finish 0 stopped "stop file"; }
    past_deadline && { log "deadline: $mnt stopped before chunk $((i + 1))/$chunks; the next run starts there"; reason=partial; break 2; }
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
    [ $(( i + 1 )) -lt "$chunks" ] && args+=(-l "$chunk") # the last chunk by offset runs to the real end
    t0="$(now_s)"
    if ! run_child fstrim "${args[@]}" "$mnt" > "$outf" 2>&1; then
      finish 5 failed "fstrim failed at $mnt chunk $((i + 1))/$chunks off=$off: $(cat "$outf")"
    fi
    took=$(( $(now_s) - t0 ))
    out="$(cat "$outf")"
    bytes="$(sed -nE 's/.*\(([0-9]+) bytes\) trimmed.*/\1/p' <<< "$out")"
    TRIMMED[$mnt]=$(( ${TRIMMED[$mnt]} + ${bytes:-0} ))
    if [ $(( i + 1 )) -lt "$chunks" ]; then
      pending_line="next=$(( i + 1 )) chunks=$chunks size=$size chunk=$chunk"
    else # the pass reached the last chunk: record it before anything else can stop the run
      COMPLETE[$mnt]=1; pending_key=""
      # The cursor goes only once the completion time is safely written; if it is not, the old cursor stays and
      # the run fails (state_err), rather than lose both.
      if state_put "$key.complete" "$(now_s)"; then state_rm "$key.cursor"; state_rm "$key.since"; fi
    fi
    rest=$took
    [ "$rest" -lt "$PAUSE_MIN_S" ] && rest=$PAUSE_MIN_S
    [ "$rest" -gt "$PAUSE_MAX_S" ] && rest=$PAUSE_MAX_S
    log "$mnt chunk=$((i + 1))/$chunks off=$off took=${took}s waited=${waited}s psi=$p rest=${rest}s ${out#*: }"
    # No rest after the very last chunk of the run: it protects nothing, and a stop landing in it would turn a
    # complete run into a terminated one.
    if [ $(( i + 1 )) -lt "$chunks" ] || [ $(( pos + 1 )) -lt "${#ORDER[@]}" ]; then run_child sleep "$rest"; fi
  done
done

[ "$reason" = done ] && finish 0 done "complete"
if [ -n "$STATE_DIR" ]; then
  stale=""
  for m in "${MNTS[@]}"; do
    t="$(state_get "$(key_of "$m").complete")"
    [[ "$t" =~ ^[0-9]+$ ]] || t="$(state_get "$(key_of "$m").since")"
    [[ "$t" =~ ^[0-9]+$ ]] && [ $(( $(now_s) - t )) -gt "$COMPLETE_MAX_AGE_S" ] && stale+=" $m"
  done
  [ -n "$stale" ] && finish 9 stale "deadline, and no complete pass of$stale for over ${COMPLETE_MAX_AGE_S}s"
fi
finish 0 partial "deadline"
