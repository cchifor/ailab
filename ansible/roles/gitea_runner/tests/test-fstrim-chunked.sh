#!/usr/bin/env bash
# Tests for files/gitea-runner-fstrim-chunked.sh, the nightly chunked trim of a runner's root fs (#1857),
# plus the role pieces it depends on: the env template's defaults, the fstab regexes, the per-node windows.
#
# The script runs for real with mocked findmnt, tune2fs, fstrim, systemctl, pgrep, sleep and date on PATH.
# `date +%s` is a FAKE CLOCK ($CTL/clock): sleep and fstrim advance it, so the window and the job waits are
# deterministic and take no real time. The invariants:
# - every block of the fs is covered, from ext4's geometry, in slices of whole block groups, with -m 64K;
# - progress persists per slice (fsynced: file, then directory), resumes, wraps at the end and counts passes;
# - nothing is trimmed outside the window (also one that wraps midnight, and also when the idle confirmation
#   ends past it), beyond the cap (slices are cut to it), or while a job runs (the cleanup's act_runner
#   signal), and a long job ends the night's run;
# - the lock, the stop file (also one that appears during the idle confirmation), bad settings and a
#   non-ext4 fs stop it before any trim;
# - the beacon carries the run's numbers and why it ended;
# - the per-host windows never overlap on a node, nor the registry trim's slot on ai-node1.
#
# Run: bash ansible/roles/gitea_runner/tests/test-fstrim-chunked.sh   (Linux; needs python3 + PyYAML)
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
at() { "$REAL_DATE" -u -d "2026-11-18 $1" +%s > "$CTL/clock"; }   # set the fake clock (UTC)
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
# 05:27:00, 40 s per slice + 2 s of idle confirmation + 20 s pause: slices start at 05:27, 05:28 and 05:29.
new_case closes; at 05:27:00; run
eq "closes mid-run: exit 0" 0 "$(rc)"
eq "closes mid-run: the slices begun inside the window ran" 3 "$(calls fstrim_calls)"
eq "closes mid-run: reason" window "$(reason)"
eq "closes mid-run: progress saved for tomorrow" $((6 * GiB)) "$(st cursor)"
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
         "GITEA_FSTRIM_WINDOW_END=00:00"; do
  new_case "set${b#GITEA_FSTRIM_}"; run "$b"
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
eq "no temp files left behind" 1 "$(ls "$CTL/textfile" | wc -l | tr -d ' ')"

echo "== the script's defaults equal the role defaults that ship (as test-cleanup.sh [M] does) =="
# Every case above runs without the env file, so the script's fallbacks are what they test. Each line of
# the env template must be exactly one {{ var }} (optionally quoted), and its role default must equal the
# script's ${NAME:-default}.
DEFAULTS_YML="$ROLE/defaults/main.yml"; ENV_TMPL="$ROLE/templates/gitea-runner-fstrim.env.j2"
role_default() { python3 - "$DEFAULTS_YML" "$1" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
v = d.get(sys.argv[2])
print("" if v is None else v)
PY
}
script_default() { sed -n "s/.*\${$1:-\([^}]*\)}.*/\1/p" "$SCRIPT" | head -1; }
checked=0
while IFS= read -r ln; do
  case "$ln" in \#* | '') continue ;; esac
  name="${ln%%=*}"; val="${ln#*=}"; val="${val#\"}"; val="${val%\"}"
  var="$(printf '%s' "$val" | sed -n 's/^{{ \([a-z0-9_]*\) }}$/\1/p')"
  checked=$((checked + 1))
  if [ -z "$var" ]; then bad "M: $name renders '$val', not a single {{ var }}"; continue; fi
  eq "M: $name = $var" "$(role_default "$var")" "$(script_default "$name")"
done < "$ENV_TMPL"
[ "$checked" -ge 8 ] && ok "M: $checked knobs checked" || bad "M: only $checked knobs found in $ENV_TMPL"
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

