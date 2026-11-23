#!/usr/bin/env bash
# Tests for files/gitea-runner-fstrim-chunked.sh, the nightly chunked trim of a runner's root fs (#1858),
# plus the role pieces it depends on: the env template's defaults, the fstab regexes, the per-node windows.
#
# The script runs for real with mocked findmnt, tune2fs, fstrim, systemctl, pgrep, sleep and date on PATH.
# `date +%s` is a FAKE CLOCK ($CTL/clock): sleep and fstrim advance it, so the window and the job waits are
# deterministic and take no real time. The invariants:
# - every block of the fs is covered, from ext4's geometry, in slices of whole block groups, with -m 64K;
# - progress persists per slice (fsynced: file, then directory), resumes, wraps at the end and counts passes;
# - nothing is trimmed outside the window (also one that wraps midnight, and also when the idle confirmation
#   ends past it), on a night not in DAYS (by the weekday the window started), beyond the cap (slices are
#   cut to it), or while a job runs (the cleanup's act_runner signal), and a long job ends the night's run;
# - each slice is sized to end in the window; a stalled one is SIGKILLed after the end (a timer of REAL
#   seconds) and only a confirmed reap counts: reaped -> `deadline`, still alive after the grace (emulated
#   through the stubbable liveness check) -> `stuck`, exit 8; neither advances the cursor, and
#   gitea_runner_fstrim_in_flight is 1 for each slice and 0 only after the reap;
# - the lock, the stop file (also one that appears during the idle confirmation), bad settings and a
#   non-ext4 fs stop it before any trim;
# - the beacon carries the run's numbers and why it ended;
# - the per-host schedule: windows end by 05:25, never overlap on a node, fit the cap, and none on the node of a
#   hand-installed registry trim overlaps that trim's deadline-bound span (Sunday into Monday included). The
#   runbook has no such unit since CT 5004's decommission (2026-11-22); fixtures/registry-weekly-unit.md keeps
#   5004's unit, and edited copies of it prove each kind of collision is caught.
#
# Run: bash ansible/roles/gitea_runner/tests/test-fstrim-chunked.sh   (Linux; needs python3 with PyYAML and
# Jinja2, both of which cloud-init pulls in on the runners)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../files/gitea-runner-fstrim-chunked.sh"
ROLE="$HERE/.."
REPO="$(cd "$HERE/../../../.." && pwd)"
[ -f "$SCRIPT" ] || { echo "cannot find $SCRIPT"; exit 1; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
has()  { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2', got: $3" ;; esac; }
calls(){ [ -f "$CTL/$1" ] && wc -l < "$CTL/$1" | tr -d ' ' || echo 0; }
nth()  { sed -n "${2}p" "$CTL/$1"; }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"; mkdir -p "$BIN"
export REAL_DATE REAL_SLEEP
REAL_DATE="$(command -v date)"; REAL_SLEEP="$(command -v sleep)"

# --- stubs (all read their behaviour from $CTL) ---------------------------------------------------
cat > "$BIN/findmnt" <<'STUB'
#!/usr/bin/env bash
# findmnt -no FSTYPE|SOURCE --mountpoint <mnt>
[ -f "$CTL/nomount" ] && exit 1
case "$*" in
  *FSTYPE*) cat "$CTL/fstype" ;;
  *SOURCE*) echo /dev/sda1 ;;
esac
STUB
cat > "$BIN/tune2fs" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CTL/tune2fs_calls"
[ -f "$CTL/tune2fs_fail" ] && { echo "tune2fs: Bad magic number in super-block" >&2; exit 1; }
printf 'Filesystem volume name:   cloudimg-rootfs\nBlock count:              %s\nBlock size:               4096\nBlocks per group:         %s\nFirst block:              0\n' \
  "$(cat "$CTL/blocks")" "$(cat "$CTL/bpg")"
