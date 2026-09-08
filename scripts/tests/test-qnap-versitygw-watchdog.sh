#!/usr/bin/env bash
# Tests for scripts/qnap-versitygw-watchdog.sh — the QNAP versitygw supervisor (W3).
#
# These encode the invariants that the 2026-09-08 forge outage proved were missing. The old
# watchdog passed a naive "is it running?" check throughout a 4.5h outage, so the tests that matter
# most here are the NEGATIVE ones: that a sick disk does NOT trigger a restart, and that a stuck
# probe does NOT allow a second one to start.
#
# Run on Linux (WSL is fine): bash scripts/tests/test-qnap-versitygw-watchdog.sh
# The suite needs two DIFFERENT filesystems to exercise the same-device guard honestly, so it puts
# the supervisor base on /tmp and the fake "USB" on /dev/shm.
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/qnap-versitygw-watchdog.sh"
[ -f "$SCRIPT" ] || { echo "cannot find $SCRIPT"; exit 1; }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n     %s\n' "$1" "${2:-}"; }
check(){ # check <desc> <expected-substring> <actual>
  case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2', got: $3" ;; esac
}

ROOT=$(mktemp -d /tmp/vgwtest.XXXXXX)
USBROOT=$(mktemp -d /dev/shm/vgwusb.XXXXXX)
trap 'chmod -R u+rwx "$USBROOT" 2>/dev/null; rm -rf "$ROOT" "$USBROOT"' EXIT

# --- stubs -------------------------------------------------------------------------------------
# curl stub: prints whatever $ROOT/http_code says. "000" is curl's own value for "no response",
# which is exactly what a wedged gateway produces, so the stub is faithful.
cat > "$ROOT/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s' "$(cat "$CTL_DIR/http_code" 2>/dev/null || echo 000)"
STUB
chmod +x "$ROOT/curl"

# start.sh stub: records each invocation, and (if told to) marks the gateway as coming back up.
mk_start() { # $1 = usb dir, $2 = "up" if the restart should succeed
  mkdir -p "$1"
  cat > "$1/start.sh" <<STUB
#!/usr/bin/env bash
echo started >> "$ROOT/started"
[ "$2" = up ] && echo 403 > "$ROOT/http_code"
sleep 30
STUB
  chmod +x "$1/start.sh"
}

# run <name> [env assignments...] -> populates $OUT (stdout+stderr) and $RC
run() {
  local usb="$USBROOT/$1"; shift
  OUT=$(env CTL_DIR="$ROOT" CURL="$ROOT/curl" \
        VGW_SUPERVISOR_BASE="$ROOT/$CASE" VGW_DIR="$usb" \
        DISK_TIMEOUT=3 START_TIMEOUT=6 "$@" bash "$SCRIPT" 2>&1)
  RC=$?
}
status_of() { cut -f2 "$ROOT/$CASE/state/status" 2>/dev/null; }
reset_case() { CASE="$1"; rm -rf "${ROOT:?}/$1" "$ROOT/started"; mkdir -p "$ROOT/$1"; }

echo "== invariant 1: the supervisor never depends on the disk it watches =="

CASE=guard1; reset_case $CASE
OUT=$(env CTL_DIR="$ROOT" CURL="$ROOT/curl" \
      VGW_SUPERVISOR_BASE="/share/external/DEV3302_2/supervisor" VGW_DIR="$USBROOT/g1" \
      bash "$SCRIPT" 2>&1); RC=$?
check "base under /share/external is refused" "on the external disk it watches" "$OUT"
[ "$RC" = 1 ] && ok "…and exits non-zero" || bad "…and exits non-zero" "rc=$RC"

# Same-device guard: put base and "USB" on the SAME filesystem. This is the subtle regression the
# guard exists to catch — a symlink or remount silently putting the lock back on the watched disk.
CASE=guard2; reset_case $CASE
mkdir -p "$USBROOT/g2"
OUT=$(env CTL_DIR="$ROOT" CURL="$ROOT/curl" \
      VGW_SUPERVISOR_BASE="$USBROOT/g2base" VGW_DIR="$USBROOT/g2" \
      bash "$SCRIPT" 2>&1); RC=$?
check "base on the same device as the gateway is refused" "same device" "$OUT"
[ "$RC" = 1 ] && ok "…and exits non-zero" || bad "…and exits non-zero" "rc=$RC"

echo
echo "== healthy path =="
CASE=healthy; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/healthy" no
run healthy
check "403 from the auth layer counts as responsive" "healthy" "$(status_of)"
[ ! -f "$ROOT/started" ] && ok "healthy gateway is not restarted" || bad "healthy gateway is not restarted" "start.sh ran"
[ ! -d "$ROOT/$CASE/state/lock" ] && ok "lock released on a clean exit" || bad "lock released on a clean exit" "lock still present"
[ -d "$USBROOT/healthy/.health" ] && ok "disk probe writes outside data/ (no phantom S3 bucket)" \
  || bad "disk probe writes outside data/" ".health missing"
[ ! -e "$USBROOT/healthy/data" ] && ok "disk probe never touches the S3 data root" || bad "probe touched data/" "data/ created"

echo
echo "== the outage case: a sick disk must NOT trigger a restart =="
CASE=sickdisk; reset_case $CASE
echo 000 > "$ROOT/http_code"          # gateway not answering either — the full outage shape
mk_start "$USBROOT/sickdisk" up
chmod 500 "$USBROOT/sickdisk"          # writes fail => disk probe fails
run sickdisk
chmod 700 "$USBROOT/sickdisk"
check "unwritable disk is reported as disk-unhealthy" "disk-unhealthy" "$(status_of)"
[ ! -f "$ROOT/started" ] && ok "NO restart attempted while the disk is bad" \
  || bad "NO restart attempted while the disk is bad" "start.sh ran — this is the 2026-09-08 bug"

