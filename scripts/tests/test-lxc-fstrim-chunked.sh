#!/usr/bin/env bash
# Tests for scripts/lxc-fstrim-chunked.sh — the pressure-guarded, chunked fstrim of an LXC mount point
# (plans/2026-10-07-etcd-leader-churn-plan.md step E).
#
# The host's own fstrim.timer never reaches LXC mount points, so the registry LXC's thin volume on
# ai-node1 grew to 377 GB allocated for 170 GB used. A one-shot `pct fstrim` would send all of that as
# one discard burst onto the NVMe that also carries cp1's etcd WAL. The invariants that matter: every
# byte of the filesystem is covered, nothing is trimmed while host IO pressure is high, a stop file
# halts it between chunks, and a dead container is an error rather than a silent no-op.
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

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
BIN="$ROOT/bin"; mkdir -p "$BIN"

# --- stubs (all read their behaviour from $CTL) -------------------------------------------------
cat > "$BIN/lxc-info" <<'STUB'
#!/usr/bin/env bash
[ -f "$CTL/pid" ] && cat "$CTL/pid" || exit 1
STUB
cat > "$BIN/nsenter" <<'STUB'
#!/usr/bin/env bash
# nsenter -t <pid> -m -- <cmd...>: record the target pid, run the command with the stubs.
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  [ "$1" = "-t" ] && echo "$2" >> "$CTL/nsenter_pids"
  shift
done
shift
exec "$@"
STUB
cat > "$BIN/df" <<'STUB'
#!/usr/bin/env bash
printf '1B-blocks\n%s\n' "$(cat "$CTL/size")"
STUB
cat > "$BIN/fstrim" <<'STUB'
#!/usr/bin/env bash
# fstrim -v -o <bytes> -l <bytes> <mp>
echo "$*" >> "$CTL/fstrim_calls"
echo "$5: 1 GiB (1073741824 bytes) trimmed"
STUB
cat > "$BIN/lvs" <<'STUB'
#!/usr/bin/env bash
echo "  60.00"
STUB
cat > "$BIN/sleep" <<'STUB'
#!/usr/bin/env bash
# Every sleep advances a counter; once it reaches $CTL/psi_drop_after, IO pressure drops to 5.
n=$(( $(cat "$CTL/sleeps" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$CTL/sleeps"
if [ -f "$CTL/psi_drop_after" ] && [ "$n" -ge "$(cat "$CTL/psi_drop_after")" ]; then
  echo "some avg10=5.00 avg60=5.00 avg300=5.00 total=1" > "$CTL/psi"
fi
STUB
chmod +x "$BIN"/*

GiB=1073741824
new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL"
  echo 4242 > "$CTL/pid"
  echo $((20 * GiB)) > "$CTL/size"
  echo "some avg10=3.00 avg60=3.00 avg300=3.00 total=1" > "$CTL/psi"
}
run() {
  PATH="$BIN:$PATH" PSI_FILE="$CTL/psi" STOP_FILE="$CTL/stop" CHUNK_GB=8 PAUSE_S=1 PSI_MAX=45 PSI_WAIT_MAX_S=5 \
    bash "$SCRIPT" 5004 /var/lib/registry > "$CTL/out" 2>&1
  echo $? > "$CTL/rc"
}

echo "== covers the whole filesystem in byte-exact chunks =="
new_case cover; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "three chunks for 20 GiB at 8 GiB" 3 "$(wc -l < "$CTL/fstrim_calls" | tr -d ' ')"
eq "first chunk"  "-v -o 0 -l $((8 * GiB)) /var/lib/registry" "$(sed -n 1p "$CTL/fstrim_calls")"
eq "last chunk starts at 16 GiB" "-v -o $((16 * GiB)) -l $((8 * GiB)) /var/lib/registry" "$(sed -n 3p "$CTL/fstrim_calls")"
eq "runs inside the container's mount namespace" 4242 "$(sort -u "$CTL/nsenter_pids")"
has "reports the total trimmed" "trimmed_total=3221225472" "$(cat "$CTL/out")"

echo "== a stop file halts it before the next chunk =="
new_case stop; touch "$CTL/stop"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "no chunk trimmed" 0 "$( [ -f "$CTL/fstrim_calls" ] && wc -l < "$CTL/fstrim_calls" | tr -d ' ' || echo 0)"
has "says why" "stop file" "$(cat "$CTL/out")"

echo "== waits out high host IO pressure, then trims =="
new_case wait; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"; echo 3 > "$CTL/psi_drop_after"; run
eq "exit 0" 0 "$(cat "$CTL/rc")"
eq "all three chunks trimmed after the wait" 3 "$(wc -l < "$CTL/fstrim_calls" | tr -d ' ')"
has "logs the wait" "waited=" "$(cat "$CTL/out")"

echo "== gives up (exit 3) when pressure never drops =="
new_case givesup; echo "some avg10=80.00 avg60=70.00 avg300=60.00 total=1" > "$CTL/psi"; run
eq "exit 3" 3 "$(cat "$CTL/rc")"
eq "nothing trimmed under pressure" 0 "$( [ -f "$CTL/fstrim_calls" ] && wc -l < "$CTL/fstrim_calls" | tr -d ' ' || echo 0)"
has "says why" "IO pressure" "$(cat "$CTL/out")"

echo "== a container that is not running is an error =="
new_case down; rm -f "$CTL/pid"; run
eq "exit 1" 1 "$(cat "$CTL/rc")"
eq "nothing trimmed" 0 "$( [ -f "$CTL/fstrim_calls" ] && wc -l < "$CTL/fstrim_calls" | tr -d ' ' || echo 0)"

echo
echo "lxc-fstrim-chunked: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