STUB
cat > "$BIN/fstrim" <<'STUB'
#!/usr/bin/env bash
# fstrim -v -m 64K -o <off> -l <len> <mnt>. Each call takes $CTL/trim_s seconds of the fake clock. Fails from
# call $CTL/fail_on on. slow_trim: an FITRIM that ignores SIGTERM and takes 2 real seconds.
echo "$*" >> "$CTL/fstrim_calls"
n=$(wc -l < "$CTL/fstrim_calls")
echo $(( $(cat "$CTL/clock") + $(cat "$CTL/trim_s") )) > "$CTL/clock"
if [ -f "$CTL/slow_trim" ]; then trap '' TERM; touch "$CTL/fstrim_running"; "$REAL_SLEEP" 2; fi
# What the in-flight marker said while this slice ran (it must be 1).
cat "$CTL/textfile/gitea_runner_fstrim_inflight.prom" 2>/dev/null | awk '{ print $2 }' >> "$CTL/inflight_seen"
# stuck: a FITRIM that outlives SIGKILL (uninterruptible in the kernel). No userspace process can, so it
# is emulated through the script's liveness seam: a fake /proc/<this pid>/stat in state D, which the
# script reads (GITEA_FSTRIM_PROC_DIR) and which stays after the real process is killed.
if [ -f "$CTL/stuck" ]; then mkdir -p "$CTL/proc/$$"; echo "$$ (fstrim) D 1 1 1" > "$CTL/proc/$$/stat"; fi
# hang: a stalled drive. The FITRIM never returns on its own (60 real seconds); only a KILL ends it.
{ [ -f "$CTL/hang" ] || [ -f "$CTL/stuck" ]; } && exec "$REAL_SLEEP" 60
mnt="${*: -1}"
if [ -f "$CTL/fail_on" ] && [ "$n" -ge "$(cat "$CTL/fail_on")" ]; then echo "fstrim: $mnt: FITRIM ioctl failed: Input/output error" >&2; exit 1; fi
echo "$mnt: 1 GiB (1073741824 bytes) trimmed"
STUB
cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
# systemctl show -p MainPID --value <unit>
echo "$*" >> "$CTL/systemctl_calls"
[ -f "$CTL/systemctl_fail" ] && exit 1
cat "$CTL/mainpid"
STUB
cat > "$BIN/pgrep" <<'STUB'
#!/usr/bin/env bash
# pgrep -P <pid>: read n answers line n of $CTL/pgrep_seq (busy|idle), then $CTL/pgrep_after. Like the real
# one: busy = a pid and rc 0, idle = rc 1. $CTL/pgrep_rc forces an error code.
n=$(( $(cat "$CTL/pgrep_n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CTL/pgrep_n"
[ -f "$CTL/pgrep_rc" ] && exit "$(cat "$CTL/pgrep_rc")"
a="$(sed -n "${n}p" "$CTL/pgrep_seq" 2>/dev/null)"; [ -n "$a" ] || a="$(cat "$CTL/pgrep_after")"
[ "$a" = busy ] && { echo 4243; exit 0; }
exit 1
STUB
cat > "$BIN/sleep" <<'STUB'
#!/usr/bin/env bash
# Advances the fake clock. From sleep number $CTL/stop_after on, a stop request appears.
n=$(( $(cat "$CTL/sleeps" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CTL/sleeps"
echo $(( $(cat "$CTL/clock") + ${1%%.*} )) > "$CTL/clock"
if [ -f "$CTL/stop_after" ] && [ "$n" -ge "$(cat "$CTL/stop_after")" ]; then touch "$CTL/stop"; fi
exit 0
STUB
cat > "$BIN/sync" <<'STUB'
#!/usr/bin/env bash
# sync FILE|DIR (an fsync of just that): logged, with what the state file held at that moment, so a case
# can tell a sync of the temp file BEFORE the rename from one of the directory after it. Fails (an fsync
# error) from call $CTL/sync_fail_on on.
echo "$*" >> "$CTL/sync_calls"
n=$(wc -l < "$CTL/sync_calls")
if [ -f "$CTL/sync_fail_on" ] && [ "$n" -ge "$(cat "$CTL/sync_fail_on")" ]; then exit 1; fi
echo "$(tr '\n' ' ' < "$CTL/state/state" 2>/dev/null)" >> "$CTL/sync_seen"
exit 0
STUB
cat > "$BIN/date" <<'STUB'
#!/usr/bin/env bash
# The fake clock for `date +%s`; everything else (the log timestamps) is the real date.
[ "$*" = "+%s" ] && { cat "$CTL/clock"; exit 0; }
exec "$REAL_DATE" "$@"
STUB
chmod +x "$BIN"/*

GiB=1073741824
SIZE=$(( 9 * GiB + 64 * 1048576 ))   # 9 GiB + 64 MiB of ext4 blocks: the last 2 GiB slice is short
# Set the fake clock (UTC): at <HH:MM:SS> [<date>]. The default date, 2026-11-18, is a Wednesday;
# 2026-11-20 is a Friday, -21 a Saturday, -22 a Sunday, -23 a Monday.
at() { "$REAL_DATE" -u -d "${2:-2026-11-18} $1" +%s > "$CTL/clock"; }
new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL/state" "$CTL/textfile"
  echo ext4 > "$CTL/fstype"
  echo $(( SIZE / 4096 )) > "$CTL/blocks"
  echo 32768 > "$CTL/bpg"          # 128 MiB block groups
  echo 40 > "$CTL/trim_s"
  echo 4242 > "$CTL/mainpid"
  echo idle > "$CTL/pgrep_after"
  at 00:10:00
}
run() { # extra VAR=value settings may be passed as arguments
  env PATH="$BIN:$PATH" GITEA_FSTRIM_ENV_FILE=/nonexistent \
    GITEA_FSTRIM_STATE_FILE="$CTL/state/state" GITEA_FSTRIM_STOP_FILE="$CTL/stop" GITEA_FSTRIM_LOCK_FILE="$CTL/lock" \
    GITEA_FSTRIM_TEXTFILE_DIR="$CTL/textfile" GITEA_FSTRIM_WINDOW_START=00:00 GITEA_FSTRIM_WINDOW_END=05:30 \
    GITEA_FSTRIM_CHUNK_GB=2 GITEA_FSTRIM_MAX_GB_PER_RUN=100 GITEA_FSTRIM_PAUSE_S=20 GITEA_FSTRIM_BUSY_WAIT_MAX_S=60 \
    "$@" bash "$SCRIPT" > "$CTL/out" 2>&1
  echo $? > "$CTL/rc"
}
rc()     { cat "$CTL/rc"; }
st()     { sed -n "s/^$1=//p" "$CTL/state/state" 2>/dev/null; }
beacon() { sed -nE "s/^gitea_runner_fstrim_$1 ([0-9]+)$/\1/p" "$CTL/textfile/gitea_runner_fstrim.prom" 2>/dev/null; }
reason() { sed -nE 's/^gitea_runner_fstrim_exit_reason\{reason="([a-z_]+)"\} 1$/\1/p' "$CTL/textfile/gitea_runner_fstrim.prom" 2>/dev/null; }

echo "== the whole filesystem from ext4's geometry, in group-aligned slices, always -m 64K =="
new_case cover; run
eq "exit 0" 0 "$(rc)"
eq "five slices cover 9 GiB + 64 MiB at 2 GiB" 5 "$(calls fstrim_calls)"
eq "first slice" "-v -m 64K -o 0 -l $((2 * GiB)) /" "$(nth fstrim_calls 1)"
eq "second slice starts where the first ended" "-v -m 64K -o $((2 * GiB)) -l $((2 * GiB)) /" "$(nth fstrim_calls 2)"
eq "the last slice stops at the fs end from tune2fs, not df" "-v -m 64K -o $((8 * GiB)) -l $((SIZE - 8 * GiB)) /" "$(nth fstrim_calls 5)"
eq "every slice uses the same -m 64K" 5 "$(grep -c -- '-m 64K ' "$CTL/fstrim_calls")"
eq "geometry read from the mount's source" "-l /dev/sda1" "$(nth tune2fs_calls 1)"
eq "a full pass is counted" 1 "$(st passes)"
eq "and the cursor wraps to 0" 0 "$(st cursor)"
eq "ends on the full pass" full "$(reason)"

echo "== a chunk that is not a whole number of block groups is rounded UP to whole groups =="
new_case groups; echo 24576 > "$CTL/bpg"; echo $(( 10 * GiB / 4096 )) > "$CTL/blocks"; run   # 96 MiB groups, 10 GiB
G=$(( 96 * 1048576 )); S=$(( 22 * G ))   # 2 GiB = 21.3 groups -> 22 groups
eq "exit 0" 0 "$(rc)"
eq "second slice at 22 groups" "-v -m 64K -o $S -l $S /" "$(nth fstrim_calls 2)"
misaligned="$(awk -v g="$G" '{ for (i = 1; i < NF; i++) if ($i == "-o" && $(i + 1) % g != 0) print }' "$CTL/fstrim_calls")"
eq "every offset is a multiple of the group size" "" "$misaligned"
eq "five slices for 10 GiB" 5 "$(calls fstrim_calls)"

echo "== resumes at the cursor, stops at the cap, wraps and counts the pass =="
new_case resume; printf 'cursor=%s\npasses=2\n' $((4 * GiB)) > "$CTL/state/state"; run GITEA_FSTRIM_MAX_GB_PER_RUN=4
eq "exit 0" 0 "$(rc)"
eq "two slices of 2 GiB under a 4 GiB cap" 2 "$(calls fstrim_calls)"
has "starts at the saved cursor" "-o $((4 * GiB)) " "$(nth fstrim_calls 1)"
eq "ends on the cap" cap "$(reason)"
eq "cursor saved after the last slice" $((8 * GiB)) "$(st cursor)"
eq "no pass yet" 2 "$(st passes)"
rm -f "$CTL/fstrim_calls"; run GITEA_FSTRIM_MAX_GB_PER_RUN=4
# The fs ends 64 MiB into a group: the short last slice (1 GiB + 64 MiB), the wrap, a 2 GiB slice, then
# the 960 MiB of budget left rounded DOWN to 7 groups (896 MiB). 64 MiB is left: under one group, so it stops.
MiB=1048576; G=$(( 128 * MiB ))
eq "next run: the short last slice, the wrap, then slices cut to the budget left" \
  "$((8 * GiB))/$((SIZE - 8 * GiB)) 0/$((2 * GiB)) $((2 * GiB))/$((7 * G))" \
  "$(awk '{ print $5 "/" $7 }' "$CTL/fstrim_calls" | tr '\n' ' ' | sed 's/ $//')"
eq "pass counted at the wrap" 3 "$(st passes)"
eq "cursor after the wrap: group-aligned" $((2 * GiB + 7 * G)) "$(st cursor)"
eq "beacon: passes" 3 "$(beacon passes_total)"
eq "beacon: cursor" $((2 * GiB + 7 * G)) "$(beacon cursor_bytes)"
eq "beacon: range covered this run is under the 4 GiB cap" $(( SIZE - 8 * GiB + 2 * GiB + 7 * G )) "$(beacon range_bytes)"
eq "ends on the cap" cap "$(reason)"

echo "== a run that starts mid-filesystem covers it exactly once, stopping at its own start =="
new_case once; printf 'cursor=%s\npasses=0\n' $((3 * GiB)) > "$CTL/state/state"; run
eq "the last slice ends at the run's start (3 GiB), not a 2 GiB slice past it" \
  "-v -m 64K -o $((2 * GiB)) -l $((1 * GiB)) /" "$(tail -1 "$CTL/fstrim_calls")"
eq "the whole fs, once" "$SIZE" "$(beacon range_bytes)"
eq "cursor back at the start" $((3 * GiB)) "$(st cursor)"
eq "reason" full "$(reason)"

echo "== the cap is never exceeded, whatever the chunk, cap and group size =="
# <blocks per group> <CHUNK_GB> <MAX_GB_PER_RUN>, on a 20 GiB fs: 128 MiB and 96 MiB groups, chunks that do
# and do not divide the cap, and a chunk that is not a whole number of groups.
for combo in "32768 3 4" "32768 4 6" "24576 2 5" "24576 3 7" "24576 5 5"; do
  set -- $combo
  new_case "cap$1-$2-$3"; echo "$1" > "$CTL/bpg"; echo $(( 20 * GiB / 4096 )) > "$CTL/blocks"
  run GITEA_FSTRIM_CHUNK_GB="$2" GITEA_FSTRIM_MAX_GB_PER_RUN="$3"
  grp=$(( $1 * 4096 )); cap=$(( $3 * GiB )); r="$(beacon range_bytes)"
  if [ -n "$r" ] && [ "$r" -le "$cap" ] && [ "$r" -gt $(( cap - grp )) ]; then
    ok "groups $(( grp / MiB )) MiB, chunk $2, cap $3: covered $r <= cap $cap (within one group of it)"
  else
    bad "groups $(( grp / MiB )) MiB, chunk $2, cap $3: covered '$r' against cap $cap" "$(cat "$CTL/fstrim_calls")"
  fi
  odd="$(awk -v g="$grp" '{ if ($5 % g || $7 % g) print }' "$CTL/fstrim_calls")"
  eq "groups $(( grp / MiB )) MiB, chunk $2, cap $3: every offset and length is whole groups" "" "$odd"
  eq "groups $(( grp / MiB )) MiB, chunk $2, cap $3: reason" cap "$(reason)"
done

echo "== a cursor that does not fit this filesystem restarts the pass at 0 =="
for c in 12345 $((20 * GiB)); do
  new_case "badcursor$c"; printf 'cursor=%s\npasses=1\n' "$c" > "$CTL/state/state"; run GITEA_FSTRIM_MAX_GB_PER_RUN=2
  has "cursor $c -> starts at 0" "-o 0 " "$(nth fstrim_calls 1)"
  has "cursor $c -> says so" "restarting the pass" "$(cat "$CTL/out")"
  eq "cursor $c -> passes kept" 1 "$(st passes)"
done

echo "== the window =="
new_case outside; at 12:00:00; run
eq "outside: exit 0" 0 "$(rc)"
eq "outside: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "outside: reason" window "$(reason)"
for t in 23:45:00 00:30:00; do
  new_case "wrap$t"; at "$t"; run GITEA_FSTRIM_WINDOW_START=23:30 GITEA_FSTRIM_WINDOW_END=01:00
  eq "23:30-01:00 at $t: trims (inside a window that wraps midnight)" 5 "$(calls fstrim_calls)"
done
for t in 01:00:00 23:29:00 12:00:00; do
  new_case "wrapout$t"; at "$t"; run GITEA_FSTRIM_WINDOW_START=23:30 GITEA_FSTRIM_WINDOW_END=01:00
  eq "23:30-01:00 at $t: nothing (outside; END is exclusive)" 0 "$(calls fstrim_calls)"
done
# 05:27:00, 40 s per slice + 2 s of idle confirmation + 20 s pause: slices start at 05:27:02, 05:28:04 and
# 05:29:06. The third has 54 s to the end: at 30 s/GiB that is 1.8 GiB, cut to 14 whole groups (1792 MiB).
new_case closes; at 05:27:00; run
eq "closes mid-run: exit 0" 0 "$(rc)"
eq "closes mid-run: the slices begun inside the window ran" 3 "$(calls fstrim_calls)"
eq "closes mid-run: the last one cut to what fits before the end" "$((4 * GiB)) $((1792 * 1048576))" \
  "$(nth fstrim_calls 3 | awk '{ print $5, $7 }')"
eq "closes mid-run: reason" window "$(reason)"
eq "closes mid-run: progress saved for tomorrow" $((4 * GiB + 1792 * 1048576)) "$(st cursor)"
# Inside at 05:29:59; the idle confirmation (3 reads, 2 s) succeeds at 05:30:01, past END: no FITRIM.
new_case confirmpast; at 05:29:59; run
eq "confirmation ends past END: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "confirmation ends past END: reason" window "$(reason)"
eq "confirmation ends past END: the confirmation itself succeeded" 3 "$(cat "$CTL/pgrep_n")"
new_case confirmpastwrap; at 00:59:59; run GITEA_FSTRIM_WINDOW_START=23:30 GITEA_FSTRIM_WINDOW_END=01:00
eq "same, in a window that wraps midnight (23:30-01:00 at 00:59:59)" "0 window" "$(calls fstrim_calls) $(reason)"
new_case confirmwrapin; at 23:59:59; run GITEA_FSTRIM_WINDOW_START=23:30 GITEA_FSTRIM_WINDOW_END=01:00 GITEA_FSTRIM_MAX_GB_PER_RUN=2
eq "a confirmation that crosses midnight inside a wrapping window still trims" 1 "$(calls fstrim_calls)"

echo "== the job gate (the cleanup's act_runner signal) =="
new_case busywait; printf 'busy\nbusy\nbusy\nbusy\nbusy\n' > "$CTL/pgrep_seq"; run
eq "waits, then trims everything" 5 "$(calls fstrim_calls)"
has "logs the wait: 5 busy polls + 2 s of confirmation" "waited=7s" "$(cat "$CTL/out")"
new_case blip; printf 'busy\nidle\nbusy\n' > "$CTL/pgrep_seq"; run
has "a one-read idle between steps is not a gap: 3 consecutive idle reads are" "waited=5s" "$(cat "$CTL/out")"
new_case giveup; echo busy > "$CTL/pgrep_after"; run GITEA_FSTRIM_BUSY_WAIT_MAX_S=10
eq "busy past BUSY_WAIT_MAX_S: exit 0" 0 "$(rc)"
eq "busy past BUSY_WAIT_MAX_S: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "busy past BUSY_WAIT_MAX_S: reason" busy "$(reason)"
new_case nosysctl; touch "$CTL/systemctl_fail"; run GITEA_FSTRIM_BUSY_WAIT_MAX_S=10
eq "unreadable MainPID is BUSY" busy "$(reason)"
eq "unreadable MainPID: nothing trimmed" 0 "$(calls fstrim_calls)"
new_case pgreperr; echo 2 > "$CTL/pgrep_rc"; run GITEA_FSTRIM_BUSY_WAIT_MAX_S=10
eq "a pgrep error is BUSY" busy "$(reason)"
new_case down; echo 0 > "$CTL/mainpid"; run
eq "MainPID 0 (daemon stopped) is no job: trims" 5 "$(calls fstrim_calls)"
eq "MainPID 0: pgrep is never asked about pid 0" 0 "$(cat "$CTL/pgrep_n" 2>/dev/null || echo 0)"
new_case busyclose; at 05:29:50; echo busy > "$CTL/pgrep_after"; run GITEA_FSTRIM_BUSY_WAIT_MAX_S=1800
eq "the window closing ends a job wait" window "$(reason)"

echo "== the day gate: DAYS, keyed on the weekday the window STARTED =="
NOSUN="GITEA_FSTRIM_DAYS=mon tue wed thu fri sat"
new_case sunday; at 04:20:00 2026-11-22; run "$NOSUN" GITEA_FSTRIM_WINDOW_START=04:15 GITEA_FSTRIM_WINDOW_END=05:25
eq "a Sunday night not in DAYS: exit 0" 0 "$(rc)"
eq "a Sunday night not in DAYS: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "a Sunday night not in DAYS: reason" day "$(reason)"
new_case monday; at 04:20:00 2026-11-23; run "$NOSUN" GITEA_FSTRIM_WINDOW_START=04:15 GITEA_FSTRIM_WINDOW_END=05:25
eq "the Monday night after it trims" 5 "$(calls fstrim_calls)"
for spec in "1,2,3,4,5,6" "Mon, TUE,wed thu,Fri SAT"; do
  new_case "daysform$spec"; at 04:20:00 2026-11-22; run "GITEA_FSTRIM_DAYS=$spec"
  eq "DAYS='$spec' (numbers or names, any case, commas or spaces): no Sunday" day "$(reason)"
done
new_case dayswed; at 00:10:00; run "GITEA_FSTRIM_DAYS=3"
eq "DAYS=3 on a Wednesday trims" 5 "$(calls fstrim_calls)"
# A window that wraps midnight belongs to the night it STARTED: with DAYS=sat, 23:30-01:00 runs from
# Saturday 23:30 to Sunday 01:00, and not from Sunday 23:30 or in the small hours of Saturday.
WRAP="GITEA_FSTRIM_WINDOW_START=23:30 GITEA_FSTRIM_WINDOW_END=01:00"
for c in "23:45:00 2026-11-21 5 Saturday-23:45" "00:30:00 2026-11-22 5 Sunday-00:30-after-a-Saturday-start" \
         "23:45:00 2026-11-22 0 Sunday-23:45" "00:30:00 2026-11-21 0 Saturday-00:30-after-a-Friday-start"; do
  set -- $c
  new_case "wrapday$4"; at "$1" "$2"; run GITEA_FSTRIM_DAYS=sat $WRAP
  eq "DAYS=sat, window 23:30-01:00, ${4//-/ }: slices" "$3" "$(calls fstrim_calls)"
done
new_case daymid; at 23:59:59 2026-11-21; run GITEA_FSTRIM_DAYS=sat $WRAP GITEA_FSTRIM_MAX_GB_PER_RUN=2
eq "DAYS=sat: an idle confirmation that crosses into Sunday stays on Saturday's night" 1 "$(calls fstrim_calls)"

echo "== the window-relative deadline: each slice is sized to END in the window, and killed if it stalls =="
MiB=1048576
# 05:24:04 + 2 s of idle confirmation leaves 54 s to 05:25: 1.8 GiB at 30 s/GiB, cut to 14 whole groups.
new_case fit; at 05:24:04; run GITEA_FSTRIM_WINDOW_END=05:25
eq "the slice is cut to the whole groups that fit before the end" "-v -m 64K -o 0 -l $((1792 * MiB)) /" "$(nth fstrim_calls 1)"
has "its kill timer is the seconds left + the 60 s allowance" "kill_after=114s" "$(cat "$CTL/out")"
eq "then the window is over" "1 window" "$(calls fstrim_calls) $(reason)"
eq "no process-group kill: no code line runs timeout(1), whose return did not prove fstrim exited" "" \
  "$(grep -nE '^[[:space:]]*[^#[:space:]][^#]*\btimeout[[:space:]]' "$SCRIPT")"
new_case fitbig; at 05:24:04; run GITEA_FSTRIM_WINDOW_END=05:25 GITEA_FSTRIM_CHUNK_GB=16
eq "a 16 GiB chunk near the end shrinks to fit just the same (the tail does not depend on the chunk)" \
  "-v -m 64K -o 0 -l $((1792 * MiB)) /" "$(nth fstrim_calls 1)"
new_case fitgroup; at 05:24:56; run GITEA_FSTRIM_WINDOW_END=05:25   # 2 s left: 68 MiB, under one 128 MiB group
eq "less than one group fits before the end: nothing trimmed, reason window" "0 window" "$(calls fstrim_calls) $(reason)"
new_case fitrate; at 05:20:00; run GITEA_FSTRIM_WINDOW_END=05:25 GITEA_FSTRIM_SEC_PER_GB=10000
eq "the rate is a setting: at 10000 s/GiB, 298 s fits less than one group" "0 window" "$(calls fstrim_calls) $(reason)"
new_case fitwrap; at 23:59:00; run GITEA_FSTRIM_WINDOW_START=23:30 GITEA_FSTRIM_WINDOW_END=01:00 GITEA_FSTRIM_MAX_GB_PER_RUN=2
has "a window that wraps midnight: 3658 s left to 01:00, killed 60 s after" "kill_after=3718s" "$(cat "$CTL/out")"
inflight() { awk '{ print $2 }' "$CTL/textfile/gitea_runner_fstrim_inflight.prom" 2>/dev/null; }
# A stalled drive: the fstrim never returns. From a saved cursor at 2 GiB, 05:24:54 + 2 s leaves 4 s (one
# group fits); with a 1 s allowance it is SIGKILLed 5 REAL seconds in, dies, and is reaped.
new_case deadline; touch "$CTL/hang"; printf 'cursor=%s\npasses=4\n' $((2 * GiB)) > "$CTL/state/state"; at 05:24:54
t0=$("$REAL_DATE" +%s); run GITEA_FSTRIM_WINDOW_END=05:25 GITEA_FSTRIM_KILL_ALLOWANCE_S=1; t1=$("$REAL_DATE" +%s)
eq "a stalled fstrim gets its one group" "-v -m 64K -o $((2 * GiB)) -l $((128 * MiB)) /" "$(nth fstrim_calls 1)"
has "and a SIGKILL 5 s in (4 s left + 1 s)" "still running 5s in, 1s past the window end: SIGKILL" "$(cat "$CTL/out")"
[ $(( t1 - t0 )) -ge 4 ] && [ $(( t1 - t0 )) -le 10 ] && ok "killed at the deadline: the run took $(( t1 - t0 )) s, not the 60 s stall" \
  || bad "killed at the deadline" "the run took $(( t1 - t0 )) s"
eq "a killed and reaped slice: exit 0" 0 "$(rc)"
eq "a killed and reaped slice: reason" deadline "$(reason)"
eq "a killed slice is unconfirmed: the cursor stays at its start" "$((2 * GiB)) 4" "$(st cursor) $(st passes)"
eq "a killed slice is not counted" "0 0" "$(beacon range_bytes) $(beacon submitted_bytes)"
has "a killed slice: logged" "SIGKILLed and reaped; the cursor stays at $((2 * GiB))" "$(cat "$CTL/out")"
eq "in_flight was 1 during the slice, 0 once the child was reaped" "1 0" "$(cat "$CTL/inflight_seen") $(inflight)"
# A FITRIM that outlives the SIGKILL (uninterruptible in the kernel), emulated through the liveness seam:
# still "alive" 2 s (the grace) after the KILL, so the run exits 8 and leaves it.
new_case stuck; touch "$CTL/stuck"; printf 'cursor=%s\npasses=4\n' $((2 * GiB)) > "$CTL/state/state"; at 05:24:54
t0=$("$REAL_DATE" +%s)
run GITEA_FSTRIM_WINDOW_END=05:25 GITEA_FSTRIM_KILL_ALLOWANCE_S=1 GITEA_FSTRIM_STUCK_GRACE_S=2 GITEA_FSTRIM_PROC_DIR="$CTL/proc"
t1=$("$REAL_DATE" +%s)
eq "a fstrim alive after its SIGKILL + grace: exit 8" 8 "$(rc)"
eq "a stuck slice: reason" stuck "$(reason)"
eq "a stuck slice: the beacon's exit code" 8 "$(beacon exit_code)"
eq "a stuck slice: the cursor stays at its start" "$((2 * GiB)) 4" "$(st cursor) $(st passes)"
eq "a stuck slice: in_flight is left at 1" 1 "$(inflight)"
has "a stuck slice: says it keeps the lock" "later runs exit 7 until it ends" "$(cat "$CTL/out")"
[ $(( t1 - t0 )) -ge 6 ] && [ $(( t1 - t0 )) -le 12 ] && ok "stuck after the KILL (5 s) plus the grace (2 s): $(( t1 - t0 )) s" \
  || bad "stuck after the KILL plus the grace" "the run took $(( t1 - t0 )) s"
echo "== gitea_runner_fstrim_in_flight: 1 around each slice, 0 after each reap =="
new_case inflight; run
eq "1 while each of the five slices ran" "1 1 1 1 1" "$(tr '\n' ' ' < "$CTL/inflight_seen" | sed 's/ $//')"
eq "0 after the last reap" 0 "$(inflight)"
new_case inflightfail; echo 2 > "$CTL/fail_on"; run
eq "a failed fstrim (it exited, and was reaped): 0" "5 0" "$(rc) $(inflight)"

echo "== the stop file =="
new_case stop; touch "$CTL/stop"; run
eq "present at start: exit 0" 0 "$(rc)"
eq "present at start: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "present at start: reason" stopped "$(reason)"
new_case stopwait; echo busy > "$CTL/pgrep_after"; echo 3 > "$CTL/stop_after"; run
eq "appears during a job wait: stops" stopped "$(reason)"
eq "appears during a job wait: nothing trimmed" 0 "$(calls fstrim_calls)"
new_case stopconfirm; echo 1 > "$CTL/stop_after"; run   # sleep 1 is inside a confirmation that then succeeds
eq "appears during a SUCCESSFUL idle confirmation: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "appears during a successful idle confirmation: reason" stopped "$(reason)"
eq "appears during a successful idle confirmation: the confirmation did succeed" 3 "$(cat "$CTL/pgrep_n")"
new_case stopbetween; echo 3 > "$CTL/stop_after"; run   # sleeps 1-2 confirm idle, 3 is the pause after slice 1
eq "appears between slices: one slice, then stops" 1 "$(calls fstrim_calls)"
eq "appears between slices: progress saved" $((2 * GiB)) "$(st cursor)"

echo "== one run at a time =="
new_case locked; echo sentinel > "$CTL/textfile/gitea_runner_fstrim.prom"
( exec 9>"$CTL/lock"; flock 9; touch "$CTL/held"; "$REAL_SLEEP" 5 ) &
holder=$!
for _ in $(seq 1 50); do [ -f "$CTL/held" ] && break; "$REAL_SLEEP" 0.1; done
run
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
eq "exit 7" 7 "$(rc)"
eq "nothing trimmed" 0 "$(calls fstrim_calls)"
has "says why" "another run" "$(cat "$CTL/out")"
eq "the holder's beacon is left alone" sentinel "$(cat "$CTL/textfile/gitea_runner_fstrim.prom")"

echo "== invalid settings: exit 2 before anything is read or trimmed =="
for b in GITEA_FSTRIM_CHUNK_GB=0 GITEA_FSTRIM_CHUNK_GB=x GITEA_FSTRIM_CHUNK_GB=65 GITEA_FSTRIM_MAX_GB_PER_RUN=0 \
         GITEA_FSTRIM_MAX_GB_PER_RUN=1 GITEA_FSTRIM_PAUSE_S=-1 GITEA_FSTRIM_PAUSE_S=1.5 GITEA_FSTRIM_BUSY_WAIT_MAX_S=abc \
         GITEA_FSTRIM_WINDOW_START=24:00 GITEA_FSTRIM_WINDOW_START=5:30 GITEA_FSTRIM_WINDOW_END=05:60 \
         "GITEA_FSTRIM_WINDOW_END=00:00" GITEA_FSTRIM_DAYS=8 GITEA_FSTRIM_DAYS=0 "GITEA_FSTRIM_DAYS=mon funday" \
         "GITEA_FSTRIM_DAYS=, ," "GITEA_FSTRIM_DAYS=*" GITEA_FSTRIM_SEC_PER_GB=0 GITEA_FSTRIM_SEC_PER_GB=2.5 \
         GITEA_FSTRIM_KILL_ALLOWANCE_S=0 GITEA_FSTRIM_KILL_ALLOWANCE_S=x GITEA_FSTRIM_STUCK_GRACE_S=0 \
         GITEA_FSTRIM_STUCK_GRACE_S=-5; do
  new_case "set$(printf '%s' "${b#GITEA_FSTRIM_}" | tr -c 'A-Za-z0-9_=' '_')"; run "$b"
  eq "$b -> exit 2" 2 "$(rc)"
  eq "$b -> nothing trimmed, geometry not read" "0 0" "$(calls fstrim_calls) $(calls tune2fs_calls)"
  eq "$b -> reason" invalid "$(reason)"
done

echo "== only an ext4 mount point with readable geometry (exit 1) =="
new_case xfs; echo xfs > "$CTL/fstype"; run
eq "xfs -> exit 1" 1 "$(rc)"
eq "xfs -> nothing trimmed, tune2fs not run" "0 0" "$(calls fstrim_calls) $(calls tune2fs_calls)"
has "xfs -> says why" "not ext4" "$(cat "$CTL/out")"
eq "xfs -> reason" unsupported "$(reason)"
new_case nomount; touch "$CTL/nomount"; run
eq "not a mount point -> exit 1" 1 "$(rc)"
new_case notune; touch "$CTL/tune2fs_fail"; run
eq "tune2fs fails -> exit 1" 1 "$(rc)"
eq "tune2fs fails -> nothing trimmed" 0 "$(calls fstrim_calls)"
new_case nogeo; : > "$CTL/blocks"; run
eq "unreadable block count -> exit 1" 1 "$(rc)"

echo "== failures =="
new_case trimfail; echo 2 > "$CTL/fail_on"; run
eq "a failed fstrim -> exit 5" 5 "$(rc)"
eq "stops at the failing slice" 2 "$(calls fstrim_calls)"
eq "only the completed slice is recorded" $((2 * GiB)) "$(st cursor)"
eq "reason" fstrim_failed "$(reason)"
has "logs fstrim's error" "Input/output error" "$(cat "$CTL/out")"
new_case nostate; touch "$CTL/afile"; run GITEA_FSTRIM_STATE_FILE="$CTL/afile/state"
eq "state not writable -> exit 6" 6 "$(rc)"
eq "state not writable -> nothing trimmed" 0 "$(calls fstrim_calls)"

echo "== the cursor is durable: the new file is fsynced before the rename, its directory after =="
# A rename is atomic but not durable: a power cut can still lose it, and a reboot also resets ext4's trimmed
# flags, so a lost cursor means re-trimming full slices. Calls: the start-of-run write (1-2), slice 1 (3-4).
new_case durable; run GITEA_FSTRIM_MAX_GB_PER_RUN=2
eq "two syncs per state write" 4 "$(calls sync_calls)"
tmpre="^$CTL/state/state\\.[A-Za-z0-9]{4}\$"
grep -qE "$tmpre" <<< "$(nth sync_calls 3)" && ok "slice 1: the temp file is synced first ($(nth sync_calls 3))" \
  || bad "slice 1: the temp file is synced first" "got: $(nth sync_calls 3)"
eq "slice 1: the state file still held the OLD cursor when the temp file was synced" "cursor=0 passes=0 " "$(nth sync_seen 3)"
eq "slice 1: then the directory, after the rename" "$CTL/state" "$(nth sync_calls 4)"
eq "slice 1: by then it held the new cursor" "cursor=$((2 * GiB)) passes=0 " "$(nth sync_seen 4)"
new_case syncfail0; echo 1 > "$CTL/sync_fail_on"; run
eq "an fsync error on the first write -> exit 6, nothing trimmed" "6 0" "$(rc) $(calls fstrim_calls)"
new_case syncfail3; echo 3 > "$CTL/sync_fail_on"; run
eq "an fsync error after slice 1 -> exit 6" 6 "$(rc)"
eq "an fsync error after slice 1: the cursor on disk is the old one" 0 "$(st cursor)"
eq "an fsync error after slice 1: no temp file left behind" "" "$(find "$CTL/state" -name 'state.????')"
eq "an fsync error after slice 1: reason" state "$(reason)"
new_case syncfail4; echo 4 > "$CTL/sync_fail_on"; run
eq "an fsync error on the directory after the rename -> exit 6" 6 "$(rc)"

echo "== SIGTERM during a slice (systemctl stop): waits for it, records the stop, does not count it =="
new_case sigterm; touch "$CTL/slow_trim"
env PATH="$BIN:$PATH" GITEA_FSTRIM_ENV_FILE=/nonexistent GITEA_FSTRIM_STATE_FILE="$CTL/state/state" \
  GITEA_FSTRIM_STOP_FILE="$CTL/stop" GITEA_FSTRIM_LOCK_FILE="$CTL/lock" GITEA_FSTRIM_TEXTFILE_DIR="$CTL/textfile" \
  bash "$SCRIPT" > "$CTL/out" 2>&1 &
spid=$!
for _ in $(seq 1 100); do [ -f "$CTL/fstrim_running" ] && break; "$REAL_SLEEP" 0.1; done
kill -TERM "$spid" $(pgrep -P "$spid") 2>/dev/null   # what KillMode=control-group does
wait "$spid"; echo $? > "$CTL/rc"
eq "exit 143" 143 "$(rc)"
eq "reason" signal "$(reason)"
eq "the in-flight slice is not counted" 0 "$(st cursor)"
eq "only that slice ran" 1 "$(calls fstrim_calls)"

echo "== the beacon =="
new_case beacon; run
P="$CTL/textfile/gitea_runner_fstrim.prom"
eq "last run time (the fake clock)" "$(cat "$CTL/clock")" "$(beacon last_run_seconds)"
eq "bytes submitted to TRIM, summed from fstrim -v" $((5 * GiB)) "$(beacon submitted_bytes)"
eq "range covered" "$SIZE" "$(beacon range_bytes)"
eq "filesystem size" "$SIZE" "$(beacon fs_bytes)"
eq "cursor" 0 "$(beacon cursor_bytes)"
eq "passes" 1 "$(beacon passes_total)"
eq "exit code" 0 "$(beacon exit_code)"
eq "one exit reason" 1 "$(grep -c '^gitea_runner_fstrim_exit_reason{' "$P")"
has "says the host-side reclamation is not visible" "not visible from the guest" "$(cat "$P")"
eq "readable by node_exporter (0644)" 644 "$(stat -c %a "$P")"
eq "no temp files left behind: the beacon and the in-flight marker only" \
  "gitea_runner_fstrim.prom gitea_runner_fstrim_inflight.prom" "$(ls "$CTL/textfile" | tr '\n' ' ' | sed 's/ $//')"

echo "== the script's defaults equal the role defaults that ship (as test-cleanup.sh [M] does) =="
# Every case above runs without the env file, so the script's fallbacks are what they test. Each line of
# the env template must be exactly one {{ var }} or {{ var | join(' ') }} (optionally quoted), and its
# role default, rendered the same way, must equal the script's ${NAME:-default}.
DEFAULTS_YML="$ROLE/defaults/main.yml"; ENV_TMPL="$ROLE/templates/gitea-runner-fstrim.env.j2"
role_default() { python3 - "$DEFAULTS_YML" "$1" "${2:-}" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
v = d.get(sys.argv[2])
if sys.argv[3] == "join":
    print(" ".join(map(str, v)) if isinstance(v, list) else "NOT-A-LIST")
else:
    print("" if v is None else v)
PY
}
script_default() { sed -n "s/.*\${$1:-\([^}]*\)}.*/\1/p" "$SCRIPT" | head -1; }
checked=0
while IFS= read -r ln; do
  case "$ln" in \#* | '') continue ;; esac
  name="${ln%%=*}"; val="${ln#*=}"; val="${val#\"}"; val="${val%\"}"
  var="$(printf '%s' "$val" | sed -n 's/^{{ \([a-z0-9_]*\) }}$/\1/p')"; how=""
  if [ -z "$var" ]; then
    var="$(printf '%s' "$val" | sed -n "s/^{{ \([a-z0-9_]*\) | join(' ') }}\$/\1/p")"; how="join"
  fi
  checked=$((checked + 1))
  if [ -z "$var" ]; then bad "M: $name renders '$val', not a single {{ var }} or {{ var | join(' ') }}"; continue; fi
  eq "M: $name = $var${how:+ | $how}" "$(role_default "$var" "$how")" "$(script_default "$name")"
done < "$ENV_TMPL"
[ "$checked" -ge 12 ] && ok "M: $checked knobs checked" || bad "M: only $checked knobs found in $ENV_TMPL"
eq "M: the env file path" "$(role_default gitea_runner_fstrim_env)" "$(script_default GITEA_FSTRIM_ENV_FILE)"
eq "M: the role ships both switches off" "False False" \
  "$(role_default gitea_runner_fstrim_enabled) $(role_default gitea_runner_root_nodiscard)"

echo "== the fstab edit: only the root entry's discard token goes, and a second run changes nothing =="
# Applies the regexes of the task in tasks/main.yml exactly as ansible.builtin.replace does
# (re.compile(regexp, re.MULTILINE), then re.subn over the file, once per loop item).
out="$(python3 - "$ROLE/tasks/main.yml" <<'PY'
import re, sys, yaml
tasks = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
task = [t for t in tasks if (t.get("ansible.builtin.replace") or {}).get("path") == "/etc/fstab"]
assert len(task) == 1, "expected one replace task on /etc/fstab"
items = task[0]["loop"]
def apply(text):
    for it in items:
        text = re.subn(re.compile(it["regexp"], re.MULTILINE), it["replace"], text, 0)[0]
    return text
other = "LABEL=UEFI\t/boot/efi\tvfat\tumask=0077\t0 1\n/swapfile none swap sw 0 0\n"
cases = [
    ("LABEL=cloudimg-rootfs\t/\t ext4\tdiscard,commit=30,errors=remount-ro\t0 1\n",
     "LABEL=cloudimg-rootfs\t/\t ext4\tcommit=30,errors=remount-ro\t0 1\n"),
    ("LABEL=cloudimg-rootfs / ext4 commit=30,discard,errors=remount-ro 0 1\n",
     "LABEL=cloudimg-rootfs / ext4 commit=30,errors=remount-ro 0 1\n"),
    ("LABEL=cloudimg-rootfs / ext4 commit=30,errors=remount-ro,discard 0 1\n",
     "LABEL=cloudimg-rootfs / ext4 commit=30,errors=remount-ro 0 1\n"),
    ("LABEL=cloudimg-rootfs / ext4 discard 0 1\n", "LABEL=cloudimg-rootfs / ext4 defaults 0 1\n"),
    ("LABEL=cloudimg-rootfs / ext4 discard\n", "LABEL=cloudimg-rootfs / ext4 defaults\n"),
    ("LABEL=cloudimg-rootfs / ext4 nodiscard,commit=30 0 1\n", None),   # not a discard token
    ("LABEL=cloudimg-rootfs / ext4 commit=30,errors=remount-ro 0 1\n", None),  # already done
    ("LABEL=other / ext4 discard,commit=30 0 1\n", None),               # not the cloud-image root
    ("LABEL=cloudimg-rootfs /data ext4 discard,commit=30 0 1\n", None),  # not /
]
for before, want in cases:
    want = before if want is None else want
    got = apply(other + before + other)
    print(("ok " if got == other + want + other else "BAD ") + repr(before.rstrip()) + " -> "
          + (repr(got[len(other):len(got) - len(other)].rstrip()) if got.startswith(other) and got.endswith(other) else repr(got)))
    again = apply(got)
    print(("ok " if again == got else "BAD ") + "a second run changes nothing: " + repr(before.rstrip()))
PY
)" || bad "fstab: the python check did not run" "$out"
while IFS= read -r l; do
  case "$l" in "ok "*) ok "fstab: ${l#ok }" ;; *) bad "fstab: ${l#BAD }" ;; esac
done <<< "$out"

echo "== --check safety: a command/shell result a later task reads is registered with check_mode: false =="
# In --check, Ansible skips command/shell/script/raw tasks, so their registered result has no real stdout
# or rc: a later `when` that reads one fails the play (older ansible-core), or reads an empty result and
# silently answers wrong (ansible-core 2.16: the remount looked "skipped" on a host that needs it). That
# broke this PR's own "--check first" rollout (reviewer-claude on #1858: the root-mount read). Every such
# task in the role is checked.
out="$(python3 - "$ROLE/tasks/main.yml" "$ROLE/handlers/main.yml" <<'PY'
import re, sys, yaml
SKIPPED_IN_CHECK = {m for b in ("command", "shell", "script", "raw") for m in (b, "ansible.builtin." + b)}
for path in sys.argv[1:]:
    tasks = yaml.safe_load(open(path, encoding="utf-8")) or []
    for i, t in enumerate(tasks):
        reg = t.get("register")
        if not reg or not SKIPPED_IN_CHECK & set(t):
            continue
        reads = re.compile(re.escape(reg) + r"\.(stdout|stdout_lines|stderr|stderr_lines|rc)\b")
        readers = [u.get("name", "?") for u in tasks[i + 1:] if reads.search(yaml.safe_dump(u))]
        if not readers:
            print(f"ok {t.get('name')}: no later task reads its result")
        elif t.get("check_mode") is False:
            print(f"ok {t.get('name')}: read later ({readers[0]}), and runs in --check (check_mode: false)")
        else:
            print(f"BAD {t.get('name')}: read later ({readers[0]}) but check_mode is {t.get('check_mode')!r}, not false")
PY
)" || bad "check mode: the python check did not run" "$out"
while IFS= read -r l; do
  case "$l" in "ok "*) ok "check mode: ${l#ok }" ;; *) bad "check mode: ${l#BAD }" ;; esac
