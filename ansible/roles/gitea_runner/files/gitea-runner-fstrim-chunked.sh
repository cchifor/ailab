#!/usr/bin/env bash
# MANAGED BY ANSIBLE (role: gitea_runner). Nightly, chunked, paced fstrim of the runner VM's root
# filesystem, so the space the cleanup frees goes back to the host's thin pool (#1857, 2026-11-18).
#
# WHY. The runner VMs sit on 250 GB thin LVs (Proxmox local-lvm, 64 KiB chunks, discard passed down to a
# QLC NVMe that etcd shares). The cloud image mounts / with online `discard`. Deleting many small files
# then sends small, scattered discards that cannot free whole 64 KiB chunks, so the thin LV does not
# shrink. Under `discard`, ext4 also never clears a group's "trimmed" flag on a free, so FITRIM skips
# those groups and trims 0 B until the next fresh mount. The role therefore mounts / `nodiscard` (frees
# then clear the flag), and this script trims what was freed. The stock weekly fstrim.timer would trim
# all of / in one ~80-min burst on the etcd drive (TRIM costs ~19 s/GB there), so the role masks it.
#
# HOW. One mount point (MNT, default /), ext4 only. The geometry comes from `tune2fs -l` on the mount's
# source, so the slices cover every block of the filesystem, not df's smaller size. Each slice is
# CHUNK_GB rounded up to whole block groups (ext4 keeps the trimmed flag per group), trimmed with
#   fstrim -v -m 64K -o <offset> -l <length> <mnt>
# The -m is a CONSTANT on purpose: ext4 retrims every flagged group when -m is smaller than the last one.
# Before EACH slice, in this order: the stop file, the night window (UTC, may wrap midnight), the day gate
# (DAYS, by the weekday the window started), and the job gate; then the stop file, the window and the day
# once more, immediately before the FITRIM. Each slice must also END inside the window: it is cut to what
# fits before the END at SEC_PER_GB, and fstrim runs under `timeout -s KILL`, which kills a stalled one
# KILL_ALLOWANCE_S after the END. After each slice the
# byte offset (cursor) is persisted durably, so a run that stops anywhere resumes there next night; at
# the end of the filesystem it wraps to 0 and counts a completed pass.
# A run ends when the whole filesystem has been covered once, when less than one block group of the
# MAX_GB_PER_RUN budget is left (a slice is cut to the budget, in whole groups: the cap is never
# exceeded), when the window closes or less than one group fits before its end, or at the deadline.
#
# A job can still start during a slice. That is why the slices are small (2 GiB, ~40 s): a TRIM slows
# the job's disk for at most one slice, and deletes nothing.
#
# The beacon reports the bytes SUBMITTED to TRIM (what FITRIM says it trimmed). Whether the host's thin
# pool reclaimed them is not visible from the guest: check `lvs` on the Proxmox node.
#
# Exit codes:
#   0   done: the cap or a full pass reached, the window closed (or less than one group fits before its
#       end), not a night in DAYS, a job outlasted BUSY_WAIT_MAX_S, the stop file is present, or a slice
#       was killed at the window-relative deadline (reason `deadline`; the cursor stays at its start)
#   1   MNT is not a mount point, not ext4, or its geometry is unreadable; or the lock file cannot be opened
#   2   invalid settings
#   5   an fstrim failed (the cursor stays at that slice)
#   6   the state file cannot be written or synced (nothing is trimmed without a durable record of progress)
#   7   another run holds the lock (no beacon is written: that run owns it)
#   143/130  SIGTERM/SIGINT (the in-flight slice is not counted; it is redone next time)
set -uo pipefail

ENV_FILE="${GITEA_FSTRIM_ENV_FILE:-/etc/gitea-runner-fstrim.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

