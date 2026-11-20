#!/usr/bin/env bash
# Tests for scripts/lxc-fstrim-chunked.sh: the pressure-guarded, chunked fstrim of an LXC mount point's
# volume (plans/2026-10-07-etcd-leader-churn-plan.md step E; hardened after Codex impl-review round 1).
#
# The host's own fstrim.timer never reaches LXC mount points, so the registry LXC's thin volume on
# ai-node1 grew to 377 GB allocated for 170 GB used. The invariants that matter:
# - no binary from inside the container runs: the volume is mounted on the HOST and the host's fstrim
#   trims it;
# - every block of the filesystem is covered, from ext4's own geometry, not df;
# - nothing is trimmed while host IO pressure is high OR unreadable;
# - a stop file halts it, even during a pressure wait;
# - a failed trim is a failed run, and bad settings or an unknown volume are errors;
# - for the nightly sweep (role pve_base): a rootfs works like an mpN, a snapshot's copy of a key is never
#   used, a DEADLINE stops it cleanly (exit 8) before a chunk that could not end in time, also during a
#   pressure wait, and a chunk that trimmed nothing is followed by the short PAUSE_IDLE_S.
#
# Run: bash scripts/tests/test-lxc-fstrim-chunked.sh   (Linux, WSL or Git Bash)
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/lxc-fstrim-chunked.sh"
[ -f "$SCRIPT" ] || { echo "cannot find $SCRIPT"; exit 1; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
has()  { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2', got: $3" ;; esac; }
calls(){ [ -f "$CTL/$1" ] && wc -l < "$CTL/$1" | tr -d ' ' || echo 0; }

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"; mkdir -p "$BIN"

# --- stubs (all read their behaviour from $CTL; none of them is a container binary) --------------
cat > "$BIN/pct" <<'STUB'
#!/usr/bin/env bash
# pct config <ctid>
[ "$1" = config ] && cat "$CTL/pct_config"
STUB
cat > "$BIN/pvesm" <<'STUB'
#!/usr/bin/env bash
# pvesm path <volid>
[ "$1" = path ] && case "$2" in local-lvm:vm-5004-disk-[0-9]) echo "/dev/pve/${2#local-lvm:}" ;; esac
STUB
cat > "$BIN/stat" <<'STUB'
#!/usr/bin/env bash
# stat -L -c %F <dev>
cat "$CTL/devtype"
STUB
cat > "$BIN/tune2fs" <<'STUB'
#!/usr/bin/env bash
printf 'Block count:              %s\nBlock size:               4096\n' "$(cat "$CTL/blocks")"
STUB
cat > "$BIN/mount" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CTL/mount_calls"
STUB
cat > "$BIN/umount" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CTL/umount_calls"
[ -f "$CTL/umount_fail" ] && { echo "umount: $1: target is busy." >&2; exit 32; }
[ -f "$CTL/fstrim_running" ] && { echo "umount: $1: target is busy (fstrim in flight)." >&2; exit 32; }
exit 0
STUB
cat > "$BIN/fstrim" <<'STUB'
#!/usr/bin/env bash
# fstrim -v -o <bytes> -l <bytes> <dir>; fails on call number $CTL/fail_on if set
echo "$*" >> "$CTL/fstrim_calls"
n=$(wc -l < "$CTL/fstrim_calls")
# fake clock (the DEADLINE cases): each trim takes $CTL/trim_s seconds of it
[ -f "$CTL/clock" ] && echo $(( $(cat "$CTL/clock") + $(cat "$CTL/trim_s") )) > "$CTL/clock"
# slow_trim: an FITRIM that cannot be interrupted (ignores SIGTERM) and holds the mount busy for 2 s
if [ -f "$CTL/slow_trim" ]; then trap '' TERM; touch "$CTL/fstrim_running"; /bin/sleep 2; rm -f "$CTL/fstrim_running"; fi
if [ -f "$CTL/fail_on" ] && [ "$n" -ge "$(cat "$CTL/fail_on")" ]; then echo "fstrim: $5: FITRIM ioctl failed: Input/output error" >&2; exit 1; fi
if [ -f "$CTL/zero_bytes" ]; then echo "$5: 0 B (0 bytes) trimmed"; else echo "$5: 1 GiB (1073741824 bytes) trimmed"; fi
STUB
cat > "$BIN/lvs" <<'STUB'
#!/usr/bin/env bash
echo "  60.00"
STUB
cat > "$BIN/sleep" <<'STUB'
#!/usr/bin/env bash
# Each sleep advances a counter. At $CTL/psi_drop_after, pressure drops to 5;
# at $CTL/stop_after, a stop request appears.
n=$(( $(cat "$CTL/sleeps" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CTL/sleeps"
echo "$1" >> "$CTL/sleep_args"
[ -f "$CTL/clock" ] && echo $(( $(cat "$CTL/clock") + $1 )) > "$CTL/clock"
if [ -f "$CTL/psi_drop_after" ] && [ "$n" -ge "$(cat "$CTL/psi_drop_after")" ]; then
  echo "some avg10=5.00 avg60=5.00 avg300=5.00 total=1" > "$CTL/psi"
fi
if [ -f "$CTL/stop_after" ] && [ "$n" -ge "$(cat "$CTL/stop_after")" ]; then touch "$CTL/stop"; fi
# block_sleep: a REAL blocking sleep, announced via $CTL/sleeping, for the SIGTERM case
if [ -f "$CTL/block_sleep" ]; then touch "$CTL/sleeping"; exec /bin/sleep 30; fi
STUB
cat > "$BIN/date" <<'STUB'
#!/usr/bin/env bash
# The fake clock for `date +%s` when $CTL/clock exists; everything else is the real date.
if [ "$*" = "+%s" ] && [ -f "$CTL/clock" ]; then cat "$CTL/clock"; else exec /bin/date "$@"; fi
STUB
chmod +x "$BIN"/*

GiB=1073741824
new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL/mnt"
  printf 'hostname: registry\nmp0: local-lvm:vm-5004-disk-1,mp=/var/lib/registry,size=384G\nrootfs: local-lvm:vm-5004-disk-0,size=16G\n' > "$CTL/pct_config"
  echo "block special file" > "$CTL/devtype"
  echo $(( 20 * GiB / 4096 )) > "$CTL/blocks"   # 20 GiB of ext4 blocks
  echo "some avg10=3.00 avg60=3.00 avg300=3.00 total=1" > "$CTL/psi"
}
run() { # extra VAR=value settings may be passed as arguments; KEY (default mp0) is the volume
  env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" MOUNT_BASE="$CTL/mnt" LOCK_FILE="$CTL/lock" CHUNK_GB=8 PAUSE_S=1 \
    PSI_MAX=45 PSI_WAIT_MAX_S=5 "$@" bash "$SCRIPT" 5004 "${KEY:-mp0}" > "$CTL/out" 2>&1
  echo $? > "$CTL/rc"
}

echo "== host-side trim of the whole filesystem, from ext4 geometry =="
new_case cover; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "three chunks for 20 GiB of blocks at 8 GiB" 3 "$(calls fstrim_calls)"
has "first chunk at 0, 8 GiB long" "-v -o 0 -l $((8 * GiB)) " "$(sed -n 1p "$CTL/fstrim_calls")"
has "last chunk starts at 16 GiB" "-v -o $((16 * GiB)) -l $((8 * GiB)) " "$(sed -n 3p "$CTL/fstrim_calls")"
has "trims the host-side mount, not a container path" "$CTL/mnt/" "$(sed -n 1p "$CTL/fstrim_calls")"
has "mounts the mp0 volume's device" "/dev/pve/vm-5004-disk-1" "$(cat "$CTL/mount_calls")"
has "mounts it nosuid,nodev,noexec" "nosuid,nodev,noexec" "$(cat "$CTL/mount_calls")"
eq "unmounts afterwards" 1 "$(calls umount_calls)"
has "reports the total trimmed" "trimmed_total=3221225472" "$(cat "$CTL/out")"

echo "== a stop file present at start: nothing trimmed, still unmounted =="
new_case stop; touch "$CTL/stop"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "no chunk trimmed" 0 "$(calls fstrim_calls)"
has "says why" "stop file" "$(cat "$CTL/out")"

echo "== a stop request arriving DURING a pressure wait stops it =="
new_case stopwait; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"; echo 2 > "$CTL/stop_after"; echo 3 > "$CTL/psi_drop_after"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "no chunk trimmed after the stop" 0 "$(calls fstrim_calls)"
has "says why" "stop file" "$(cat "$CTL/out")"

echo "== waits out high host IO pressure, then trims =="
new_case wait; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"; echo 3 > "$CTL/psi_drop_after"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "all three chunks trimmed after the wait" 3 "$(calls fstrim_calls)"
has "logs the wait" "waited=" "$(cat "$CTL/out")"

echo "== gives up (exit 3) when pressure never drops; 45.01 counts as above 45 =="
new_case givesup; echo "some avg10=45.01 avg60=45.00 avg300=40.00 total=1" > "$CTL/psi"; run
eq "exit 3" 3 "$(cat "$CTL/rc")"
eq "nothing trimmed under pressure" 0 "$(calls fstrim_calls)"
has "says why" "IO pressure" "$(cat "$CTL/out")"
eq "unmounts on the way out" 1 "$(calls umount_calls)"

echo "== unreadable IO pressure refuses to trim (exit 4) =="
new_case nopsi; : > "$CTL/psi"; run
eq "exit 4" 4 "$(cat "$CTL/rc")"
eq "nothing trimmed blind" 0 "$(calls fstrim_calls)"

echo "== a failed trim fails the run (exit 5) =="
new_case trimfail; echo 2 > "$CTL/fail_on"; run
eq "exit 5" 5 "$(cat "$CTL/rc")"
eq "stops at the failing chunk" 2 "$(calls fstrim_calls)"
has "says which chunk" "chunk 2" "$(cat "$CTL/out")"
eq "unmounts on the way out" 1 "$(calls umount_calls)"

echo "== a failed unmount after a clean run fails the run (exit 6) and keeps the dir =="
new_case umountfail; touch "$CTL/umount_fail"; run
eq "exit 6" 6 "$(cat "$CTL/rc")"
has "names the mount it could not release" "cleanup: umount" "$(cat "$CTL/out")"
eq "the busy mount dir is NOT removed" 1 "$(ls -d "$CTL"/mnt/lxc-fstrim-* 2>/dev/null | wc -l | tr -d ' ')"

echo "== a failed unmount after a failed trim keeps the trim's exit code (5) =="
new_case bothfail; touch "$CTL/umount_fail"; echo 1 > "$CTL/fail_on"; run
eq "exit 5 preserved" 5 "$(cat "$CTL/rc")"
has "still reports the unmount failure" "cleanup: umount" "$(cat "$CTL/out")"

echo "== systemctl stop (SIGTERM to the script and its child, mid-sleep) still unmounts =="
new_case sigterm; touch "$CTL/block_sleep"
env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" MOUNT_BASE="$CTL/mnt" LOCK_FILE="$CTL/lock" CHUNK_GB=8 PAUSE_S=1   PSI_MAX=45 PSI_WAIT_MAX_S=5 bash "$SCRIPT" 5004 mp0 > "$CTL/out" 2>&1 &
spid=$!
for _ in $(seq 1 100); do [ -f "$CTL/sleeping" ] && break; /bin/sleep 0.1; done
# What systemd's default KillMode=control-group does: SIGTERM to every process of the unit.
kill -TERM "$spid" $(pgrep -P "$spid") 2>/dev/null
wait "$spid"; echo $? > "$CTL/rc"
eq "exits 143 (terminated)" 143 "$(cat "$CTL/rc")"
eq "unmounted on SIGTERM" 1 "$(calls umount_calls)"
eq "mount dir removed" 0 "$(ls -d "$CTL"/mnt/lxc-fstrim-* 2>/dev/null | wc -l | tr -d ' ')"

echo "== SIGTERM during a pressure wait (the registry unit's TimeoutStartSec deadline): stops promptly, unmounts =="
# The deadline in docs/runbooks/registry-cache.md relies on this: systemd SIGTERMs the unit's processes,
# and a run that is waiting out high IO pressure must stop at once, not after its PSI_WAIT_MAX_S.
new_case sigpsi; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"; touch "$CTL/block_sleep"
env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" MOUNT_BASE="$CTL/mnt" LOCK_FILE="$CTL/lock" CHUNK_GB=8 PAUSE_S=1   PSI_MAX=45 PSI_WAIT_MAX_S=1800 bash "$SCRIPT" 5004 mp0 > "$CTL/out" 2>&1 &
spid=$!
for _ in $(seq 1 100); do [ -f "$CTL/sleeping" ] && break; /bin/sleep 0.1; done
t0=$(date +%s)
kill -TERM "$spid" $(pgrep -P "$spid") 2>/dev/null
wait "$spid"; echo $? > "$CTL/rc"
eq "exits 143 (terminated)" 143 "$(cat "$CTL/rc")"
[ $(( $(date +%s) - t0 )) -le 5 ] && ok "stopped within 5 s, not after the 1800 s pressure budget" || bad "stopped promptly" "took $(( $(date +%s) - t0 )) s"
eq "nothing trimmed" 0 "$(calls fstrim_calls)"
eq "unmounted" 1 "$(calls umount_calls)"
eq "mount dir removed" 0 "$(ls -d "$CTL"/mnt/lxc-fstrim-* 2>/dev/null | wc -l | tr -d ' ')"

echo "== SIGTERM while an uninterruptible fstrim is in flight: waits for it, then unmounts =="
new_case sigtrim; touch "$CTL/slow_trim"
env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" MOUNT_BASE="$CTL/mnt" LOCK_FILE="$CTL/lock" CHUNK_GB=8 PAUSE_S=1   PSI_MAX=45 PSI_WAIT_MAX_S=5 bash "$SCRIPT" 5004 mp0 > "$CTL/out" 2>&1 &
spid=$!
for _ in $(seq 1 100); do [ -f "$CTL/fstrim_running" ] && break; /bin/sleep 0.1; done
kill -TERM "$spid" $(pgrep -P "$spid") 2>/dev/null
wait "$spid"; echo $? > "$CTL/rc"
eq "exits 143 (terminated)" 143 "$(cat "$CTL/rc")"
eq "only the in-flight chunk ran" 1 "$(calls fstrim_calls)"
eq "mount released after the trim finished" 0 "$(ls -d "$CTL"/mnt/lxc-fstrim-* 2>/dev/null | wc -l | tr -d ' ')"

echo "== a second run while one holds the lock refuses (exit 7) before mounting =="
new_case locked
( exec 9>"$CTL/lock"; flock 9; touch "$CTL/locked"; /bin/sleep 5 ) &
holder=$!
for _ in $(seq 1 50); do [ -f "$CTL/locked" ] && break; /bin/sleep 0.1; done
run
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
eq "exit 7" 7 "$(cat "$CTL/rc")"
eq "nothing mounted" 0 "$(calls mount_calls)"
has "says why" "another run" "$(cat "$CTL/out")"

echo "== the container's rootfs is trimmed like a mount point =="
new_case rootfs; KEY=rootfs run
eq "exit 0" 0 "$(cat "$CTL/rc")"
has "mounts the rootfs volume's device" "/dev/pve/vm-5004-disk-0" "$(cat "$CTL/mount_calls")"
eq "covers it (3 chunks)" 3 "$(calls fstrim_calls)"

echo "== a snapshot's copy of a key is never used =="
new_case snapshot
printf 'hostname: registry\nrootfs: local-lvm:vm-5004-disk-0,size=16G\n\n[before-upgrade]\nmp0: local-lvm:vm-5004-disk-1,mp=/var/lib/registry,size=384G\n' > "$CTL/pct_config"
run
eq "mp0 only in a snapshot -> exit 1" 1 "$(cat "$CTL/rc")"
eq "nothing mounted" 0 "$(calls mount_calls)"
new_case snapshot2
printf 'mp0: local-lvm:vm-5004-disk-1,mp=/var/lib/registry,size=384G\nrootfs: local-lvm:vm-5004-disk-0,size=16G\n[old]\nmp0: local-lvm:vm-5004-disk-7,mp=/var/lib/registry,size=256G\n' > "$CTL/pct_config"
run
eq "the main section's mp0 is used" 0 "$(cat "$CTL/rc")"
eq "mounted once, the current volume" "1 /dev/pve/vm-5004-disk-1" "$(calls mount_calls) $(grep -o '/dev/pve/vm-5004-disk-[0-9]' "$CTL/mount_calls")"

echo "== DEADLINE: no chunk starts that could not end by it (exit 8, mount released) =="
# Fake clock: each trim takes 60 s, a pause 1 s; a chunk is costed at 8 GiB x 10 s = 80 s.
new_case deadline; echo 1000 > "$CTL/clock"; echo 60 > "$CTL/trim_s"
run DEADLINE=1200 SEC_PER_GB=10
eq "exit 8" 8 "$(cat "$CTL/rc")"
eq "two chunks fit (1000+80, 1061+80), the third (1122+80 > 1200) does not" 2 "$(calls fstrim_calls)"
has "says why" "deadline reached" "$(cat "$CTL/out")"
eq "unmounted" 1 "$(calls umount_calls)"
eq "mount dir removed" 0 "$(ls -d "$CTL"/mnt/lxc-fstrim-* 2>/dev/null | wc -l | tr -d ' ')"
new_case deadpast; echo 5000 > "$CTL/clock"; echo 60 > "$CTL/trim_s"
run DEADLINE=4000
eq "a deadline already past -> exit 8" 8 "$(cat "$CTL/rc")"
eq "a deadline already past -> nothing trimmed" 0 "$(calls fstrim_calls)"
new_case deadroom; echo 1000 > "$CTL/clock"; echo 60 > "$CTL/trim_s"
run DEADLINE=99999
eq "a deadline with room for the whole volume -> exit 0" 0 "$(cat "$CTL/rc")"
eq "all three chunks" 3 "$(calls fstrim_calls)"

echo "== DEADLINE during a pressure wait: exit 8, not the pressure give-up =="
new_case deadpsi; echo 1000 > "$CTL/clock"; echo 60 > "$CTL/trim_s"
echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"
run DEADLINE=1100 SEC_PER_GB=10 PAUSE_S=15 PSI_WAIT_MAX_S=1800
eq "exit 8" 8 "$(cat "$CTL/rc")"
eq "nothing trimmed" 0 "$(calls fstrim_calls)"
has "says it was the deadline during the wait" "deadline reached during the pressure wait" "$(cat "$CTL/out")"

echo "== PAUSE_IDLE_S after a chunk that trimmed nothing, PAUSE_S after one that did =="
new_case idle; touch "$CTL/zero_bytes"; run PAUSE_S=7 PAUSE_IDLE_S=2
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "three short pauses" "2 2 2" "$(tr '\n' ' ' < "$CTL/sleep_args" | sed 's/ $//')"
new_case busy; run PAUSE_S=7 PAUSE_IDLE_S=2
eq "three full pauses" "7 7 7" "$(tr '\n' ' ' < "$CTL/sleep_args" | sed 's/ $//')"
new_case idledefault; touch "$CTL/zero_bytes"; run PAUSE_S=7
eq "PAUSE_IDLE_S defaults to PAUSE_S (the registry unit's behaviour)" "7 7 7" "$(tr '\n' ' ' < "$CTL/sleep_args" | sed 's/ $//')"

echo "== invalid settings are rejected before anything is mounted (exit 2) =="
for bad in "CHUNK_GB=-1" "CHUNK_GB=0" "PAUSE_S=0" "PSI_MAX=abc" "PSI_WAIT_MAX_S=x" "PAUSE_IDLE_S=0" "SEC_PER_GB=x" "DEADLINE=soon" "DEADLINE=-5"; do
  new_case "set${bad%%=*}"; run "$bad"
  eq "$bad -> exit 2" 2 "$(cat "$CTL/rc")"
  eq "$bad -> nothing mounted" 0 "$(calls mount_calls)"
done

echo "== an unknown mount point or a non-block volume is an error (exit 1) =="
new_case nomp; printf 'hostname: registry\nrootfs: local-lvm:vm-5004-disk-0,size=16G\n' > "$CTL/pct_config"; run
eq "missing mp0 -> exit 1" 1 "$(cat "$CTL/rc")"
eq "missing mp0 -> nothing mounted" 0 "$(calls mount_calls)"
new_case notblock; echo "regular file" > "$CTL/devtype"; run
eq "non-block device -> exit 1" 1 "$(cat "$CTL/rc")"
eq "non-block device -> nothing mounted" 0 "$(calls mount_calls)"
new_case badkey; KEY=unused0 run
eq "a key that is neither rootfs nor mpN -> exit 2" 2 "$(cat "$CTL/rc")"

echo
echo "lxc-fstrim-chunked: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