done <<< "$out"
grep -q "^ok Read the live options of the root mount: read later" <<< "$out" \
  && ok "check mode: the root-mount read is among the tasks checked" || bad "check mode: the root-mount read was not checked" "$out"

echo "== the per-host schedule: tails by 05:30, no overlap on a node, the cap and the unit timeout fit, the registry trim missed =="
# The node of each runner comes from the runners' tofu (kubernetes/infra/runners/variables.tf).
# - A window's TAIL is how far its trimming normally runs past its END: slices are sized to end by then, a
#   stalled one is killed gitea_runner_fstrim_kill_allowance_s after it (ext4 usually stops within one
#   block group, 128 MiB on these roots), and one still alive gitea_runner_fstrim_stuck_grace_s later is
#   left as `stuck` (exit 8, alerted: a stuck discard can outlast any tail, so that overlap is detected,
#   not prevented). So tail = allowance + grace + one group at sec_per_gb, rounded UP to whole minutes,
#   derived per host from its settings (not the chunk size, and not a constant). The tail must end by
#   05:30 UTC and before the next window on the same node.
# - The service's TimeoutStartSec, rendered from the template for every host and for the role default,
#   must cover the whole window plus the allowance and the grace with slack, so a busy night ends with the
#   script's own exit, not a systemd timeout.
# - A capped run must fit its own window: the cap at 23 s per GiB (the measured worst) plus, per slice,
#   the pause and 2 s of idle confirmation.
# - The registry LXC's weekly trim is a hand-installed timer + oneshot defined in
#   docs/runbooks/registry-cache.md (its node from that section's heading). It starts at OnCalendar + up to
#   RandomizedDelaySec + AccuracySec and waits out IO pressure per chunk, so only its deadline ends it: a
#   oneshot's TimeoutStartSec (RuntimeMaxSec does nothing on a oneshot), then up to TimeoutStopSec, which
#   must cover an in-flight chunk. Its span, from 15 min before its earliest start to 15 min after that
#   latest end, may cross midnight; it conflicts with every window on its node, on any day (by the window's
#   start day, gitea_runner_fstrim_days), that it overlaps, tail included.
# schedule_check <registry runbook>: one "ok ..." or "BAD ..." line per check. The fixture cases below feed
# it edited copies of the runbook, to prove each conflict is caught.
# TEST_KILL_ALLOWANCE_S / TEST_STUCK_GRACE_S, when set, replace every host's allowance / grace (the tail
# derivation's own fixtures).
schedule_check() { python3 - "$REPO" "$1" <<'PY'
import math, os, pathlib, re, sys, yaml
repo = pathlib.Path(sys.argv[1])
tf = (repo / "kubernetes/infra/runners/variables.tf").read_text(encoding="utf-8")
nodes = dict(re.findall(r'^\s*"(ci-runner-\d+)"\s*=\s*\{\s*node_name\s*=\s*"([^"]+)"', tf, re.M))
inv = (repo / "inventory/hosts.yml").read_text(encoding="utf-8")
group = re.search(r"^    gitea_runners:\n(.*?)(?=^    \S)", inv, re.M | re.S).group(1)
hosts = re.findall(r"^        (ci-runner-\d+):", group, re.M)
print(("ok " if len(hosts) >= 8 else "BAD ") + "gitea_runners hosts found: " + " ".join(hosts))
load = lambda p: (yaml.safe_load((repo / p).read_text(encoding="utf-8")) or {}) if (repo / p).exists() else {}
base = dict(load("ansible/roles/gitea_runner/defaults/main.yml")); base.update(load("ansible/group_vars/gitea_runners.yml"))
NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
def daynum(x):
    s = str(x).strip().lower()
    return int(s) if s in ("1", "2", "3", "4", "5", "6", "7") else (NAMES.index(s) + 1 if s in NAMES else None)
mins = lambda s: int(s[:2]) * 60 + int(s[3:])
hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
CUTOFF, GROUP_GIB = mins("05:30"), 128 / 1024
def allowance(v):
    return int(os.environ.get("TEST_KILL_ALLOWANCE_S") or v["gitea_runner_fstrim_kill_allowance_s"])
def grace(v):
    return int(os.environ.get("TEST_STUCK_GRACE_S") or v["gitea_runner_fstrim_stuck_grace_s"])
def tail_of(v):  # minutes a window's trimming normally runs past its end
    return math.ceil((allowance(v) + grace(v) + GROUP_GIB * int(v["gitea_runner_fstrim_sec_per_gb"])) / 60)
try:
    import jinja2  # Ansible's template engine; trim_blocks as the template module sets it
    svc = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True).from_string(
        (repo / "ansible/roles/gitea_runner/templates/gitea-runner-fstrim.service.j2").read_text(encoding="utf-8"))