echo "== the per-host windows: inside 00:00-06:00 UTC, never overlapping on one Proxmox node, clear of the registry trim =="
# The node of each runner comes from the runners' tofu (kubernetes/infra/runners/variables.tf). ai-node1 also
# runs the registry LXC's weekly trim, a hand-installed timer defined in docs/runbooks/registry-cache.md: its
# start window (OnCalendar + RandomizedDelaySec), with 15 min of margin before and 30 after, counts as an
# occupied slot on EVERY day (the runners avoid it by schedule, not by day-of-week logic). A runner's window
# must end 5 min before the next slot starts: a slice begun just before its END runs past it.
out="$(python3 - "$REPO" <<'PY'
import pathlib, re, sys, yaml
repo = pathlib.Path(sys.argv[1])
tf = (repo / "kubernetes/infra/runners/variables.tf").read_text(encoding="utf-8")
nodes = dict(re.findall(r'^\s*"(ci-runner-\d+)"\s*=\s*\{\s*node_name\s*=\s*"([^"]+)"', tf, re.M))
inv = (repo / "inventory/hosts.yml").read_text(encoding="utf-8")
group = re.search(r"^    gitea_runners:\n(.*?)(?=^    \S)", inv, re.M | re.S).group(1)
hosts = re.findall(r"^        (ci-runner-\d+):", group, re.M)
print(("ok " if len(hosts) >= 8 else "BAD ") + "gitea_runners hosts found: " + " ".join(hosts))
mins = lambda s: int(s[:2]) * 60 + int(s[3:])
spans = {}
for h in hosts:
    hv = repo / "ansible/host_vars" / (h + ".yml")
    v = yaml.safe_load(hv.read_text(encoding="utf-8")) if hv.exists() else {}
    s, e = v.get("gitea_runner_fstrim_window_start"), v.get("gitea_runner_fstrim_window_end")
    good = isinstance(s, str) and isinstance(e, str) and re.fullmatch(r"\d\d:\d\d", s) and re.fullmatch(r"\d\d:\d\d", e) \
        and 0 <= mins(s) < mins(e) <= mins("06:00")
    print(("ok " if good else "BAD ") + f"{h} window {s!r}-{e!r} is a quoted HH:MM span inside 00:00-06:00")
    print(("ok " if h in nodes else "BAD ") + f"{h} has a node in variables.tf ({nodes.get(h)})")
    if good and h in nodes:
        spans.setdefault(nodes[h], []).append((mins(s), mins(e), h, True))
reg = (repo / "docs/runbooks/registry-cache.md").read_text(encoding="utf-8")
blk = reg[reg.find("registry-lxc-fstrim.timer <<"):] if "registry-lxc-fstrim.timer <<" in reg else ""
cal = re.search(r"^OnCalendar=\S+ \*-\*-\* (\d\d):(\d\d)(?::\d\d)? UTC$", blk, re.M)
dly = re.search(r"^RandomizedDelaySec=(\d+)min$", blk, re.M)
print(("ok " if cal and dly else "BAD ") + "the registry trim timer (OnCalendar, RandomizedDelaySec) is read from registry-cache.md")
if cal and dly:
    rs = int(cal.group(1)) * 60 + int(cal.group(2))
    slot = (rs - 15, rs + int(dly.group(1)) + 30, "registry-lxc-fstrim", False)
    print(f"ok ai-node1 keeps {slot[0] // 60:02d}:{slot[0] % 60:02d}-{slot[1] // 60:02d}:{slot[1] % 60:02d} free for the registry trim")
    spans.setdefault("ai-node1", []).append(slot)
for node, ws in sorted(spans.items()):
    ws.sort()
    clash = [f"{a[2]}/{b[2]}" for i, a in enumerate(ws) for b in ws[i + 1:] if b[0] < a[1] + (5 if a[3] else 0)]
    print(("ok " if not clash else "BAD ") + f"{node}: {' '.join(w[2] for w in ws)} do not overlap {clash or ''}")
PY
)" || bad "windows: the python check did not run" "$out"
while IFS= read -r l; do
  case "$l" in "ok "*) ok "windows: ${l#ok }" ;; *) bad "windows: ${l#BAD }" ;; esac
done <<< "$out"

echo
echo "gitea-runner-fstrim-chunked: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