# Every default below that the role renders must equal its role default (defaults/main.yml ->
# templates/gitea-runner-fstrim.env.j2); tests/test-fstrim-chunked.sh checks that.
SERVICE="${GITEA_RUNNER_SERVICE:-gitea-act-runner.service}"
WINDOW_START="${GITEA_FSTRIM_WINDOW_START:-00:00}"      # HH:MM UTC; the run trims only inside the window
WINDOW_END="${GITEA_FSTRIM_WINDOW_END:-05:25}"          # exclusive; END < START wraps midnight
# Weekdays the window may run, keyed on the day the window STARTS (UTC): 1-7 = Mon-Sun, or mon..sun,
# separated by spaces or commas. A night not listed trims nothing.
DAYS="${GITEA_FSTRIM_DAYS:-mon tue wed thu fri sat sun}"
CHUNK_GB="${GITEA_FSTRIM_CHUNK_GB:-2}"                  # GiB per slice, rounded up to whole block groups
MAX_GB_PER_RUN="${GITEA_FSTRIM_MAX_GB_PER_RUN:-96}"     # GiB of the filesystem covered per run
PAUSE_S="${GITEA_FSTRIM_PAUSE_S:-20}"                   # seconds between slices
BUSY_WAIT_MAX_S="${GITEA_FSTRIM_BUSY_WAIT_MAX_S:-1800}" # give up for the night after one job wait this long
# The window-relative deadline. A slice is cut to what fits before the window END at SEC_PER_GB seconds per
# GiB (conservative: measured 17-23), and fstrim is SIGKILLed KILL_ALLOWANCE_S after the END if the drive
# stalls. The allowance (plus one group) is each window's tail: the gap the schedule test requires
# before the next window on the node.
SEC_PER_GB="${GITEA_FSTRIM_SEC_PER_GB:-30}"
KILL_ALLOWANCE_S="${GITEA_FSTRIM_KILL_ALLOWANCE_S:-60}"
TEXTFILE_DIR="${GITEA_FSTRIM_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
# Not rendered by the role: fixed paths and the cleanup's own idle-confirm cadence.
MNT="${GITEA_FSTRIM_MOUNT:-/}"
STATE_FILE="${GITEA_FSTRIM_STATE_FILE:-/var/lib/gitea-runner-fstrim/state}"
STOP_FILE="${GITEA_FSTRIM_STOP_FILE:-/var/lib/gitea-runner-fstrim/stop}"
LOCK_FILE="${GITEA_FSTRIM_LOCK_FILE:-/run/gitea-runner-fstrim.lock}"
IDLE_POLL_S="${GITEA_FSTRIM_IDLE_POLL_S:-1}"
IDLE_CONFIRM_POLLS="${GITEA_FSTRIM_IDLE_CONFIRM_POLLS:-3}"
MINLEN=64K # constant, deliberately not a setting (see the header)
GiB=1073741824

log() { echo "$(date -u +%FT%TZ) gitea-runner-fstrim[$MNT]: $*"; }
is_num() { [[ "${1:-}" =~ ^(0|[1-9][0-9]{0,14})$ ]]; } # canonical, bounded: safe for $(( ))
now_s() { date +%s; }

# Run counters, reported by every exit that writes the beacon.
submitted=0; range=0; size=0; cursor=0; passes=0