except ImportError:
    svc = None
    print("BAD python3 jinja2 is missing, so the unit cannot be rendered (cloud-init ships it; apt install python3-jinja2)")
def timeout_covers(label, v):
    if svc is None:
        return
    s, e = mins(v["gitea_runner_fstrim_window_start"]), mins(v["gitea_runner_fstrim_window_end"])
    length = (e - s) % 1440 or 1440  # a window that wraps midnight included
    m = re.search(r"^TimeoutStartSec=(\d+)$", svc.render(**v), re.M)
    need = length * 60 + int(v["gitea_runner_fstrim_kill_allowance_s"]) + int(v["gitea_runner_fstrim_stuck_grace_s"]) + 300
    t = int(m.group(1)) if m else 0
    print(("ok " if t >= need else "BAD ") + f"{label}: the unit's TimeoutStartSec ({t} s) covers its {length}-min window,"
          f" the kill allowance, the stuck grace and 5 min ({need} s)")
timeout_covers("the role default (00:00-05:25)", base)
spans = {}
for h in hosts:
    v = dict(base); v.update(load(f"ansible/host_vars/{h}.yml"))
    s, e = v.get("gitea_runner_fstrim_window_start"), v.get("gitea_runner_fstrim_window_end")
    tail = tail_of(v)
    good = isinstance(s, str) and isinstance(e, str) and re.fullmatch(r"\d\d:\d\d", s) and re.fullmatch(r"\d\d:\d\d", e) \
        and 0 <= mins(s) < mins(e) and mins(e) + tail <= CUTOFF
    print(("ok " if good else "BAD ") + f"{h} window {s!r}-{e!r} is a quoted HH:MM span that ends, with its {tail}-min tail"
          f" ({allowance(v)} s kill allowance + {grace(v)} s stuck grace + one group), by 05:30")
    print(("ok " if h in nodes else "BAD ") + f"{h} has a node in variables.tf ({nodes.get(h)})")
    raw = v.get("gitea_runner_fstrim_days")
    days = [daynum(d) for d in raw] if isinstance(raw, list) else []
    okd = bool(days) and None not in days
    print(("ok " if okd else "BAD ") + f"{h} days {raw} are weekdays")
    if not (good and okd and h in nodes):
        continue
    cap, chunk, pause = (int(v[k]) for k in ("gitea_runner_fstrim_max_gb_per_run", "gitea_runner_fstrim_chunk_gb", "gitea_runner_fstrim_pause_s"))
    need, have = cap * 23 + math.ceil(cap / chunk) * (pause + 2), (mins(e) - mins(s)) * 60
    print(("ok " if need <= have else "BAD ") + f"{h}: a capped run ({cap} GiB, {need // 60} min at worst) fits its {have // 60}-min window")
    timeout_covers(h, v)
    spans.setdefault(nodes[h], []).append((mins(s), mins(e), h, set(days), tail))
