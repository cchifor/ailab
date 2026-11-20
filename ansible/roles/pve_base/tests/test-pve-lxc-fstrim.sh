#!/usr/bin/env bash
# Tests for files/pve-lxc-fstrim.sh, the nightly sweep that trims every running container's local-lvm
# volumes on a Proxmox host through scripts/lxc-fstrim-chunked.sh (tested on its own by
# scripts/tests/test-lxc-fstrim-chunked.sh).
#
# The sweep runs for real with a stubbed `pct`, a stubbed inner script and a fake clock (`date +%s`). The
# invariants:
# - only RUNNING containers without a lock, minus the exclude list; only their rootfs and mpN volumes on
#   the listed storages; never a bind mount, a read-only mount point, an unused disk or a snapshot's copy;
# - one volume at a time, in config order, each with DEADLINE = start + the run budget; a container that
#   stopped (or took a lock) since the listing is skipped;
# - the deadline, an inner exit 8 or 7, and the stop file make a partial pass (exit 0); an inner failure
#   makes the sweep fail (exit 1) but the other volumes are still trimmed;
# - the beacon: per-volume exit code, bytes and last complete pass, the total, and a last-success time that
#   is the oldest of the volumes' last complete passes (carried over while one has never completed);
# - progress across nights: a CURSOR_FILE per volume in the state dir, a .complete only when the inner script
#   reached the last chunk (exit 0, no cursor left), volumes least recently completed first, so a run the
#   deadline cuts every night still reaches every volume;
# - a failed late read of a container's config skips that volume as a failure (never assumed unlocked);
# - the inner script's lines reach the sweep's output (the journal) while the volume is still in flight;
# - the lock (exit 7, no beacon), bad settings (exit 2), a stop file already there at the start (exit 3), an
#   unusable state dir (exit 1), and SIGTERM to the unit's processes (waits for the volume in flight, exit 143).
#
# Run: bash ansible/roles/pve_base/tests/test-pve-lxc-fstrim.sh   (Linux; bash, flock, awk, sed)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../files/pve-lxc-fstrim.sh"
[ -f "$SCRIPT" ] || { echo "cannot find $SCRIPT"; exit 1; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
has()  { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2', got: $3" ;; esac; }
hasnt(){ case "$3" in *"$2"*) bad "$1" "expected NOT to contain '$2', got: $3" ;; *) ok "$1" ;; esac; }
calls(){ [ -f "$CTL/inner_calls" ] && cut -d' ' -f1,2 "$CTL/inner_calls" | tr '\n' ',' | sed 's/,$//' || echo ""; }
metric(){ sed -nE "s/^$1 (.*)$/\1/p" "$CTL/textfile/pve_lxc_fstrim.prom" 2>/dev/null; }
tree() { local p; echo "$1"; for p in $(pgrep -P "$1"); do tree "$p"; done; } # a pid and all its descendants

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"; mkdir -p "$BIN"