write_beacon() { # <rc> <reason>
  [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/gitea_runner_fstrim.prom" tmp
  tmp="$(mktemp "$f.XXXX" 2>/dev/null)" || return 0
  {
    echo "# HELP gitea_runner_fstrim_submitted_bytes Bytes FITRIM reported trimmed in the last run, i.e. submitted to the host. The thin pool's own reclamation is not visible from the guest."
    printf 'gitea_runner_fstrim_%s %s\n' last_run_seconds "$(now_s)" submitted_bytes "$submitted" \
      range_bytes "$range" cursor_bytes "$cursor" fs_bytes "$size" passes_total "$passes" exit_code "$1"
    printf 'gitea_runner_fstrim_exit_reason{reason="%s"} 1\n' "$2"
  } > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true # node_exporter (non-root) must read it
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}
finish() { # <rc> <reason> <message>
  trap - TERM INT
  log "$3 - exit $1 ($2): submitted=$submitted range=$range cursor=$cursor passes=$passes"
  write_beacon "$1" "$2"
  [ -n "${outf:-}" ] && rm -f "$outf"
  exit "$1"
}

# fstrim and sleep run as tracked children, so a SIGTERM/SIGINT is handled at once (bash defers a trap
# until a FOREGROUND child exits, but `wait` returns to it immediately). The trap waits for an in-flight
# FITRIM (ext4 ends one early when the signal reaches it too, as `systemctl stop` does), then exits
# through finish, so the beacon records the stop. That slice is not counted: it is redone next time.
child=""
run_child() { "$@" & child=$!; wait "$child"; local rc=$?; child=""; return "$rc"; }
on_signal() { # <rc> <signal name>
  trap - TERM INT
  [ -n "$child" ] && wait "$child" 2>/dev/null
  finish "$1" signal "stopped by $2"
}

# State: key=value lines, digits only. Parsed, never sourced; rewritten atomically AND durably.
# Surviving a power loss is about durability, not just atomicity: the rename alone is atomic, but without
# an fsync of the new file (before the rename) and of its directory (after it), an abrupt reboot can drop
# the update. That would cost real TRIM time, because a reboot also resets ext4's trimmed flags: the next
# run would redo full slices from the stale cursor. `sync FILE` / `sync DIR` (coreutils) fsync just those.
state_get() { sed -n "s/^$1=\([0-9]*\)$/\1/p" "$STATE_FILE" 2>/dev/null | tail -1; }
write_state() { # <cursor> <passes>
  local tmp dir
  dir="$(dirname "$STATE_FILE")"
  mkdir -p "$dir" 2>/dev/null || return 1
  tmp="$(mktemp "$STATE_FILE.XXXX" 2>/dev/null)" || return 1
  printf 'cursor=%s\npasses=%s\n' "$1" "$2" > "$tmp" || { rm -f "$tmp"; return 1; }
  sync "$tmp" || { rm -f "$tmp"; return 1; }
  mv -f "$tmp" "$STATE_FILE" || { rm -f "$tmp"; return 1; }
  sync "$dir" || return 1
}

stopped() { [ -e "$STOP_FILE" ]; }
hm_min() { echo $(( 10#${1%:*} * 60 + 10#${1#*:} )); }
in_window() { # now, as UTC minutes since midnight, in [START, END), wrapping midnight when END < START
  local now=$(( ($(now_s) % 86400) / 60 ))
  if [ "$ws" -lt "$we" ]; then [ "$now" -ge "$ws" ] && [ "$now" -lt "$we" ]
  else [ "$now" -ge "$ws" ] || [ "$now" -lt "$we" ]; fi
}
# The day gate. A time window alone cannot keep a runner off a slot something else holds for an unbounded
# time: ai-node1's weekly registry LXC trim starts Sunday 03:30-03:45 and can run long after (it waits out
# IO pressure per chunk), so ci-runner-9 skips Sundays. Keyed on the ISO weekday (1 = Mon) of the night the
# window STARTED, UTC: in the after-midnight part of a window that wraps, that is yesterday.
start_dow() {
  local t day
  t="$(now_s)"; day=$(( t / 86400 ))
  [ "$ws" -gt "$we" ] && [ $(( (t % 86400) / 60 )) -lt "$we" ] && day=$(( day - 1 ))
  echo $(( (day + 3) % 7 + 1 )) # 1970-01-01, day 0, was a Thursday (4)
}
day_ok() { case "$days" in *" $(start_dow) "*) return 0 ;; esac; return 1; }
# Seconds from now to the window END. Only called inside the window, so a non-positive difference means
# the END is past midnight (a window that wraps).
secs_left() {
  local left
  left=$(( we * 60 - $(now_s) % 86400 ))
  [ "$left" -gt 0 ] || left=$(( left + 86400 ))
  echo "$left"
}

# Job gate. MIRRORS any_job_running()/idle_confirmed() in gitea-runner-cleanup.sh, deliberately: the
# same signal (act_runner spawns each job's step processes as children of its MainPID, so "MainPID has
# children" means a job is running), the same rule that an unreadable read is BUSY, MainPID 0 (daemon
# down) as a definite "no job", and the same confirmation across IDLE_CONFIRM_POLLS consecutive reads,
# because the children test reads idle for a moment between a job's steps. One difference, in the safe
# direction: a pgrep error is BUSY here (the cleanup reads it as no children). The cleanup's peer-runner
# branch is not mirrored: no peer runs on these hosts since 2026-10-12 (gitea_runner_cleanup_peer_services
# is `none`), and a wrong answer here costs a slower job, not a deleted file.
job_running() {
  local mp rc
  mp="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)" || return 0
  is_num "$mp" || return 0
  [ "$mp" -eq 0 ] && return 1
  pgrep -P "$mp" >/dev/null 2>&1; rc=$?
  [ "$rc" -eq 1 ] && return 1 # 1 = definitely no children; 0 = children; anything else = unknown
  return 0
}
idle_confirmed() {
  local i=0
  while [ "$i" -lt "$IDLE_CONFIRM_POLLS" ]; do
    job_running && return 1
    i=$(( i + 1 ))
    [ "$i" -lt "$IDLE_CONFIRM_POLLS" ] && run_child sleep "$IDLE_POLL_S"
  done
  return 0
}
# Waits for a confirmed idle runner; leaves the seconds waited in $waited. Ends the run (exit 0) on the
# stop file, the window closing, or a wait past BUSY_WAIT_MAX_S.
wait_idle() {
  local t0
  t0="$(now_s)"; waited=0
  until idle_confirmed; do
    waited=$(( $(now_s) - t0 ))
    stopped && finish 0 stopped "stop file $STOP_FILE present (during a job wait)"
    in_window || finish 0 window "the window $WINDOW_START-$WINDOW_END UTC closed while a job ran"
    [ "$waited" -lt "$BUSY_WAIT_MAX_S" ] || finish 0 busy "a job kept the runner busy for ${waited}s - resuming next night"
    run_child sleep "$IDLE_POLL_S"
  done
  waited=$(( $(now_s) - t0 ))
}

# --- the persisted progress (read first, so even an early exit's beacon carries it) -------------------
c_raw="$(state_get cursor)"; cursor="$c_raw"; is_num "$cursor" || cursor=0
passes="$(state_get passes)"; is_num "$passes" || passes=0

# --- settings ------------------------------------------------------------------------------------------
posint() { [[ "$1" =~ ^[1-9][0-9]{0,5}$ ]]; }
nonneg() { [[ "$1" =~ ^(0|[1-9][0-9]{0,5})$ ]]; }
hhmm() { [[ "$1" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; }
posint "$CHUNK_GB" && [ "$CHUNK_GB" -le 64 ] || finish 2 invalid "invalid CHUNK_GB='$CHUNK_GB' (1-64)"
posint "$MAX_GB_PER_RUN" || finish 2 invalid "invalid MAX_GB_PER_RUN='$MAX_GB_PER_RUN' (a positive integer)"
[ "$MAX_GB_PER_RUN" -ge "$CHUNK_GB" ] || finish 2 invalid "MAX_GB_PER_RUN=$MAX_GB_PER_RUN is below CHUNK_GB=$CHUNK_GB"
nonneg "$PAUSE_S" || finish 2 invalid "invalid PAUSE_S='$PAUSE_S' (whole seconds, 0 or more)"
nonneg "$BUSY_WAIT_MAX_S" || finish 2 invalid "invalid BUSY_WAIT_MAX_S='$BUSY_WAIT_MAX_S' (whole seconds, 0 or more)"
posint "$SEC_PER_GB" || finish 2 invalid "invalid SEC_PER_GB='$SEC_PER_GB' (a positive integer)"
posint "$KILL_ALLOWANCE_S" || finish 2 invalid "invalid KILL_ALLOWANCE_S='$KILL_ALLOWANCE_S' (a positive integer)"
posint "$IDLE_POLL_S" || finish 2 invalid "invalid IDLE_POLL_S='$IDLE_POLL_S' (a positive integer)"
posint "$IDLE_CONFIRM_POLLS" || finish 2 invalid "invalid IDLE_CONFIRM_POLLS='$IDLE_CONFIRM_POLLS' (a positive integer)"
hhmm "$WINDOW_START" || finish 2 invalid "invalid WINDOW_START='$WINDOW_START' (HH:MM, UTC)"
hhmm "$WINDOW_END" || finish 2 invalid "invalid WINDOW_END='$WINDOW_END' (HH:MM, UTC)"
ws="$(hm_min "$WINDOW_START")"; we="$(hm_min "$WINDOW_END")"
[ "$ws" -ne "$we" ] || finish 2 invalid "WINDOW_START and WINDOW_END are both $WINDOW_START (an empty window)"
day_num() {
  case "${1,,}" in
    1 | mon) echo 1 ;; 2 | tue) echo 2 ;; 3 | wed) echo 3 ;; 4 | thu) echo 4 ;;
    5 | fri) echo 5 ;; 6 | sat) echo 6 ;; 7 | sun) echo 7 ;; *) return 1 ;;
  esac
}
days=" "
read -ra day_words <<< "${DAYS//,/ }"
for d in "${day_words[@]}"; do
  n="$(day_num "$d")" || finish 2 invalid "invalid DAYS='$DAYS' ('$d': use 1-7 for Mon-Sun, or mon..sun)"
  days="$days$n "