for node, ws in sorted(spans.items()):
    ws.sort()
    clash = [f"{a[2]}/{b[2]}" for i, a in enumerate(ws) for b in ws[i + 1:] if b[0] < a[1] + a[4]]
    print(("ok " if not clash else "BAD ") + f"{node}: {' '.join(w[2] for w in ws)} do not overlap, tails included {clash or ''}")
reg = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
def unit(name):  # the body of the `cat > /etc/systemd/system/<name> <<'UNIT'` heredoc
    m = re.search(r"/" + re.escape(name) + r" <<'UNIT'\n(.*?)^UNIT$", reg, re.M | re.S)
    return m.group(1) if m else ""
def key(body, k):
    m = re.findall(r"^" + k + r"=(.*)$", body, re.M)
    return m[-1].strip() if m else None
UNITS = {"": 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}
def span(v, default=None):  # a systemd time span in seconds; None = unset, 0 or infinity = no limit
    if v is None:
        return default
    if v in ("0", "infinity"):
        return math.inf
    parts = re.findall(r"(\d+)\s*([a-z]*)\s*", v)
    if not parts or "".join(n + u for n, u in parts) != v.replace(" ", "") or any(u not in UNITS for _, u in parts):
        raise ValueError(f"cannot read the time span {v!r}")
    return sum(int(n) * UNITS[u] for n, u in parts)
