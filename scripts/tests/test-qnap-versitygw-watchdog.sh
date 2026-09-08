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

# A probe whose SUBPROCESS blocks. The disk work (mkdir/cat/rm) runs as children of the probe
# subshell, so an implementation that watches only the subshell pid can kill the parent, see it
# gone, call the probe "killable" and release the lock — while the command actually stuck on the
# disk is still running. That is the original pile-up, one level down. Here `mkdir` is stubbed to
# hang; the supervisor must signal the whole PROCESS GROUP and leave nothing behind.
CASE=blockedchild; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/blockedchild" no
mkdir -p "$ROOT/hangbin"
# Hang ONLY on the probe's own directory. The supervisor legitimately mkdir's its state dir at
# startup, and a blanket stub would wedge it there instead of inside the fenced probe.
cat > "$ROOT/hangbin/mkdir" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  *.health*) echo $$ >> "$CTL_DIR/hung_pids"; exec sleep 300 ;;
esac
exec /bin/mkdir "$@"
STUB
chmod +x "$ROOT/hangbin/mkdir"
rm -f "$ROOT/hung_pids"
# Redirect to a FILE rather than capturing with $(...). A blocked child inherits the supervisor's
# stdout, so command substitution would wait on that inherited pipe until the stub's `sleep`
# finished on its own — by which time the assertion below would be checking a process that had
# already exited naturally, and would pass no matter what the supervisor did.
env CTL_DIR="$ROOT" CURL="$ROOT/curl" PATH="$ROOT/hangbin:$PATH" \
    VGW_SUPERVISOR_BASE="$ROOT/$CASE" VGW_DIR="$USBROOT/blockedchild" \
    DISK_TIMEOUT=3 START_TIMEOUT=6 bash "$SCRIPT" > "$ROOT/blockedchild.out" 2>&1; RC=$?
OUT=$(cat "$ROOT/blockedchild.out")
check "a probe blocked in a subprocess is reported, not restarted" "disk-" "$(status_of)"
[ ! -f "$ROOT/started" ] && ok "…and the gateway is NOT restarted on a blocked probe" \
  || bad "no restart on a blocked probe" "start.sh ran"
HUNG=$(cat "$ROOT/hung_pids" 2>/dev/null | head -1)
if [ -z "$HUNG" ]; then
  # Guard against a vacuous pass: if the stub never ran, "the subprocess is gone" is trivially true
  # and the assertion below would prove nothing. That silent-green shape is the exact defect this
  # whole file exists to prevent, so an unmet precondition is a FAILURE, not a skip.
  bad "the blocked subprocess is reaped with its whole process group" \
      "precondition not met: the stub mkdir never recorded a pid, so the assertion is vacuous"
else
  sleep 1
  if kill -0 "$HUNG" 2>/dev/null; then
    bad "the blocked subprocess is reaped with its whole process group" \
        "pid $HUNG survived — only the subshell was killed, so a blocked probe would linger"
    kill -9 "$HUNG" 2>/dev/null
  else
    ok "the blocked subprocess is reaped with its whole process group"
  fi
fi

# The other half of the same finding: while a group member IS still alive, the lock must stay held
# so no successor probe can start on top of it.
CASE=abandoned; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/abandoned" no
mkdir -p "$ROOT/$CASE/state/lock"
setsid sleep 60 & SURVIVOR=$!
sleep 0.3
SPGID=$(sed 's/.*) //' /proc/$SURVIVOR/stat | awk '{print $3}')
echo abandoned-probe > "$ROOT/$CASE/state/lock/kind"
echo "$SPGID" > "$ROOT/$CASE/state/lock/pgid"
run abandoned
[ ! -f "$ROOT/$CASE/state/status" ] && ok "an abandoned probe group keeps the lock held" \
  || bad "abandoned probe group keeps the lock" "ran anyway: $(status_of)"
kill -9 -"$SPGID" 2>/dev/null; wait $SURVIVOR 2>/dev/null
# Once the group is gone the lock must be reclaimable — self-healing, no manual step.
run abandoned
check "…and is reclaimed once that group finally dies" "healthy" "$(status_of)"