done
[ "$days" != " " ] || finish 2 invalid "DAYS='$DAYS' lists no day"

# --- one run at a time (a hand run must not double the TRIM load of the timer's run) ------------------
exec 9>"$LOCK_FILE" || finish 1 lock "cannot open lock file $LOCK_FILE"
flock -n 9 || { log "another run holds $LOCK_FILE - not starting a second trim"; exit 7; }
trap 'on_signal 143 SIGTERM' TERM
trap 'on_signal 130 SIGINT' INT

# --- geometry: the WHOLE filesystem, from ext4 itself ---------------------------------------------------
fstype="$(findmnt -no FSTYPE --mountpoint "$MNT" 2>/dev/null | head -1)"
[ -n "$fstype" ] || finish 1 unsupported "$MNT is not a mount point"
[ "$fstype" = ext4 ] || finish 1 unsupported "$MNT is $fstype, not ext4 - refusing (the group slicing and the trimmed-flag behaviour are ext4's)"
src="$(findmnt -no SOURCE --mountpoint "$MNT" 2>/dev/null | head -1)"
geo="$(tune2fs -l "$src" 2>/dev/null)" || finish 1 unsupported "tune2fs -l $src failed"
blocks="$(sed -nE 's/^Block count:[[:space:]]+([0-9]+)$/\1/p' <<< "$geo")"
bsize="$(sed -nE 's/^Block size:[[:space:]]+([0-9]+)$/\1/p' <<< "$geo")"
bpg="$(sed -nE 's/^Blocks per group:[[:space:]]+([0-9]+)$/\1/p' <<< "$geo")"
is_num "$blocks" && is_num "$bsize" && is_num "$bpg" && [ "$blocks" -gt 0 ] && [ "$bsize" -gt 0 ] && [ "$bpg" -gt 0 ] \
  || finish 1 unsupported "cannot read the ext4 geometry of $src (block count '$blocks', size '$bsize', per group '$bpg')"