sec = re.search(r"^## Thin-volume trim \(weekly, (ai-node\d+)\)$", reg, re.M)
svc, tmr = unit("registry-lxc-fstrim.service"), unit("registry-lxc-fstrim.timer")
cal = re.search(r"^OnCalendar=(?:(\w+) )?\*-\*-\* (\d\d):(\d\d)(?::\d\d)? UTC$", tmr, re.M)
if not (sec or svc or tmr):
    print("ok no hand-installed registry trim unit in this runbook (the registry is in its host's nightly sweep)")
else:
    print(("ok " if sec and svc and cal else "BAD ") + "the registry trim's node, service and OnCalendar are read from the runbook")
if sec and svc and cal:
    rnode, rday = sec.group(1), (daynum(cal.group(1)[:3]) if cal.group(1) else None)
    delay, accuracy = span(key(tmr, "RandomizedDelaySec"), 0), span(key(tmr, "AccuracySec"), 60)
    oneshot = key(svc, "Type") == "oneshot"
    # A oneshot is "activating" for its whole run: RuntimeMaxSec has no effect on it (systemd.service(5)).
    deadline = span(key(svc, "TimeoutStartSec"), math.inf) if oneshot else span(key(svc, "RuntimeMaxSec"), math.inf)
    stop = span(key(svc, "TimeoutStopSec"), 90)
    print(("ok " if deadline != math.inf else "BAD ") + "the registry trim has an overall deadline ("
          + ("TimeoutStartSec" if oneshot else "RuntimeMaxSec") + f" = {key(svc, 'TimeoutStartSec' if oneshot else 'RuntimeMaxSec')})"
          + ("" if deadline != math.inf or not oneshot or not key(svc, "RuntimeMaxSec") else "; RuntimeMaxSec does nothing on a oneshot"))
    lxc = (repo / "scripts/lxc-fstrim-chunked.sh").read_text(encoding="utf-8")
    chunk_gb = int(re.search(r'^CHUNK_GB="\$\{CHUNK_GB:-(\d+)\}"', lxc, re.M).group(1))
    pause = int(re.search(r'^PAUSE_S="\$\{PAUSE_S:-(\d+)\}"', lxc, re.M).group(1))
    inflight = chunk_gb * 23 + pause + 60  # a chunk at the measured worst, its pause, and the unmount
    print(("ok " if stop >= inflight else "BAD ") + f"TimeoutStopSec ({stop} s) lets an in-flight {chunk_gb} GiB chunk finish ({inflight} s)")
    if deadline != math.inf:
        first = int(cal.group(2)) * 60 + int(cal.group(3))
        busy = (first - 15, first + math.ceil((delay + accuracy + deadline + stop) / 60) + 15)
        print(f"ok the registry trim may hold {rnode} from {hm(busy[0] % 1440)} to {hm(busy[1] % 1440)}"
              + (" the next day" if busy[1] >= 1440 else "") + f" on weekday {rday or 'every day'}")
        NAMESD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for s, e, h, days, tail in spans.get(rnode, []):
            hits = []
            for rd in ([rday] if rday else range(1, 8)):
                r0, r1 = (rd - 1) * 1440 + busy[0], (rd - 1) * 1440 + busy[1]
                for w in (0, 1):  # this week and the next, for a span that crosses Sunday into Monday
                    for d in days:
                        a, b = (w * 7 + d - 1) * 1440 + s, (w * 7 + d - 1) * 1440 + e + tail
                        if a < r1 and r0 < b:
                            hits.append(NAMESD[d - 1])
            print(("ok " if not hits else "BAD ") + f"{h} ({hm(s)}-{hm(e)}) " + ("misses the registry trim's span"
                  if not hits else f"overlaps the registry trim's span on {', '.join(sorted(set(hits)))}"))