echo
echo "== a slow/hung probe must not pile up =="
CASE=slow; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/slow" no
# Hold the lock with a live process to simulate an outstanding (or abandoned D-state) probe.
mkdir -p "$ROOT/$CASE/state/lock"
sleep 60 & HOLDER=$!
echo "$HOLDER" > "$ROOT/$CASE/state/lock/pid"
awk '{print $22}' /proc/$HOLDER/stat > "$ROOT/$CASE/state/lock/starttime"
run slow
[ "$RC" = 0 ] && ok "a run with the lock held exits 0 (cron stays quiet)" || bad "exits 0" "rc=$RC"
[ ! -f "$ROOT/$CASE/state/status" ] && ok "…and takes no action at all" || bad "…and takes no action" "status=$(status_of)"
[ "$(cat "$ROOT/$CASE/state/lock/pid")" = "$HOLDER" ] && ok "…and does not steal the lock" || bad "lock stolen" ""
kill $HOLDER 2>/dev/null; wait $HOLDER 2>/dev/null

# Same lock, holder now dead -> must be reclaimed rather than deadlocking forever.
run slow
check "a lock whose holder died is reclaimed" "healthy" "$(status_of)"

# PID reuse: the pid is alive but is a DIFFERENT process than the one that took the lock.
CASE=pidreuse; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/pidreuse" no
mkdir -p "$ROOT/$CASE/state/lock"
sleep 60 & HOLDER=$!
echo "$HOLDER" > "$ROOT/$CASE/state/lock/pid"
echo "999999999" > "$ROOT/$CASE/state/lock/starttime"   # start-time does not match => not our holder
run pidreuse
check "a recycled pid does not hold the lock forever" "healthy" "$(status_of)"
kill $HOLDER 2>/dev/null; wait $HOLDER 2>/dev/null

echo
echo "== restart only when a restart can help =="
CASE=restart; reset_case $CASE
echo 000 > "$ROOT/http_code"; mk_start "$USBROOT/restart" up
run restart
check "disk ok + gateway down => restart" "restarted" "$(status_of)"
[ -f "$ROOT/started" ] && ok "…and start.sh was actually invoked" || bad "start.sh invoked" "no marker"

CASE=restartfail; reset_case $CASE
echo 000 > "$ROOT/http_code"; mk_start "$USBROOT/restartfail" no   # never comes up
run restartfail
check "a restart that does not come up is reported" "restart-failed" "$(status_of)"

echo
echo "== bounded restarts: alert rather than spin =="
CASE=budget; reset_case $CASE
echo 000 > "$ROOT/http_code"; mk_start "$USBROOT/budget" up
mkdir -p "$ROOT/$CASE/state"
NOW=$(date +%s)
for i in 1 2 3; do echo $((NOW - 60)); done > "$ROOT/$CASE/state/attempts"
run budget
check "budget exhausted => stop restarting" "restart-budget-exhausted" "$(status_of)"
[ ! -f "$ROOT/started" ] && ok "…and start.sh is NOT invoked again" || bad "start.sh invoked" "spun anyway"

# Attempts outside the window must not count against the budget.
CASE=oldattempts; reset_case $CASE
echo 000 > "$ROOT/http_code"; mk_start "$USBROOT/oldattempts" up
mkdir -p "$ROOT/$CASE/state"
for i in 1 2 3; do echo $((NOW - 7200)); done > "$ROOT/$CASE/state/attempts"
run oldattempts
check "attempts older than the window are ignored" "restarted" "$(status_of)"

# A healthy check clears the ledger, so the budget tracks consecutive trouble, not uptime.
CASE=clear; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/clear" no
mkdir -p "$ROOT/$CASE/state"
for i in 1 2 3; do echo $((NOW - 60)); done > "$ROOT/$CASE/state/attempts"
run clear
[ ! -s "$ROOT/$CASE/state/attempts" ] && ok "a healthy check clears the restart ledger" \
  || bad "healthy clears ledger" "attempts=$(cat "$ROOT/$CASE/state/attempts")"

echo
echo "== an unresponsive-but-present gateway is stopped before a replacement starts =="
CASE=dup; reset_case $CASE
echo 000 > "$ROOT/http_code"; mk_start "$USBROOT/dup" up
bash -c 'exec -a "versitygw --port :7070" sleep 45' & ZOMBIE=$!
sleep 0.3
run dup
sleep 0.5
if kill -0 $ZOMBIE 2>/dev/null; then
  bad "the stale gateway process is reaped" "pid $ZOMBIE still alive => two gateways on :7070"
  kill -9 $ZOMBIE 2>/dev/null
else
  ok "the stale gateway process is reaped before restart"
fi
wait $ZOMBIE 2>/dev/null

echo
echo "== logs and state never land on the watched disk =="
CASE=logs; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/logs" no
run logs
[ -f "$ROOT/$CASE/watchdog.log" ] && ok "watchdog.log is on the internal base" || bad "watchdog.log on internal base" "missing"
STRAY=$(find "$USBROOT/logs" -name '*.log' 2>/dev/null | wc -l)
[ "$STRAY" = 0 ] && ok "no log file is created on the USB disk" || bad "no log on USB" "found $STRAY"

echo
printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || exit 1