# Group detection must tolerate OTHER processes exiting during the scan. Reading /proc is inherently
# racy on a busy NAS: an implementation that globs /proc/[0-9]*/stat and hands the list to one awk
# fails to open a vanished entry and exits non-zero, which the caller reads as "the group is gone" —
# releasing an abandoned probe's lock while its D-state descendant is still alive.
#
# Tested as a UNIT rather than end-to-end. A supervisor run calls group_alive only a handful of
# times, so an end-to-end test would have to win a timing race to observe the bug; calling the
# function directly, hundreds of times, under continuous churn turns a rare race into a certainty.
sed -n '/^group_alive() {/,/^}/p' "$SCRIPT" > "$ROOT/group_alive.sh"
# shellcheck disable=SC1090
. "$ROOT/group_alive.sh"
PROC_ROOT=/proc   # the function reads it; the script sets it, the sourced fragment does not
# The race is made DETERMINISTIC with a synthetic /proc (PROC_ROOT): entry 050 is a directory with
# no stat file, which is exactly what the kernel leaves behind for a process that exited between the
# shell expanding the glob and the scanner opening the file. It sorts BEFORE the entry holding the
# target, so any implementation that aborts the whole scan on one unreadable entry never reaches the
# live member. No timing, no flakiness.
FAKE="$ROOT/fakeproc"
rm -rf "$FAKE"; mkdir -p "$FAKE/050" "$FAKE/100" "$FAKE/200"
printf '100 (sleep) S 1 4242 4242 0 -1 0 0 0 0 0 0 0 20 0 1 0 999 0 0\n' > "$FAKE/100/stat"
printf '200 (bash) S 1 7777 7777 0 -1 0 0 0 0 0 0 0 20 0 1 0 999 0 0\n'  > "$FAKE/200/stat"
if PROC_ROOT="$FAKE" group_alive 4242; then
  ok "a vanished /proc entry does not hide a live group member"
else
  bad "vanished /proc entry tolerated" \
      "reported group 4242 gone because entry 050 had no stat file — a routine event on a busy NAS, and it would release an abandoned probe's lock"
fi
# It must still report a genuinely absent group as absent, or the lock would never be reclaimed.
PROC_ROOT="$FAKE" group_alive 999999 \
  && bad "an absent group is reported absent" "reported alive — a stale lock would never be reclaimed" \
  || ok "an absent group is still reported absent"

# Belt and braces against the real /proc, which churns constantly on a NAS.
setsid sleep 30 & SURVIVOR=$!
sleep 0.3
SPGID=$(sed 's/.*) //' /proc/$SURVIVOR/stat | awk '{print $3}')
( for i in $(seq 1 2000); do /bin/true & done; wait ) >/dev/null 2>&1 &
CHURN=$!
MISSES=0
for i in $(seq 1 200); do
  group_alive "$SPGID" || MISSES=$((MISSES + 1))
done
kill -9 $CHURN 2>/dev/null; wait $CHURN 2>/dev/null
[ "$MISSES" = 0 ] && ok "and never reports a live group as gone against the real /proc under churn" \
  || bad "live group under real churn" "$MISSES/200 calls said the group was gone"
kill -9 -"$SPGID" 2>/dev/null; wait $SURVIVOR 2>/dev/null

# Stale-lock reclamation must be SERIALISED. The dangerous interleaving is not two runs racing to
# delete, it is the second deleting the FIRST run's freshly created lock. That window is microseconds
# wide, so it is tested through the guard that closes it rather than by trying to win the race:
# while another run holds the reclaim guard, a run facing a stale lock must back off entirely.
CASE=reclaimguard; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/reclaimguard" no
mkdir -p "$ROOT/$CASE/state/lock" "$ROOT/$CASE/state/reclaim"
echo 999999 > "$ROOT/$CASE/state/lock/pid"      # a holder that is definitively dead
echo 1 > "$ROOT/$CASE/state/lock/starttime"
run reclaimguard
[ ! -f "$ROOT/$CASE/state/status" ] && ok "a held reclaim guard stops a second run reclaiming" \
  || bad "held reclaim guard" "reclaimed anyway: $(status_of) — reclamation is not serialised"

# ...but a guard left behind by a run that died inside it must be broken, or reclamation deadlocks.
CASE=guardstale; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/guardstale" no
mkdir -p "$ROOT/$CASE/state/lock" "$ROOT/$CASE/state/reclaim"
echo 999999 > "$ROOT/$CASE/state/lock/pid"
echo 1 > "$ROOT/$CASE/state/lock/starttime"
touch -d '2 hours ago' "$ROOT/$CASE/state/reclaim" 2>/dev/null || touch -t 200001010000 "$ROOT/$CASE/state/reclaim"
run guardstale
check "an abandoned reclaim guard is broken rather than deadlocking" "healthy" "$(status_of)"