PY
}
out="$(schedule_check "$REPO/docs/runbooks/registry-cache.md")" || bad "windows: the python check did not run" "$out"
while IFS= read -r l; do
  case "$l" in "ok "*) ok "windows: ${l#ok }" ;; *) bad "windows: ${l#BAD }" ;; esac
done <<< "$out"

echo "== the schedule check catches a registry trim that would collide (edited copies of CT 5004's unit) =="
fixture() { # <sed program applied inside the registry unit heredocs> -> path of an edited copy of 5004's unit
  sed "/registry-lxc-fstrim\\.[a-z]* <<'UNIT'/,/^UNIT\$/{$1;}" "$REPO/ansible/roles/gitea_runner/tests/fixtures/registry-weekly-unit.md" > "$ROOT/registry.fixture.md"
  echo "$ROOT/registry.fixture.md"
}
expect_bad() { # <label> <sed program> <text a BAD line must contain>
  local o; o="$(schedule_check "$(fixture "$2")" 2>&1)"
  if grep -q "^BAD .*$3" <<< "$o"; then ok "fixture: $1"; else bad "fixture: $1" "no 'BAD ...$3' line in: $o"; fi
}
expect_bad "a deadline long enough to spill from Sunday into Monday's 00:00 window" \
  's/^TimeoutStartSec=.*/TimeoutStartSec=21h/' "ci-runner-1 .*overlaps the registry trim's span on Mon"
