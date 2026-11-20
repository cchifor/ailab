#!/usr/bin/env bash
# Tests for kubernetes/apps/node-fstrim/node-fstrim.sh: the chunked, IO-pressure-guarded, deadline-bound
# fstrim of mounted filesystems that the nightly Talos node trim runs on each node's /var.
#
# The script runs for real with stubbed mountpoint, stat, fstrim and sleep and a fake clock (`date +%s`).
# The invariants:
# - every byte is covered: chunks from statfs, the LAST one without -l (it runs to the real end, which
#   statfs understates), -m 64K always, mount points in order;
# - the rest after a chunk is as long as the chunk took, between PAUSE_MIN_S and PAUSE_MAX_S;
# - no chunk starts that could not end by the deadline (a partial run, exit 0; the beacon says partial and
#   keeps the last success time);
# - nothing is trimmed while IO pressure is high (waits, then gives up: exit 3, also at the deadline) or
#   unreadable (exit 4); a failed fstrim is exit 5; every argument must be a mount point (exit 1, nothing
#   trimmed); the lock (exit 7), the stop file and bad settings (exit 2);
# - SIGTERM waits for the in-flight fstrim and exits 143;
# - with STATE_DIR: a run the deadline cuts every night still reaches the tail (the cursor, also kept on a
#   failure; a pass spans runs and completes at the last chunk), a cursor for another geometry starts over,
#   the least recently completed
#   mount point goes first, and a partial run exits 9 once a mount point has had no complete pass for
#   COMPLETE_MAX_AGE_S; no rest after the run's last chunk.
#
# Run: bash scripts/tests/test-node-fstrim.sh   (Linux, WSL or Git Bash with flock)
set -u

