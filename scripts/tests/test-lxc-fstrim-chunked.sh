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
# - a failed trim is a failed run, and bad settings or an unknown volume are errors.
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
[ "$1" = path ] && [ "$2" = "local-lvm:vm-5004-disk-1" ] && echo /dev/pve/vm-5004-disk-1
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
exit 0
STUB
cat > "$BIN/fstrim" <<'STUB'
#!/usr/bin/env bash
# fstrim -v -o <bytes> -l <bytes> <dir>; fails on call number $CTL/fail_on if set
echo "$*" >> "$CTL/fstrim_calls"
n=$(wc -l < "$CTL/fstrim_calls")
if [ -f "$CTL/fail_on" ] && [ "$n" -ge "$(cat "$CTL/fail_on")" ]; then echo "fstrim: $5: FITRIM ioctl failed: Input/output error" >&2; exit 1; fi
echo "$5: 1 GiB (1073741824 bytes) trimmed"
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
if [ -f "$CTL/psi_drop_after" ] && [ "$n" -ge "$(cat "$CTL/psi_drop_after")" ]; then
  echo "some avg10=5.00 avg60=5.00 avg300=5.00 total=1" > "$CTL/psi"
fi
if [ -f "$CTL/stop_after" ] && [ "$n" -ge "$(cat "$CTL/stop_after")" ]; then touch "$CTL/stop"; fi
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
run() { # extra VAR=value settings may be passed as arguments
  env PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" MOUNT_BASE="$CTL/mnt" CHUNK_GB=8 PAUSE_S=1 \
    PSI_MAX=45 PSI_WAIT_MAX_S=5 "$@" bash "$SCRIPT" 5004 mp0 > "$CTL/out" 2>&1
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

echo "== invalid settings are rejected before anything is mounted (exit 2) =="
for bad in "CHUNK_GB=-1" "CHUNK_GB=0" "PAUSE_S=0" "PSI_MAX=abc" "PSI_WAIT_MAX_S=x"; do
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

echo
echo "lxc-fstrim-chunked: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