# Smoke test on top of the guard: many runs, one stale lock, exactly one gets through. The curl stub
# is called only AFTER the lock is acquired, so counting its invocations counts winners.
CASE=reclaimrace; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/reclaimrace" no
mkdir -p "$ROOT/$CASE/state/lock" "$ROOT/slowbin"
echo 999999 > "$ROOT/$CASE/state/lock/pid"
echo 1 > "$ROOT/$CASE/state/lock/starttime"
cat > "$ROOT/slowbin/curl" <<'STUB'
#!/usr/bin/env bash
echo x >> "$CTL_DIR/acquired"
sleep 2
printf '%s' "$(cat "$CTL_DIR/http_code" 2>/dev/null || echo 000)"
STUB
chmod +x "$ROOT/slowbin/curl"
rm -f "$ROOT/acquired"
for i in 1 2 3 4 5; do
  env CTL_DIR="$ROOT" CURL="$ROOT/slowbin/curl" VGW_SUPERVISOR_BASE="$ROOT/$CASE" \
      VGW_DIR="$USBROOT/reclaimrace" DISK_TIMEOUT=5 bash "$SCRIPT" >/dev/null 2>&1 &
done
wait
WINNERS=$(wc -l < "$ROOT/acquired" 2>/dev/null || echo 0)
[ "$WINNERS" = 1 ] && ok "exactly one of 5 concurrent runs reclaims a stale lock" \
  || bad "concurrent stale-lock reclaim" "$WINNERS runs got past the lock — reclamation is racy"

echo
echo "== the gateway's own log stays bounded while it holds the fd open =="
# rotate() must COPY-then-TRUNCATE. `mv` would leave the running gateway appending to the renamed
# inode forever: unbounded, and never size-checked again because rotate only stats the live path.
# That is the 478 MB log bug relocated onto the internal pool, so it is asserted against a writer
# that holds the descriptor open across the rotation, exactly like the real gateway does.
CASE=rotate; reset_case $CASE
echo 403 > "$ROOT/http_code"; mk_start "$USBROOT/rotate" no
mkdir -p "$ROOT/$CASE"
head -c 200000 /dev/zero | tr '\0' 'x' > "$ROOT/$CASE/versitygw.log"
# A persistent writer holding an O_APPEND fd, as `>>` gives the real gateway.
( exec 9>> "$ROOT/$CASE/versitygw.log"; for i in $(seq 1 40); do echo "line $i" >&9; sleep 0.1; done ) &
WRITER=$!
sleep 0.3
env CTL_DIR="$ROOT" CURL="$ROOT/curl" VGW_SUPERVISOR_BASE="$ROOT/$CASE" \
    VGW_DIR="$USBROOT/rotate" DISK_TIMEOUT=3 MAX_LOG_BYTES=1000 bash "$SCRIPT" >/dev/null 2>&1
wait $WRITER 2>/dev/null
LIVE=$(wc -c < "$ROOT/$CASE/versitygw.log" 2>/dev/null || echo -1)
[ -f "$ROOT/$CASE/versitygw.log.1" ] && ok "rotation keeps one saved generation" || bad "saved generation" "no .log.1"
if [ "$LIVE" -lt 100000 ] && [ "$LIVE" -gt 0 ]; then
  ok "the live log was truncated in place and the writer kept writing to it ($LIVE bytes)"
else
  bad "writer follows the truncated inode" "live log is $LIVE bytes — writer likely followed a renamed inode"
fi

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

# A startup that never answers must not be left running. start.sh reads the gateway binary from the
# USB, so it can wedge exactly as a probe can — and `ps | grep 'versitygw --port'` cannot see a
# process stuck BEFORE exec. Without reaping its process group, every failed attempt would leave a
# blocked startup behind and they would accumulate across cron runs.
CASE=startorphan; reset_case $CASE
echo 000 > "$ROOT/http_code"
mkdir -p "$USBROOT/startorphan"
cat > "$USBROOT/startorphan/start.sh" <<STUB
#!/usr/bin/env bash
echo started >> "$ROOT/started"
echo \$\$ >> "$ROOT/start_pids"
exec sleep 300
STUB
chmod +x "$USBROOT/startorphan/start.sh"
rm -f "$ROOT/start_pids"
run startorphan
check "a startup that never answers is reported" "restart-" "$(status_of)"
SP_PID=$(head -1 "$ROOT/start_pids" 2>/dev/null)
if [ -z "$SP_PID" ]; then
  bad "the failed startup is reaped with its group" "precondition not met: start.sh never recorded a pid"
else
  sleep 1
  if kill -0 "$SP_PID" 2>/dev/null; then
    bad "the failed startup is reaped with its group" "pid $SP_PID survived — failed startups would accumulate"
    kill -9 "$SP_PID" 2>/dev/null
  else
    ok "the failed startup is reaped with its whole process group"
  fi
fi

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