expect_bad "no deadline at all (a oneshot's default)" '/^TimeoutStartSec=/d' "has an overall deadline"
expect_bad "RuntimeMaxSec instead of TimeoutStartSec on a oneshot" \
  's/^TimeoutStartSec=/RuntimeMaxSec=/' "RuntimeMaxSec does nothing on a oneshot"
expect_bad "a stop time too short for the in-flight chunk" 's/^TimeoutStopSec=.*/TimeoutStopSec=180/' "TimeoutStopSec"
expect_bad "the registry trim moved to Saturday, when ci-runner-9 runs" \
  's/^OnCalendar=Sun /OnCalendar=Sat /' "ci-runner-9 .*overlaps the registry trim's span on Sat"
o="$(schedule_check "$(fixture 's/^TimeoutStartSec=.*/TimeoutStartSec=19h45min/')" 2>&1)"
eq "fixture: the shipped deadline itself passes" "" "$(grep '^BAD' <<< "$o")"
# The tail is derived from the kill allowance AND the stuck grace: 301 s + 30 s + a group = 6 min no longer
# fits the 5-min gaps between windows, nor 05:25 + its tail before 05:30; nor does a 250 s grace with the
# 60 s allowance; 236 s + 30 s + a group = exactly 5 min still does.
o="$(export TEST_KILL_ALLOWANCE_S=301; schedule_check "$REPO/docs/runbooks/registry-cache.md" 2>&1)"
grep -q "^BAD ai-node1: .*do not overlap" <<< "$o" && ok "fixture: a 301 s kill allowance makes the windows' 6-min tails overlap" \
  || bad "fixture: a 301 s kill allowance makes the windows' tails overlap" "$o"
grep -q "^BAD ci-runner-9 window .*6-min tail" <<< "$o" && ok "fixture: and pushes ci-runner-9's tail past 05:30" \
  || bad "fixture: and pushes ci-runner-9's tail past 05:30" "$o"
o="$(export TEST_STUCK_GRACE_S=250; schedule_check "$REPO/docs/runbooks/registry-cache.md" 2>&1)"
grep -q "^BAD ai-node1: .*do not overlap" <<< "$o" && ok "fixture: a 250 s stuck grace counts in the tail too (6 min: overlap)" \
  || bad "fixture: a 250 s stuck grace counts in the tail too" "$o"
o="$(export TEST_KILL_ALLOWANCE_S=236; schedule_check "$REPO/docs/runbooks/registry-cache.md" 2>&1)"
eq "fixture: 236 s + the 30 s grace + a group (5 min of tail, exactly the gaps) still fits" "" "$(grep '^BAD' <<< "$o")"

echo
echo "gitea-runner-fstrim-chunked: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