# --- stubs (all read their behaviour from $CTL) ---------------------------------------------------
cat > "$BIN/pct" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  list) [ -f "$CTL/list_fail" ] && exit 1; cat "$CTL/pct_list" ;;
  config)
    [ -f "$CTL/config.$2" ] || exit 2
    n=$(( $(cat "$CTL/reads.$2" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CTL/reads.$2"
    [ -f "$CTL/config.$2.fail_from" ] && [ "$n" -ge "$(cat "$CTL/config.$2.fail_from")" ] && exit 2
    if [ "$n" -ge 2 ] && [ -f "$CTL/config.$2.late" ]; then cat "$CTL/config.$2.late"; else cat "$CTL/config.$2"; fi ;;
  status) if [ -f "$CTL/status.$2" ]; then cat "$CTL/status.$2"; else echo "status: running"; fi ;;
  *) exit 2 ;;
esac
STUB
cat > "$BIN/inner" <<'STUB'
#!/usr/bin/env bash
# The inner script: records its call and the settings it inherited, takes $CTL/trim_s of the fake clock,
# prints its end line and exits with $CTL/rc.<ct>.<key> (default 0).
echo "$1 $2 DEADLINE=${DEADLINE:-} CHUNK_GB=${CHUNK_GB:-} PAUSE_IDLE_S=${PAUSE_IDLE_S:-} PSI_MAX=${PSI_MAX:-} STOP_FILE=${STOP_FILE:-} CURSOR_FILE=${CURSOR_FILE:-}" >> "$CTL/inner_calls"
# hold: print a chunk line, then wait for $CTL/release (the live-output case)
if [ -f "$CTL/hold" ]; then
  echo "lxc-fstrim-chunked[$1/$2]: chunk=1/3 off=0 (in flight)"
  for _ in $(seq 1 100); do [ -f "$CTL/release" ] && break; /bin/sleep 0.1; done
fi
[ -f "$CTL/clock" ] && echo $(( $(cat "$CTL/clock") + $(cat "$CTL/trim_s" 2>/dev/null || echo 0) )) > "$CTL/clock"
if [ -f "$CTL/block" ]; then
  # an in-flight trim that finishes its chunk (1 s) when terminated, then exits 143
  trap 'sleep 1; echo "inner: terminated, mount released"; touch "$CTL/inner_released"; exit 143' TERM
  touch "$CTL/inner_running"; sleep 30 & wait
fi
[ -f "$CTL/touch_stop_on.$1.$2" ] && touch "$CTL/stop"
echo "lxc-fstrim-chunked[$1/$2]: end: trimmed_total=$(cat "$CTL/bytes.$1.$2" 2>/dev/null || echo 1024) pool=50%"
rc="$(cat "$CTL/rc.$1.$2" 2>/dev/null || echo 0)"
# The cursor protocol of lxc-fstrim-chunked.sh: kept when the pass stopped short (the deadline, or a stop file:
# leave_cursor), removed when it reached the last chunk.
if [ -n "${CURSOR_FILE:-}" ]; then
  if [ "$rc" = 8 ] || [ -f "$CTL/leave_cursor.$1.$2" ]; then echo "next=1 chunks=3 size=1 chunk=1" > "$CTL/state-cursor-written.$1.$2"; cp "$CTL/state-cursor-written.$1.$2" "$CURSOR_FILE"
  elif [ "$rc" = 0 ]; then rm -f "$CURSOR_FILE"; fi
fi
exit "$rc"
STUB
cat > "$BIN/date" <<'STUB'
#!/usr/bin/env bash
if [ "$*" = "+%s" ] && [ -f "$CTL/clock" ]; then cat "$CTL/clock"; else exec /bin/date "$@"; fi
STUB
chmod +x "$BIN"/*

# A host like ai-node2: 5002 running with every kind of entry, 5003 stopped, 5004 running but excluded,
# 5005 running but locked by a backup, 5006 running with only a rootfs.
new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL/textfile"
  echo 1000 > "$CTL/clock"
  cat > "$CTL/pct_list" <<'EOF'
VMID       Status     Lock         Name
5002       running                 ai-llm-2
5003       stopped                 ai-llm-3
5004       running                 registry
5005       running    backup       backup-me
5006       running                 small
EOF
  cat > "$CTL/config.5002" <<'EOF'
arch: amd64
cores: 16
hostname: ai-llm-2
mp0: /mnt/pve/qnap-nfs/models,mp=/models
mp1: local-lvm:vm-5002-disk-1,mp=/models-local,size=160G
mp2: qnap-iscsi:vm-5002-disk-3,mp=/scratch,size=10G
mp3: local-lvm:vm-5002-disk-4,mp=/ro-data,ro=1,size=8G
rootfs: local-lvm:vm-5002-disk-0,size=24G
unused0: local-lvm:vm-5002-disk-2

[pre-upgrade]
mp4: local-lvm:vm-5002-disk-9,mp=/old,size=8G
rootfs: local-lvm:vm-5002-disk-8,size=24G
EOF
  printf 'hostname: ai-llm-3\nrootfs: local-lvm:vm-5003-disk-0,size=24G\n' > "$CTL/config.5003"
  printf 'hostname: registry\nmp0: local-lvm:vm-5004-disk-1,mp=/var/lib/registry,size=384G\nrootfs: local-lvm:vm-5004-disk-0,size=16G\n' > "$CTL/config.5004"
  printf 'hostname: backup-me\nlock: backup\nrootfs: local-lvm:vm-5005-disk-0,size=8G\n' > "$CTL/config.5005"
  printf 'hostname: small\nrootfs: local-lvm:vm-5006-disk-0,size=8G\n' > "$CTL/config.5006"
}
run() { # extra VAR=value settings may be passed as arguments
  env PATH="$BIN:$PATH" PVE_LXC_FSTRIM_ENV_FILE=/dev/null PVE_LXC_FSTRIM_INNER="$BIN/inner" \
    PVE_LXC_FSTRIM_LOCK_FILE="$CTL/lock" PVE_LXC_FSTRIM_TEXTFILE_DIR="$CTL/textfile" STOP_FILE="$CTL/stop" \
    PVE_LXC_FSTRIM_STATE_DIR="$CTL/state" \
    PVE_LXC_FSTRIM_EXCLUDE=5004 PVE_LXC_FSTRIM_RUN_MAX_S=780 CHUNK_GB=4 SEC_PER_GB=30 TMPDIR="$CTL" \
    "$@" bash "$SCRIPT" > "$CTL/out" 2>&1
  echo $? > "$CTL/rc"
}

echo "== which volumes: running, unlocked, not excluded; rootfs and mpN on local-lvm only =="
new_case select; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "5002's mp1 and rootfs (config order), then 5006's rootfs" "5002 mp1,5002 rootfs,5006 rootfs" "$(calls)"
out="$(cat "$CTL/out")"
has "the bind mount is skipped, and said so" "5002 mp0: bind mount /mnt/pve/qnap-nfs/models - skipped" "$out"
has "another storage is skipped" "5002 mp2: qnap-iscsi:vm-5002-disk-3 not on local-lvm - skipped" "$out"
has "a read-only mount point is skipped" "5002 mp3: read-only mount point - skipped" "$out"
has "the excluded container is skipped" "ct 5004 excluded" "$out"
has "a locked container is skipped" "ct 5005 holds lock 'backup'" "$out"
hasnt "the stopped container is never looked at" "5003" "$(calls)"
hasnt "the unused disk is never trimmed" "disk-2" "$(cat "$CTL/inner_calls")"
hasnt "a snapshot's entries are never trimmed" "mp4" "$(calls)"
has "each volume gets DEADLINE = start + the budget (1000 + 780)" "DEADLINE=1780" "$(sed -n 1p "$CTL/inner_calls")"
has "the inner script inherits CHUNK_GB" "CHUNK_GB=4" "$(sed -n 1p "$CTL/inner_calls")"
has "and the shared stop file" "STOP_FILE=$CTL/stop" "$(sed -n 1p "$CTL/inner_calls")"

echo "== a locked container is a partial pass, an unlocked host a complete one =="
eq "a lock makes it partial" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="partial"\}')"
eq "the success time covers the volumes found (the locked container is not among them)" 1000 "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)"
new_case complete; printf 'hostname: backup-me\nrootfs: local-lvm:vm-5005-disk-0,size=8G\n' > "$CTL/config.5005"
sed -i 's/ backup       backup-me/              backup-me/' "$CTL/pct_list"
echo 2048 > "$CTL/bytes.5002.mp1"; echo 30 > "$CTL/trim_s"
run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "four volumes" "5002 mp1,5002 rootfs,5005 rootfs,5006 rootfs" "$(calls)"
eq "reason done" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="done"\}')"
eq "success time = the oldest volume's complete pass (the first, at 1000 + 30 s)" 1030 "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)"
eq "per volume: its last complete pass" 1120 "$(metric 'pve_lxc_fstrim_volume_last_complete_timestamp_seconds\{ctid="5006",volume="rootfs"\}')"
has "each volume gets its own cursor file in the state dir" "CURSOR_FILE=$CTL/state/5002-mp1.cursor" "$(sed -n 1p "$CTL/inner_calls")"
eq "the total is the sum of the volumes' trimmed_total" $((2048 + 3 * 1024)) "$(metric pve_lxc_fstrim_trimmed_bytes)"
eq "volumes found" 4 "$(metric pve_lxc_fstrim_volumes)"
eq "per-volume bytes" 2048 "$(metric 'pve_lxc_fstrim_volume_trimmed_bytes\{ctid="5002",volume="mp1"\}')"
eq "per-volume exit code" 0 "$(metric 'pve_lxc_fstrim_volume_exit_code\{ctid="5006",volume="rootfs"\}')"
eq "run duration" 120 "$(metric pve_lxc_fstrim_last_run_duration_seconds)"
bad_lines="$(grep -vE '^(# (HELP|TYPE) pve_lxc_fstrim_[a-z_]+ .+|pve_lxc_fstrim_[a-z_]+(\{[a-z]+="[^"]*"(,[a-z]+="[^"]*")*\})? -?[0-9]+)$' "$CTL/textfile/pve_lxc_fstrim.prom")"
eq "every beacon line is a HELP/TYPE comment or a sample" "" "$bad_lines"
eq "no temp file left in the textfile dir" 0 "$(find "$CTL/textfile" -name '.pve_lxc_fstrim*' | wc -l | tr -d ' ')"
eq "file mode 0644 (node_exporter reads it)" 644 "$(stat -c %a "$CTL/textfile/pve_lxc_fstrim.prom")"

echo "== a failed volume fails the sweep, the others are still trimmed, the success time is kept =="
new_case fail; printf 'pve_lxc_fstrim_last_success_timestamp_seconds 12345\n' > "$CTL/textfile/pve_lxc_fstrim.prom"
echo 5 > "$CTL/rc.5002.mp1"; run
eq "exit 1" 1 "$(cat "$CTL/rc")"
eq "the next volumes still ran" "5002 mp1,5002 rootfs,5006 rootfs" "$(calls)"
eq "reason failed" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="failed"\}')"
eq "the volume's exit code" 5 "$(metric 'pve_lxc_fstrim_volume_exit_code\{ctid="5002",volume="mp1"\}')"
eq "the last success is carried over" 12345 "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)"
eq "the sweep's exit code" 1 "$(metric pve_lxc_fstrim_last_run_exit_code)"

echo "== the deadline: no volume starts that could not get one chunk done by it =="
# budget 100 s, a chunk costs 4 x 10 = 40 s, each volume takes 50 s: 1000+40 ok, 1050+40 ok, 1100+40 no
new_case deadline; echo 50 > "$CTL/trim_s"; run PVE_LXC_FSTRIM_RUN_MAX_S=100 SEC_PER_GB=10
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "two volumes, the third waits for the next night" "5002 mp1,5002 rootfs" "$(calls)"
eq "reason partial" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="partial"\}')"
eq "the volume not reached reads -1" -1 "$(metric 'pve_lxc_fstrim_volume_exit_code\{ctid="5006",volume="rootfs"\}')"
eq "no success time" "" "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)"
has "says why" "deadline reached" "$(cat "$CTL/out")"

echo "== the inner script's deadline (exit 8) ends the sweep as partial =="
new_case inner8; echo 8 > "$CTL/rc.5002.mp1"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "nothing after it" "5002 mp1" "$(calls)"
eq "reason partial" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="partial"\}')"

echo "== the shared lock held by the registry's weekly trim (exit 7): skipped, the sweep goes on =="
new_case inner7; echo 7 > "$CTL/rc.5002.mp1"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "the next volumes still ran" "5002 mp1,5002 rootfs,5006 rootfs" "$(calls)"
eq "reason partial" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="partial"\}')"
has "says why" "another trim holds the shared lock" "$(cat "$CTL/out")"

echo "== a container that stopped since the listing is skipped =="
new_case gone; echo "status: stopped" > "$CTL/status.5006"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "5006 not trimmed" "5002 mp1,5002 rootfs" "$(calls)"
eq "reason partial" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="partial"\}')"

echo "== a container that took a lock since the listing (a backup started) is skipped =="
new_case lockedlate
# config.5006 gains a lock after the first read: the stub serves config.5006.late from the 2nd read on
printf 'hostname: small\nlock: backup\nrootfs: local-lvm:vm-5006-disk-0,size=8G\n[snap]\nrootfs: local-lvm:vm-5006-disk-9,size=8G\n' > "$CTL/config.5006.late"
run
eq "5006 not trimmed" "5002 mp1,5002 rootfs" "$(calls)"
has "says why" "now holds a lock (status: running, lock backup)" "$(cat "$CTL/out")"

echo "== the stop file: one already there at the start is a leftover (exit 3); one that appears stops after the volume =="
new_case stop; touch "$CTL/stop"; run
eq "exit 3" 3 "$(cat "$CTL/rc")"
eq "nothing trimmed" "" "$(calls)"
eq "reason stopped" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="stopped"\}')"
has "says it is a leftover and how to clear it" "a leftover? remove it (rm -f $CTL/stop)" "$(cat "$CTL/out")"
new_case stopmid; touch "$CTL/touch_stop_on.5002.mp1"; run
eq "stops after that volume" "5002 mp1" "$(calls)"
eq "reason stopped" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="stopped"\}')"

echo "== nothing to trim is a complete pass =="
new_case none; printf 'VMID       Status     Lock         Name\n' > "$CTL/pct_list"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "reason done" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="done"\}')"
eq "success time" 1000 "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)"

echo "== pct list failing fails the sweep =="
new_case listfail; touch "$CTL/list_fail"; run
eq "exit 1" 1 "$(cat "$CTL/rc")"
eq "nothing trimmed" "" "$(calls)"
eq "reason failed" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="failed"\}')"

echo "== the env file's settings reach the inner script =="
new_case envfile
printf 'PVE_LXC_FSTRIM_EXCLUDE="5002 5004"\nPAUSE_IDLE_S=1\nPSI_MAX=40\n' > "$CTL/env"
run PVE_LXC_FSTRIM_ENV_FILE="$CTL/env"
eq "the file's exclude list" "5006 rootfs" "$(calls)"
has "PAUSE_IDLE_S exported" "PAUSE_IDLE_S=1" "$(cat "$CTL/inner_calls")"
has "PSI_MAX exported" "PSI_MAX=40" "$(cat "$CTL/inner_calls")"

echo "== a second sweep while one holds the lock refuses (exit 7), writes no beacon =="
new_case locked
( exec 9>"$CTL/lock"; flock 9; touch "$CTL/locked"; /bin/sleep 5 ) &
holder=$!
for _ in $(seq 1 50); do [ -f "$CTL/locked" ] && break; /bin/sleep 0.1; done
run
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
eq "exit 7" 7 "$(cat "$CTL/rc")"
eq "nothing trimmed" "" "$(calls)"
eq "no beacon" absent "$([ -e "$CTL/textfile/pve_lxc_fstrim.prom" ] && echo present || echo absent)"

echo "== invalid settings are rejected before anything runs (exit 2) =="
for setting in "PVE_LXC_FSTRIM_RUN_MAX_S=0" "PVE_LXC_FSTRIM_RUN_MAX_S=x" "PVE_LXC_FSTRIM_STORAGES=bad!id" \
  "PVE_LXC_FSTRIM_EXCLUDE=abc" "CHUNK_GB=0" "SEC_PER_GB=-1" "PVE_LXC_FSTRIM_INNER=/nonexistent"; do
  new_case "set${setting%%=*}"; run "$setting"
  eq "$setting -> exit 2" 2 "$(cat "$CTL/rc")"
  eq "$setting -> nothing trimmed" "" "$(calls)"
done

echo "== a failed late read of the config skips the volume as a failure, never as unlocked =="
new_case latefail; echo 2 > "$CTL/config.5006.fail_from"; run
eq "exit 1" 1 "$(cat "$CTL/rc")"
eq "5006 not trimmed" "5002 mp1,5002 rootfs" "$(calls)"
has "says why" "ct 5006: pct config failed - rootfs skipped (its lock could not be checked)" "$(cat "$CTL/out")"
eq "reason failed" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="failed"\}')"

echo "== progress across nights: least recently completed first, so every volume gets its turn =="
# One volume fits a night: budget 100 s, a chunk costs 4 x 10 s, each volume takes 70 s.
new_case nights; echo 70 > "$CTL/trim_s"
for night in 1 2 3 4; do
  run PVE_LXC_FSTRIM_RUN_MAX_S=100 SEC_PER_GB=10
  tail -1 "$CTL/inner_calls" | cut -d' ' -f1,2 >> "$CTL/firsts"
  echo $(( $(cat "$CTL/clock") + 86400 )) > "$CTL/clock"
done
eq "night by night: 5002 mp1, 5002 rootfs, 5006 rootfs, then 5002 mp1 again (the oldest)" \
  "5002 mp1,5002 rootfs,5006 rootfs,5002 mp1" "$(tr '\n' ',' < "$CTL/firsts" | sed 's/,$//')"
eq "one volume a night" 4 "$(wc -l < "$CTL/inner_calls" | tr -d ' ')"
eq "after night 4: success = the oldest last complete pass (night 2's, 5002 rootfs, at 87470 + 70)" 87540 \
  "$(metric pve_lxc_fstrim_last_success_timestamp_seconds)"
new_case order; mkdir -p "$CTL/state"; echo 900 > "$CTL/state/5002-mp1.complete"; echo 950 > "$CTL/state/5002-rootfs.complete"
run
eq "never completed first, then the oldest" "5006 rootfs,5002 mp1,5002 rootfs" "$(calls)"
new_case cursorleft; touch "$CTL/leave_cursor.5002.mp1"; run
eq "a volume left with a cursor (exit 0 at a stop file) is not complete" absent \
  "$([ -e "$CTL/state/5002-mp1.complete" ] && echo present || echo absent)"
eq "the others are" "present present" \
  "$([ -e "$CTL/state/5002-rootfs.complete" ] && echo present) $([ -e "$CTL/state/5006-rootfs.complete" ] && echo present)"
eq "so the pass is partial" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="partial"\}')"
new_case nostate; run PVE_LXC_FSTRIM_STATE_DIR=/proc/nonexistent/state
eq "an unusable state dir: exit 1" 1 "$(cat "$CTL/rc")"
eq "an unusable state dir: nothing trimmed" "" "$(calls)"

echo "== the inner script's lines reach the journal while the volume is in flight =="
new_case live; touch "$CTL/hold"
env PATH="$BIN:$PATH" PVE_LXC_FSTRIM_ENV_FILE=/dev/null PVE_LXC_FSTRIM_INNER="$BIN/inner" \
  PVE_LXC_FSTRIM_LOCK_FILE="$CTL/lock" PVE_LXC_FSTRIM_TEXTFILE_DIR="$CTL/textfile" STOP_FILE="$CTL/stop" \
  PVE_LXC_FSTRIM_STATE_DIR="$CTL/state" PVE_LXC_FSTRIM_EXCLUDE=5004 TMPDIR="$CTL" bash "$SCRIPT" > "$CTL/out" 2>&1 &
lpid=$!
for _ in $(seq 1 50); do grep -q "chunk=1/3 off=0 (in flight)" "$CTL/out" 2>/dev/null && break; /bin/sleep 0.1; done
has "the in-flight chunk line is already in the output" "chunk=1/3 off=0 (in flight)" "$(cat "$CTL/out")"
eq "while the inner script is still running" absent "$(grep -q 'trimmed_total' "$CTL/out" && echo present || echo absent)"
touch "$CTL/release"; wait "$lpid"; echo $? > "$CTL/rc"
eq "then the sweep completes (exit 0)" 0 "$(cat "$CTL/rc")"
eq "and still reads each volume's total from it" $((3 * 1024)) "$(metric pve_lxc_fstrim_trimmed_bytes)"

echo "== SIGTERM while a volume is in flight: waits for it to release its mount, exit 143 =="
new_case sigterm; touch "$CTL/block"; rm -f "$CTL/clock"
env PATH="$BIN:$PATH" PVE_LXC_FSTRIM_ENV_FILE=/dev/null PVE_LXC_FSTRIM_INNER="$BIN/inner" \
  PVE_LXC_FSTRIM_LOCK_FILE="$CTL/lock" PVE_LXC_FSTRIM_TEXTFILE_DIR="$CTL/textfile" STOP_FILE="$CTL/stop" \
  PVE_LXC_FSTRIM_STATE_DIR="$CTL/state" PVE_LXC_FSTRIM_EXCLUDE=5004 TMPDIR="$CTL" bash "$SCRIPT" > "$CTL/out" 2>&1 &
spid=$!
for _ in $(seq 1 100); do [ -f "$CTL/inner_running" ] && break; /bin/sleep 0.1; done
# What systemd's default KillMode=control-group does: SIGTERM to every process of the unit (the sweep, its
# subshell, the inner script and its sleep, and tee, which ignores it).
kill -TERM $(tree "$spid") 2>/dev/null
wait "$spid"; echo $? > "$CTL/rc"
eq "exit 143" 143 "$(cat "$CTL/rc")"
eq "the volume in flight released its mount first" present "$([ -e "$CTL/inner_released" ] && echo present || echo absent)"
has "its last line still reached the journal (tee outlived the TERM)" "inner: terminated, mount released" "$(cat "$CTL/out")"
eq "only that volume ran" "5002 mp1" "$(calls)"
eq "reason terminated" 1 "$(metric 'pve_lxc_fstrim_last_run_reason\{reason="terminated"\}')"

echo
echo "pve-lxc-fstrim: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