size=$(( blocks * bsize ))
group=$(( bpg * bsize ))  # groups start at block 0 on every ext4 with blocks above 1 KiB, as here
slice=$(( (CHUNK_GB * GiB + group - 1) / group * group ))
cap=$(( MAX_GB_PER_RUN * GiB ))
[ "$cap" -ge "$group" ] || finish 2 invalid "MAX_GB_PER_RUN=$MAX_GB_PER_RUN is below one block group ($group bytes)"

# A cursor that does not fit this filesystem (a rebuilt or resized disk, a hand edit) restarts the pass.
if [ "$cursor" -ge "$size" ] || [ $(( cursor % group )) -ne 0 ] || [ "$cursor" != "${c_raw:-0}" ]; then
  log "cursor '${c_raw}' does not fit this filesystem (size $size, group $group) - restarting the pass at 0"
  cursor=0
fi
write_state "$cursor" "$passes" || finish 6 state "cannot write or sync $STATE_FILE - not trimming without a durable record of progress"
outf="$STATE_FILE.fstrim-out" # fstrim's output, per slice (it runs as a tracked child, see run_child)

log "start: $src size=$size group=$group slice=$slice cap=$cap window=$WINDOW_START-$WINDOW_END UTC cursor=$cursor passes=$passes"
while :; do
  # The slice: never past the cap (the budget left, rounded DOWN to whole groups; under one group left
  # ends the run), never past the fs end, and never back over this run's own start after a wrap
  # (size - range is then the group-aligned distance to it).
  [ "$range" -ge "$size" ] && finish 0 full "covered the whole filesystem once"
  budget=$(( (cap - range) / group * group ))
  [ "$budget" -ge "$group" ] || finish 0 cap "covered MAX_GB_PER_RUN=${MAX_GB_PER_RUN} GiB (less than one block group left)"
  off="$cursor"; len="$slice"
  [ "$len" -gt "$budget" ] && len="$budget"
  [ $(( off + len )) -gt "$size" ] && len=$(( size - off ))
  [ "$len" -gt $(( size - range )) ] && len=$(( size - range ))
  stopped && finish 0 stopped "stop file $STOP_FILE present"
  in_window || finish 0 window "outside the window $WINDOW_START-$WINDOW_END UTC"
  day_ok || finish 0 day "this window started on weekday $(start_dow), not one of DAYS='$DAYS'"
  wait_idle
  # Again, IMMEDIATELY before the FITRIM: a successful idle confirmation takes seconds, and the window
  # can close or a stop request arrive during it.
  stopped && finish 0 stopped "stop file $STOP_FILE present (appeared while confirming idle)"
  in_window || finish 0 window "the window $WINDOW_START-$WINDOW_END UTC closed while confirming idle"
  day_ok || finish 0 day "this window started on weekday $(start_dow), not one of DAYS='$DAYS'"
  # The window-relative deadline. The checks above only say the window is OPEN; a slice must also END in
  # it, or the next runner on this node starts while it runs. So: cut the slice to what fits before the
  # END at SEC_PER_GB (whole groups; less than one group ends the run), and SIGKILL the fstrim if it is
  # still running KILL_ALLOWANCE_S after the END (a stalled drive). ext4 checks for a fatal signal while it
  # walks the groups, so a KILL ends the FITRIM within about one group's discard.
  left="$(secs_left)"
  fit=$(( left * GiB / SEC_PER_GB / group * group ))
  [ "$fit" -ge "$group" ] || finish 0 window "${left}s to the window end: less than one block group fits at ${SEC_PER_GB}s/GiB"
  [ "$len" -gt "$fit" ] && len="$fit"
  run_child timeout -s KILL $(( left + KILL_ALLOWANCE_S )) fstrim -v -m "$MINLEN" -o "$off" -l "$len" "$MNT" > "$outf" 2>&1
  rc=$?
  # 137: the KILL (timeout re-raises it on itself); 124: timed out, as timeout reports it otherwise. A
  # killed slice is UNCONFIRMED: the cursor stays at its start, so the next night trims it again.
  case "$rc" in
    0) : ;;
    124 | 137) finish 0 deadline "fstrim at off=$off len=$len was still running $(( left + KILL_ALLOWANCE_S ))s later, ${KILL_ALLOWANCE_S}s past the window end - killed; the cursor stays at $off" ;;
    *) finish 5 fstrim_failed "fstrim failed at off=$off len=$len: $(cat "$outf" 2>/dev/null)" ;;
  esac
  out="$(cat "$outf" 2>/dev/null)"
  bytes="$(sed -nE 's/.*\(([0-9]+) bytes\) trimmed.*/\1/p' <<< "$out")"
  is_num "$bytes" || { log "could not read the trimmed bytes from: $out"; bytes=0; }
  submitted=$(( submitted + bytes )); range=$(( range + len ))
  next=$(( off + len )); np="$passes"
  if [ "$next" -ge "$size" ]; then next=0; np=$(( passes + 1 )); fi
  write_state "$next" "$np" || finish 6 state "cannot write or sync $STATE_FILE after the slice at $off"
  cursor="$next"; passes="$np"
  log "slice off=$off len=$len waited=${waited}s trimmed=$bytes next=$cursor passes=$passes"
  [ "$range" -lt "$size" ] && [ $(( (cap - range) / group )) -ge 1 ] && run_child sleep "$PAUSE_S"
done