SCRIPT="$(cd "$(dirname "$0")/../.." && pwd)/kubernetes/apps/node-fstrim/node-fstrim.sh"
[ -f "$SCRIPT" ] || { echo "cannot find $SCRIPT"; exit 1; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
has()  { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2', got: $3" ;; esac; }
calls(){ [ -f "$CTL/$1" ] && wc -l < "$CTL/$1" | tr -d ' ' || echo 0; }
metric(){ sed -nE "s|^$1 (.*)$|\1|p" "$CTL/textfile/node_fstrim.prom" 2>/dev/null; } # | : labels hold paths

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"; mkdir -p "$BIN"
GiB=1073741824

cat > "$BIN/mountpoint" <<'STUB'
#!/usr/bin/env bash
# mountpoint -q <dir>: a mount point when listed in $CTL/mounts
grep -qxF "${@: -1}" "$CTL/mounts"
STUB
cat > "$BIN/stat" <<'STUB'
#!/usr/bin/env bash
# stat -f -c '%b %S' <dir>: <blocks> 4096 from $CTL/blocks.<dir with / as _>
f="$CTL/blocks.$(echo "${@: -1}" | tr / _)"
[ -f "$f" ] || exit 1
echo "$(cat "$f") 4096"
STUB
cat > "$BIN/fstrim" <<'STUB'
#!/usr/bin/env bash
# Records its arguments; takes $CTL/trim_s seconds of the fake clock; fails from call $CTL/fail_on on.
echo "$*" >> "$CTL/fstrim_calls"
n=$(wc -l < "$CTL/fstrim_calls")
echo $(( $(cat "$CTL/clock") + $(cat "$CTL/trim_s" 2>/dev/null || echo 0) )) > "$CTL/clock"
if [ -f "$CTL/slow_trim" ]; then trap '' TERM; touch "$CTL/fstrim_running"; /bin/sleep 2; touch "$CTL/fstrim_done"; fi
if [ -f "$CTL/fail_on" ] && [ "$n" -ge "$(cat "$CTL/fail_on")" ]; then echo "fstrim: ${@: -1}: FITRIM ioctl failed: Input/output error" >&2; exit 1; fi
echo "${@: -1}: 1 GiB (1073741824 bytes) trimmed"
STUB
cat > "$BIN/sleep" <<'STUB'
#!/usr/bin/env bash
echo "$1" >> "$CTL/sleep_args"
echo $(( $(cat "$CTL/clock") + $1 )) > "$CTL/clock"
n=$(wc -l < "$CTL/sleep_args")
if [ -f "$CTL/psi_drop_after" ] && [ "$n" -ge "$(cat "$CTL/psi_drop_after")" ]; then
  echo "some avg10=5.00 avg60=5.00 avg300=5.00 total=1" > "$CTL/psi"
fi
exit 0
STUB
cat > "$BIN/mktemp" <<'STUB'
#!/usr/bin/env bash
# state_write_fail: every temp file in a STATE_DIR (path .../state/...) fails, as on a full or read-only filesystem.
if [ -f "$CTL/state_write_fail" ]; then for a in "$@"; do case "$a" in */state/*) exit 1 ;; esac; done; fi
exec /usr/bin/mktemp "$@"
STUB
cat > "$BIN/date" <<'STUB'
#!/usr/bin/env bash
if [ "$*" = "+%s" ]; then cat "$CTL/clock"; else exec /bin/date "$@"; fi
STUB
chmod +x "$BIN"/*

new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL/textfile"
  echo 1000 > "$CTL/clock"
  printf '/host/var\n/workspace\n' > "$CTL/mounts"
  echo $(( 10 * GiB / 4096 )) > "$CTL/blocks._host_var"     # 10 GiB by statfs
  echo $(( 8 * GiB / 4096 )) > "$CTL/blocks._workspace"     # 8 GiB by statfs
  echo "some avg10=3.00 avg60=3.00 avg300=3.00 total=1" > "$CTL/psi"
}
run() { # [VAR=value ...] -- <mount>...
  local envs=()
  while [ $# -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
  shift
  env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" LOCK_FILE="$CTL/lock" TMPDIR="$CTL" \
    TEXTFILE_DIR="$CTL/textfile" "${envs[@]}" bash "$SCRIPT" "$@" > "$CTL/out" 2>&1
  echo $? > "$CTL/rc"
}

echo "== every byte is covered: statfs chunks, the last one without -l, -m 64K =="
new_case cover; run -- /host/var
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "three chunks for 10 GiB at 4 GiB" 3 "$(calls fstrim_calls)"
eq "chunk 1" "-v -m 64K -o 0 -l $((4 * GiB)) /host/var" "$(sed -n 1p "$CTL/fstrim_calls")"
eq "chunk 2" "-v -m 64K -o $((4 * GiB)) -l $((4 * GiB)) /host/var" "$(sed -n 2p "$CTL/fstrim_calls")"
eq "the last chunk runs to the real end (no -l)" "-v -m 64K -o $((8 * GiB)) /host/var" "$(sed -n 3p "$CTL/fstrim_calls")"
eq "reason done" 1 "$(metric 'node_fstrim_last_run_reason\{reason="done"\}')"
eq "complete" 1 "$(metric 'node_fstrim_complete\{mountpoint="/host/var"\}')"
eq "trimmed bytes as fstrim reported them" $((3 * GiB)) "$(metric 'node_fstrim_trimmed_bytes\{mountpoint="/host/var"\}')"
eq "a success time (two 2 s rests, none after the last chunk)" 1004 "$(metric node_fstrim_last_success_timestamp_seconds)"

echo "== an exact multiple of the chunk: the second and last chunk has no -l =="
new_case exact; run -- /workspace
eq "two chunks for 8 GiB" 2 "$(calls fstrim_calls)"
eq "the last one" "-v -m 64K -o $((4 * GiB)) /workspace" "$(sed -n 2p "$CTL/fstrim_calls")"

echo "== a filesystem smaller than one chunk: one call, no -l =="
new_case small; echo $(( GiB / 4096 )) > "$CTL/blocks._workspace"; run -- /workspace
eq "one whole-filesystem call" "-v -m 64K -o 0 /workspace" "$(cat "$CTL/fstrim_calls")"

echo "== two mount points, in order, both complete =="
new_case two; run -- /workspace /host/var
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "2 + 3 chunks" 5 "$(calls fstrim_calls)"
has "the first argument first" "/workspace" "$(sed -n 1p "$CTL/fstrim_calls")"
has "then the second" "/host/var" "$(sed -n 3p "$CTL/fstrim_calls")"
eq "both complete" "1 1" "$(metric 'node_fstrim_complete\{mountpoint="/workspace"\}') $(metric 'node_fstrim_complete\{mountpoint="/host/var"\}')"

echo "== the rest after a chunk is as long as the chunk took, within PAUSE_MIN_S..PAUSE_MAX_S =="
new_case restfast; echo 0 > "$CTL/trim_s"; run -- /workspace
eq "a chunk that returned at once rests PAUSE_MIN_S (2); none after the run's last chunk" "2" "$(tr '\n' ' ' < "$CTL/sleep_args" | sed 's/ $//')"
new_case restmid; echo 11 > "$CTL/trim_s"; run -- /workspace
eq "an 11 s chunk rests 11 s" "11" "$(tr '\n' ' ' < "$CTL/sleep_args" | sed 's/ $//')"
new_case restcap; echo 100 > "$CTL/trim_s"; run RUN_MAX_S=9999 -- /workspace
eq "a 100 s chunk rests PAUSE_MAX_S (30)" "30" "$(tr '\n' ' ' < "$CTL/sleep_args" | sed 's/ $//')"
new_case resttwo; echo 0 > "$CTL/trim_s"; run -- /workspace /host/var
eq "two mount points: a rest after every chunk but the run's last (2 + 3 chunks, 4 rests)" 4 "$(calls sleep_args)"

echo "== the deadline: no chunk starts that could not end by it; partial, exit 0 =="
# RUN_MAX_S 300; a chunk is costed at 4 GiB x 30 s = 120 s; each takes 100 s and rests 30 s:
# chunk 1 at +0 (120 <= 300), chunk 2 at +130 (250 <= 300), chunk 3 at +260 (380 > 300): stop.
new_case deadline; echo 100 > "$CTL/trim_s"
printf 'node_fstrim_last_success_timestamp_seconds 555\n' > "$CTL/textfile/node_fstrim.prom"
run RUN_MAX_S=300 -- /host/var /workspace
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "two chunks" 2 "$(calls fstrim_calls)"
eq "the second mount point is not reached" 0 "$(grep -c /workspace "$CTL/fstrim_calls")"
eq "reason partial" 1 "$(metric 'node_fstrim_last_run_reason\{reason="partial"\}')"
eq "not complete" 0 "$(metric 'node_fstrim_complete\{mountpoint="/host/var"\}')"
eq "the last success time is kept" 555 "$(metric node_fstrim_last_success_timestamp_seconds)"
has "says why" "the next run starts there" "$(cat "$CTL/out")"

echo "== IO pressure: waits it out, gives up (3), counts the deadline as giving up, refuses blind (4) =="
new_case wait; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"; echo 2 > "$CTL/psi_drop_after"
run -- /workspace
eq "waited, then trimmed: exit 0" 0 "$(cat "$CTL/rc")"
eq "both chunks" 2 "$(calls fstrim_calls)"
eq "waited PAUSE_MAX_S at a time" "30 30" "$(head -2 "$CTL/sleep_args" | tr '\n' ' ' | sed 's/ $//')"
new_case giveup; echo "some avg10=45.01 avg60=45.00 avg300=40.00 total=1" > "$CTL/psi"; run PSI_WAIT_MAX_S=60 -- /workspace
eq "exit 3 (45.01 is above 45)" 3 "$(cat "$CTL/rc")"
eq "nothing trimmed" 0 "$(calls fstrim_calls)"
eq "reason failed" 1 "$(metric 'node_fstrim_last_run_reason\{reason="failed"\}')"
new_case psideadline; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"
run RUN_MAX_S=200 PSI_WAIT_MAX_S=9999 -- /workspace
eq "pressure until the deadline: exit 3" 3 "$(cat "$CTL/rc")"
has "says so" "until the deadline" "$(cat "$CTL/out")"
new_case nopsi; : > "$CTL/psi"; run -- /workspace
eq "unreadable pressure: exit 4" 4 "$(cat "$CTL/rc")"
eq "nothing trimmed blind" 0 "$(calls fstrim_calls)"

echo "== a failed fstrim fails the run (5) =="
new_case trimfail; echo 2 > "$CTL/fail_on"; run -- /host/var
eq "exit 5" 5 "$(cat "$CTL/rc")"
eq "stops at the failing chunk" 2 "$(calls fstrim_calls)"
has "says which" "chunk 2/3" "$(cat "$CTL/out")"

echo "== every argument must be a mount point with a readable size, before anything is trimmed (1) =="
new_case notmnt; run -- /workspace /not-mounted
eq "exit 1" 1 "$(cat "$CTL/rc")"
eq "not even the first one is trimmed" 0 "$(calls fstrim_calls)"
new_case nosize; rm "$CTL/blocks._workspace"; run -- /workspace
eq "unreadable size: exit 1" 1 "$(cat "$CTL/rc")"

echo "== STATE_DIR: a run the deadline cuts every night still reaches the tail (the cursor) =="
# One chunk fits a night: RUN_MAX_S 200, a chunk is costed at 120 s, each takes 100 s and rests 30 s.
new_case nights; mkdir "$CTL/state"; echo 100 > "$CTL/trim_s"
for night in 1 2 3 4; do
  run STATE_DIR="$CTL/state" RUN_MAX_S=200 -- /host/var
  echo "$(cat "$CTL/rc"):$(sed -nE 's/^node_fstrim_last_run_reason\{reason="([a-z]+)"\} 1$/\1/p' "$CTL/textfile/node_fstrim.prom")" >> "$CTL/nights"
  [ "$night" = 1 ] && cp "$CTL/state/_host_var.cursor" "$CTL/cursor-after-1"
  echo $(( $(cat "$CTL/clock") + 86400 )) > "$CTL/clock"
done
eq "one chunk a night, in order, the third to the real end, then round again" \
  "-o 0 -l $((4 * GiB))|-o $((4 * GiB)) -l $((4 * GiB))|-o $((8 * GiB))|-o 0 -l $((4 * GiB))" \
  "$(sed -E 's/^-v -m 64K //; s| /host/var$||' "$CTL/fstrim_calls" | tr '\n' '|' | sed 's/|$//')"
eq "exit and reason per night" "0:partial 0:partial 0:done 0:partial" "$(tr '\n' ' ' < "$CTL/nights" | sed 's/ $//')"
eq "the cursor after night 1" "next=1 chunks=3 size=$((10 * GiB)) chunk=$((4 * GiB))" "$(cat "$CTL/cursor-after-1")"
[ -s "$CTL/state/_host_var.complete" ] && ok "the complete pass is recorded" || bad "no .complete after night 3"
eq "the beacon carries it" "$(cat "$CTL/state/_host_var.complete")" "$(metric 'node_fstrim_last_complete_timestamp_seconds\{mountpoint="/host/var"\}')"

echo "== STATE_DIR: a run continues the pass at the cursor; one for another geometry, or garbage, starts over =="
new_case resume; mkdir "$CTL/state"
echo "next=2 chunks=3 size=$((10 * GiB)) chunk=$((4 * GiB))" > "$CTL/state/_host_var.cursor"
run STATE_DIR="$CTL/state" -- /host/var
eq "only chunk 3, to the end: the pass begun in earlier runs is complete" "-v -m 64K -o $((8 * GiB)) /host/var" "$(cat "$CTL/fstrim_calls")"
eq "reason done" 1 "$(metric 'node_fstrim_last_run_reason\{reason="done"\}')"
eq "the cursor is gone" absent "$([ -e "$CTL/state/_host_var.cursor" ] && echo present || echo absent)"
run STATE_DIR="$CTL/state" -- /host/var
has "the next run starts a new pass at chunk 1" "-o 0 -l" "$(sed -n 2p "$CTL/fstrim_calls")"
for cur in "next=2 chunks=3 size=$((12 * GiB)) chunk=$((4 * GiB))" "next=9 chunks=3 size=$((10 * GiB)) chunk=$((4 * GiB))" "garbage"; do
  new_case badcursor; mkdir "$CTL/state"; echo "$cur" > "$CTL/state/_host_var.cursor"
  run STATE_DIR="$CTL/state" -- /host/var
  eq "cursor '${cur:0:20}...' -> starts at chunk 1" "-v -m 64K -o 0 -l $((4 * GiB)) /host/var" "$(sed -n 1p "$CTL/fstrim_calls")"
done
has "and says so" "does not fit this filesystem" "$(cat "$CTL/out")"

echo "== STATE_DIR: the least recently completed mount point goes first =="
new_case order; mkdir "$CTL/state"; echo 900 > "$CTL/state/_workspace.complete"
run STATE_DIR="$CTL/state" -- /workspace /host/var
has "/host/var (never completed) before /workspace" "/host/var" "$(sed -n 1p "$CTL/fstrim_calls")"
new_case order2; mkdir "$CTL/state"; echo 900 > "$CTL/state/_workspace.complete"; echo 950 > "$CTL/state/_host_var.complete"
run STATE_DIR="$CTL/state" -- /host/var /workspace
has "/workspace (completed longer ago) first" "/workspace" "$(sed -n 1p "$CTL/fstrim_calls")"

echo "== STATE_DIR: a partial run exits 9 once a mount point has had no complete pass for COMPLETE_MAX_AGE_S =="
new_case stale; mkdir "$CTL/state"; echo 100 > "$CTL/trim_s"; echo 1795000000 > "$CTL/clock"
echo $(( 1795000000 - 8 * 86400 )) > "$CTL/state/_host_var.since"
run STATE_DIR="$CTL/state" RUN_MAX_S=200 -- /host/var
eq "first seen 8 days ago, never complete: exit 9" 9 "$(cat "$CTL/rc")"
eq "reason stale" 1 "$(metric 'node_fstrim_last_run_reason\{reason="stale"\}')"
eq "it still made progress (the cursor moved)" "next=1" "$(cut -d' ' -f1 "$CTL/state/_host_var.cursor")"
new_case stale2; mkdir "$CTL/state"; echo 100 > "$CTL/trim_s"; echo 1795000000 > "$CTL/clock"
echo $(( 1795000000 - 8 * 86400 )) > "$CTL/state/_host_var.complete"
run STATE_DIR="$CTL/state" RUN_MAX_S=200 -- /host/var
eq "last complete 8 days ago: exit 9" 9 "$(cat "$CTL/rc")"
new_case fresh; mkdir "$CTL/state"; echo 100 > "$CTL/trim_s"
run STATE_DIR="$CTL/state" RUN_MAX_S=200 -- /host/var
eq "a first partial run: exit 0" 0 "$(cat "$CTL/rc")"
[ -s "$CTL/state/_host_var.since" ] && ok "and it records when the mount point was first seen" || bad "no .since"
new_case stale3; mkdir "$CTL/state"; echo 1795000000 > "$CTL/clock"; echo $(( 1795000000 - 8 * 86400 )) > "$CTL/state/_host_var.since"
run STATE_DIR="$CTL/state" -- /host/var
eq "a complete run is never stale: exit 0" 0 "$(cat "$CTL/rc")"
eq "and the .since is gone" absent "$([ -e "$CTL/state/_host_var.since" ] && echo present || echo absent)"
new_case nostate; echo 100 > "$CTL/trim_s"; run RUN_MAX_S=200 -- /host/var
eq "without STATE_DIR a partial run exits 0 (stateless)" 0 "$(cat "$CTL/rc")"

echo "== STATE_DIR: a failed chunk keeps the cursor at it; an unusable STATE_DIR refuses (1) =="
new_case cursorfail; mkdir "$CTL/state"; echo 2 > "$CTL/fail_on"; run STATE_DIR="$CTL/state" -- /host/var
eq "exit 5" 5 "$(cat "$CTL/rc")"
eq "cursor at the failed chunk" "next=1" "$(cut -d' ' -f1 "$CTL/state/_host_var.cursor")"
new_case nodir; run STATE_DIR="$CTL/missing" -- /workspace
eq "a missing STATE_DIR: exit 1" 1 "$(cat "$CTL/rc")"
eq "nothing trimmed" 0 "$(calls fstrim_calls)"

echo "== STATE_DIR: a state write that fails fails the run (exit 6); the cursor goes only after .complete =="
new_case statefail; mkdir "$CTL/state"; echo 100 > "$CTL/trim_s"; touch "$CTL/state_write_fail"
run STATE_DIR="$CTL/state" RUN_MAX_S=200 -- /host/var
eq "a partial night whose cursor cannot be written: exit 6, not 0" 6 "$(cat "$CTL/rc")"
eq "reason failed" 1 "$(metric 'node_fstrim_last_run_reason\{reason="failed"\}')"
has "says why" "a write to $CTL/state failed" "$(cat "$CTL/out")"
new_case completefail; mkdir "$CTL/state"; echo "next=2 chunks=3 size=$((10 * GiB)) chunk=$((4 * GiB))" > "$CTL/state/_host_var.cursor"
touch "$CTL/state_write_fail"; run STATE_DIR="$CTL/state" -- /host/var
eq "the last chunk trimmed but .complete not written: exit 6" 6 "$(cat "$CTL/rc")"
eq "and the cursor is kept, not lost with it" "next=2" "$(cut -d' ' -f1 "$CTL/state/_host_var.cursor")"
eq "no .complete" absent "$([ -e "$CTL/state/_host_var.complete" ] && echo present || echo absent)"
new_case statefail5; mkdir "$CTL/state"; echo 1 > "$CTL/fail_on"; touch "$CTL/state_write_fail"
run STATE_DIR="$CTL/state" -- /host/var
eq "a failed fstrim keeps its own exit code (5)" 5 "$(cat "$CTL/rc")"

echo "== the stop file, the lock, bad settings, usage =="
new_case stop; touch "$CTL/stop"; run -- /workspace
eq "stop file: exit 0" 0 "$(cat "$CTL/rc")"
eq "stop file: nothing trimmed" 0 "$(calls fstrim_calls)"
eq "reason stopped" 1 "$(metric 'node_fstrim_last_run_reason\{reason="stopped"\}')"
new_case locked
( exec 9>"$CTL/lock"; flock 9; touch "$CTL/locked"; /bin/sleep 5 ) &
holder=$!
for _ in $(seq 1 50); do [ -f "$CTL/locked" ] && break; /bin/sleep 0.1; done
run -- /workspace
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
eq "lock held: exit 7" 7 "$(cat "$CTL/rc")"
eq "lock held: nothing trimmed" 0 "$(calls fstrim_calls)"
for setting in CHUNK_GB=0 CHUNK_GB=x MINLEN=64X PAUSE_MIN_S=0 PAUSE_MAX_S=1 PSI_MAX=101 PSI_WAIT_MAX_S=-1 RUN_MAX_S=x SEC_PER_GB=0 COMPLETE_MAX_AGE_S=0; do
  new_case "set${setting%%=*}"; run "$setting" -- /workspace
  eq "$setting -> exit 2" 2 "$(cat "$CTL/rc")"
  eq "$setting -> nothing trimmed" 0 "$(calls fstrim_calls)"
done
new_case usage; run --
eq "no mount point: exit 2" 2 "$(cat "$CTL/rc")"
new_case nobeacon; run TEXTFILE_DIR= -- /workspace
eq "no TEXTFILE_DIR: no beacon" absent "$([ -e "$CTL/textfile/node_fstrim.prom" ] && echo present || echo absent)"

echo "== SIGTERM while an uninterruptible fstrim runs: waits for it, exit 143 =="
new_case sigterm; touch "$CTL/slow_trim"
env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" LOCK_FILE="$CTL/lock" TMPDIR="$CTL" \
  TEXTFILE_DIR="$CTL/textfile" bash "$SCRIPT" /workspace > "$CTL/out" 2>&1 &
spid=$!
for _ in $(seq 1 100); do [ -f "$CTL/fstrim_running" ] && break; /bin/sleep 0.1; done
kill -TERM "$spid" $(pgrep -P "$spid") 2>/dev/null
wait "$spid"; echo $? > "$CTL/rc"
eq "exit 143" 143 "$(cat "$CTL/rc")"
eq "the in-flight fstrim finished first" present "$([ -e "$CTL/fstrim_done" ] && echo present || echo absent)"
eq "only that chunk ran" 1 "$(calls fstrim_calls)"
eq "reason terminated" 1 "$(metric 'node_fstrim_last_run_reason\{reason="terminated"\}')"

echo
echo "node-fstrim: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
