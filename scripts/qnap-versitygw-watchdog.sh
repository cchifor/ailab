#!/bin/bash
# versitygw supervisor for the QNAP (W3 of the USB failure-domain plan).
# Deployed to the NAS by scripts/qnap-versitygw-install.sh; do not edit in place on the NAS.
#
# WHY THIS REPLACEMENT EXISTS. On 2026-09-08 a USB stall wedged versitygw and took the git forge
# down for ~4.5h. The previous watchdog was five lines and every one of them was part of the
# failure:
#
#     BIN=$(ls /share/external/*/versitygw/bin/versitygw ...)   # globs INTO the wedged mount
#     ps w | grep "versitygw --port :7070" && exit 0            # tests the PROCESS, not the answer
#     setsid "$D/start.sh" >> "$D/versitygw.log" 2>&1 &         # logs ONTO the wedged disk
#
# It lived at /share/external/DEV3302_2/versitygw/watchdog.sh -- ON the disk it was watching -- and
# cron ran it every 3 minutes with no mutual exclusion. When the disk hung, `[ -f "$f" ]` (a stat,
# served from cache) still succeeded but `/bin/sh "$f"` (a read) blocked, so a fresh copy wedged
# every 3 minutes. 25 accumulated in D-state. That is also why `cat`-ing the script hung during
# triage: the diagnostic tool and the patient were on the same failing disk.
#
# THE FOUR INVARIANTS THIS SCRIPT KEEPS
#
#   1. NOTHING THE SUPERVISOR NEEDS LIVES ON THE USB DISK. Script, lock, state, and both logs sit on
#      the internal pool. The USB is touched in exactly one place (the disk probe), always from a
#      child process that the parent is prepared to abandon.
#
#   2. AT MOST ONE OUTSTANDING PROBE, EVER. A process blocked in uninterruptible sleep (D) does NOT
#      die on SIGKILL -- proven in the incident: kubelet ignored SIGKILL for 6 minutes and
#      `talosctl reboot` hung too. So a `wait`-based timeout would hang the supervisor itself, and
#      `kill -9` is not a reliable escape. Instead the parent POLLS and, on deadline, ABANDONS the
#      child while DELIBERATELY LEAVING THE LOCK HELD in that child's name. Every later cron run
#      sees a live lock holder and exits immediately. That converts the old 25-copy pile-up into a
#      hard ceiling of one. When the disk recovers the abandoned child completes, its pid dies, and
#      the next run reclaims the stale lock -- self-healing with no manual step.
#
#   3. A RESTART IS ONLY ATTEMPTED WHEN A RESTART CAN ACTUALLY HELP. Restarting cannot repair
#      uninterruptible disk I/O; it just adds another process that hangs. So the disk is probed
#      FIRST and a bad disk suppresses the restart entirely. Restarts are also budgeted
#      (MAX_RESTARTS in RESTART_WINDOW) so a crash-looping binary reports instead of spinning.
#
#   4. NEVER REBOOT THE NAS. It is shared infrastructure (Talos etcd backups, Velero, the pve-nfs
#      export). Escalation is a log line and a QuLog event, not a reboot.
#
# WHAT THIS DELIBERATELY DOES *NOT* DO -- division of labour with the cluster-side probe.
# This script does not perform an authenticated S3 round-trip. Signing SigV4 in bash 3.2 (the
# NAS ships bash 3.2.57, with no flock/timeout/pgrep) would be fragile in exactly the code path
# that must never fail. The authoritative end-to-end check is the in-cluster CronJob
# `versitygw-probe` (kubernetes/apps/infrastructure/storage/talos-backup/versitygw-probe.yaml),
# which does a real PUT/GET/verify/DELETE every 10 minutes and raises VersitygwProbeFailed.
# The split is intentional:
#     cluster-side    -> "is the object store USABLE?"           (detection + alerting)
#     NAS-side (here) -> "can a local restart fix it, or not?"   (bounded, safe remediation)
# Neither is a substitute for the other, and this script is never the thing that pages you.
set -u

#--- configuration ------------------------------------------------------------------------------
BASE="${VGW_SUPERVISOR_BASE:-/share/ZFS2_DATA/.versitygw-supervisor}"  # INTERNAL pool. Never /share/external.
VGW_DIR="${VGW_DIR:-/share/external/DEV3302_2/versitygw}"              # on the USB disk, by design
VGW_URL="${VGW_URL:-https://127.0.0.1:7070/}"
HTTP_TIMEOUT="${HTTP_TIMEOUT:-8}"      # curl self-bounds; a socket wait is killable, unlike D-state
DISK_TIMEOUT="${DISK_TIMEOUT:-20}"     # deadline before the disk-probe child is abandoned
START_TIMEOUT="${START_TIMEOUT:-25}"   # how long to wait for a restarted gateway to answer
MAX_RESTARTS="${MAX_RESTARTS:-3}"
RESTART_WINDOW="${RESTART_WINDOW:-1800}"   # seconds (30 min)
MAX_LOG_BYTES="${MAX_LOG_BYTES:-20971520}" # 20 MiB, then rotate; keep 2 generations
STALE_LOCK_SECS="${STALE_LOCK_SECS:-3600}" # backstop for a lock whose pid file never got written

STATE="$BASE/state"
LOCK="$STATE/lock"
STATUS="$STATE/status"
ATTEMPTS="$STATE/attempts"
WD_LOG="$BASE/watchdog.log"
VGW_LOG="$BASE/versitygw.log"   # versitygw's OWN stdout, moved OFF the USB (see redirect below)

CURL="${CURL:-/sbin/curl}"

#--- logging ------------------------------------------------------------------------------------
rotate() {  # $1 = logfile. Size-capped so an unbounded log can never fill the internal pool.
  local f=$1 sz
  [ -f "$f" ] || return 0
  sz=$(wc -c < "$f" 2>/dev/null || echo 0)
  [ "${sz:-0}" -gt "$MAX_LOG_BYTES" ] || return 0
  rm -f "$f.2"
  [ -f "$f.1" ] && mv -f "$f.1" "$f.2"
  mv -f "$f" "$f.1"
}
log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$WD_LOG" 2>/dev/null; }

# QuLog event, so the NAS's own notification pipeline sees a problem even if the cluster is down.
# Guarded: log_tool is QNAP-specific and absent on any test box.  -t 1 = warning, -t 2 = error.
event() {  # $1 = 1|2 severity, $2 = message
  [ -x /sbin/log_tool ] || return 0
  /sbin/log_tool -t "$1" -a "versitygw-watchdog: $2" >/dev/null 2>&1 || true
}

set_status() {  # $1 = verdict token, $2 = detail
  printf '%s\t%s\t%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "$2" > "$STATUS" 2>/dev/null
  log "[$1] $2"
}

#--- invariant 1: refuse to run from, or store state on, the disk we are watching -----------------
case "$BASE" in
  /share/external/*)
    echo "FATAL: supervisor base '$BASE' is on the external disk it watches -- refusing" >&2
    exit 1 ;;
esac
mkdir -p "$STATE" 2>/dev/null || { echo "FATAL: cannot create $STATE" >&2; exit 1; }
# Prove the base is on a different block device than the gateway's disk. A symlink or a remount
# could otherwise quietly put our lock and logs back onto the USB, re-arming the original bug.
base_dev=$(df -P "$BASE" 2>/dev/null | awk 'NR==2{print $1}')
vgw_dev=$(df -P "$VGW_DIR" 2>/dev/null | awk 'NR==2{print $1}')
if [ -n "$base_dev" ] && [ "$base_dev" = "$vgw_dev" ]; then
  echo "FATAL: supervisor base and gateway disk are the same device ($base_dev) -- refusing" >&2
  exit 1
fi
rotate "$WD_LOG"
rotate "$VGW_LOG"

#--- invariant 2: a single atomic lock, held across abandonment -----------------------------------
# `mkdir` is atomic on ext4/ZFS, which is why it is the lock primitive here: the NAS has no flock.
# The lock records BOTH the pid and that pid's start-time (field 22 of /proc/<pid>/stat) so a
# recycled pid number can never make a dead lock look alive forever.
proc_starttime() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }

holder_alive() {
  local pid st
  [ -f "$LOCK/pid" ] || return 1
  pid=$(cat "$LOCK/pid" 2>/dev/null)
  [ -n "${pid:-}" ] || return 1
  [ -d "/proc/$pid" ] || return 1
  st=$(proc_starttime "$pid")
  # No stored start-time (older lock) -> fall back to pid liveness alone.
  [ -f "$LOCK/starttime" ] || return 0
  [ "$st" = "$(cat "$LOCK/starttime" 2>/dev/null)" ]
}

if ! mkdir "$LOCK" 2>/dev/null; then
  if holder_alive; then
    # THE pile-up guard. This is the normal, healthy response while a probe is outstanding -- and
    # the load-bearing one while a probe is wedged in D-state. Exit silently: at 3-minute cron
    # granularity, logging here would itself become the noise.
    exit 0
  fi
  # Stale: holder is gone, or the lock was created by a run that died before recording its pid.
  lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
  if [ -f "$LOCK/pid" ] || [ "$lock_age" -ge "$STALE_LOCK_SECS" ]; then
    log "reclaiming stale lock (age ${lock_age}s)"
    rm -f "$LOCK"/* 2>/dev/null
  else
    exit 0   # very young, pid not yet written -- let the other run finish
  fi
fi
# The lock is ours. It is released on every exit EXCEPT deliberate abandonment (see below).
RELEASE_LOCK=yes
cleanup() { [ "$RELEASE_LOCK" = yes ] && rm -rf "$LOCK" 2>/dev/null; }
trap cleanup EXIT INT TERM

#--- probe A: does the gateway ANSWER? ------------------------------------------------------------
# Runs in the parent: curl bounds itself with --max-time and is blocked on a socket, not on disk,
# so it is genuinely killable. Any HTTP status counts as responsive -- 403 from the auth layer is a
# perfectly good proof that the request loop is being serviced. (It is NOT proof the backend is
# healthy, which is what probe B and the cluster-side round-trip are for.)
http_code=$("$CURL" -sk -o /dev/null -w '%{http_code}' --max-time "$HTTP_TIMEOUT" "$VGW_URL" 2>/dev/null)
case "${http_code:-000}" in
  000|"") http_ok=no ;;
  *)      http_ok=yes ;;
esac

#--- probe B: is the DISK underneath actually usable? ---------------------------------------------
# The only place this script touches the USB, and it is fenced. Runs as a child so the parent keeps
# control when the filesystem wedges. Probes $VGW_DIR/.health -- the same ext4 filesystem as the S3
# backend but OUTSIDE $VGW_DIR/data, because versitygw's posix backend maps every top-level
# directory under the data root to a BUCKET; probing inside it would publish a phantom bucket.
# create+write+read+unlink is deliberate: it forces ext4 metadata journalling, and it was the jbd2
# journal thread that sat in D-state during the incident. A read-only or stat-only check would have
# been served from cache and passed while the disk was dead -- the same blind spot as `ps | grep`.
PROBE_RESULT="$STATE/probe.result"
rm -f "$PROBE_RESULT" 2>/dev/null
(
  hdir="$VGW_DIR/.health"
  f="$hdir/probe.$$"
  stamp="probe-$(date +%s)-$$"
  mkdir -p "$hdir" 2>/dev/null || { echo "mkdir-failed" > "$PROBE_RESULT"; exit 1; }
  printf '%s' "$stamp" > "$f" 2>/dev/null || { echo "write-failed" > "$PROBE_RESULT"; exit 1; }
  got=$(cat "$f" 2>/dev/null)
  rm -f "$f" 2>/dev/null
  if [ "$got" = "$stamp" ]; then
    echo ok > "$PROBE_RESULT"
  else
    echo "readback-mismatch" > "$PROBE_RESULT"
  fi
) &
probe_pid=$!
printf '%s' "$probe_pid" > "$LOCK/pid"
printf '%s' "$(proc_starttime "$probe_pid")" > "$LOCK/starttime"

# Poll -- never `wait`. `wait` on a D-state child blocks forever and would hang the supervisor,
# which is the exact failure being engineered out.
waited=0
while [ "$waited" -lt "$DISK_TIMEOUT" ]; do
  kill -0 "$probe_pid" 2>/dev/null || break
  sleep 1
  waited=$((waited + 1))
done

if kill -0 "$probe_pid" 2>/dev/null; then
  # Deadline hit. Try SIGKILL as a courtesy (it works if the child is merely slow), then ABANDON.
  kill -9 "$probe_pid" 2>/dev/null
  sleep 1
  if kill -0 "$probe_pid" 2>/dev/null; then
    # Survived SIGKILL => uninterruptible sleep on hung I/O. Nothing local can fix this: not a
    # restart, not a kill. Hold the lock in the abandoned child's name so no successor ever stacks
    # on top of it, and leave. The lock is reclaimed automatically once the pid finally dies.
    RELEASE_LOCK=no
    set_status disk-wedged \
      "disk probe unkillable in D-state after ${DISK_TIMEOUT}s (pid $probe_pid, http=${http_code:-none}); NOT restarting -- a restart cannot repair hung I/O. Lock held until the pid clears. Storage intervention required."
    event 2 "USB disk wedged: probe pid $probe_pid stuck in D-state; versitygw NOT restarted (restart cannot fix hung I/O)"
    exit 0
  fi
  set_status disk-slow "disk probe exceeded ${DISK_TIMEOUT}s but was killable (http=${http_code:-none}); NOT restarting"
  event 1 "USB disk probe timed out after ${DISK_TIMEOUT}s; versitygw not restarted"
  exit 0
fi

disk_result=$(cat "$PROBE_RESULT" 2>/dev/null || echo "no-result")
rm -f "$PROBE_RESULT" 2>/dev/null
if [ "$disk_result" = ok ]; then disk_ok=yes; else disk_ok=no; fi

#--- decide ---------------------------------------------------------------------------------------
if [ "$disk_ok" = no ]; then
  # Invariant 3: the disk is the problem, so a restart is not the answer. Report and stop.
  set_status disk-unhealthy "disk probe failed ($disk_result, http=${http_code:-none}); NOT restarting -- storage intervention required"
  event 2 "USB disk probe failed ($disk_result); versitygw not restarted"
  exit 0
fi

if [ "$http_ok" = yes ]; then
  # Disk good, gateway answering. Clear the restart ledger: the budget is for consecutive trouble,
  # not for the lifetime of the box.
  : > "$ATTEMPTS" 2>/dev/null
  set_status healthy "http=$http_code, disk=ok"
  exit 0
fi

#--- the one case a restart can help: disk fine, gateway not answering -----------------------------
now=$(date +%s)
recent=0
if [ -f "$ATTEMPTS" ]; then
  while read -r ts; do
    [ -n "${ts:-}" ] || continue
    [ $((now - ts)) -lt "$RESTART_WINDOW" ] && recent=$((recent + 1))
  done < "$ATTEMPTS"
fi

if [ "$recent" -ge "$MAX_RESTARTS" ]; then
  # Invariant 3: alert rather than spin. Something is wrong that restarting does not fix.
  set_status restart-budget-exhausted \
    "$recent restarts in the last $((RESTART_WINDOW / 60))m and still not answering (disk=ok); NOT restarting again -- needs human diagnosis"
  event 2 "versitygw restart budget exhausted ($recent in $((RESTART_WINDOW / 60))m); giving up, needs human diagnosis"
  exit 0
fi

# Reap an unresponsive-but-present process before starting a replacement, so we never end up with
# two gateways contending for :7070. Safe to signal: we only get here with a HEALTHY disk, so the
# process is not in uninterruptible sleep. TERM, grace, then KILL.
if ps w 2>/dev/null | grep -q '[v]ersitygw --port :7070'; then
  log "gateway present but unresponsive (http=${http_code:-none}) -- stopping it before restart"
  pids=$(ps w 2>/dev/null | grep '[v]ersitygw --port :7070' | awk '{print $1}')
  for p in $pids; do kill -TERM "$p" 2>/dev/null; done
  waited=0
  while [ "$waited" -lt 10 ] && ps w 2>/dev/null | grep -q '[v]ersitygw --port :7070'; do
    sleep 1
    waited=$((waited + 1))
  done
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  sleep 1
fi

printf '%s\n' "$now" >> "$ATTEMPTS"
# stdout goes to the INTERNAL log. The old watchdog appended to $VGW_DIR/versitygw.log on the USB,
# which had grown to 478 MB unrotated and -- worse -- made the gateway block on its own logging the
# moment the disk stalled. `ls -l /proc/<pid>/fd/1` confirmed that fd pointing at the USB.
setsid "$VGW_DIR/start.sh" < /dev/null >> "$VGW_LOG" 2>&1 &
log "restart issued (attempt $((recent + 1))/$MAX_RESTARTS in the last $((RESTART_WINDOW / 60))m)"

waited=0
started=no
while [ "$waited" -lt "$START_TIMEOUT" ]; do
  code=$("$CURL" -sk -o /dev/null -w '%{http_code}' --max-time 5 "$VGW_URL" 2>/dev/null)
  case "${code:-000}" in
    000|"") : ;;
    *) started=yes; break ;;
  esac
  sleep 2
  waited=$((waited + 2))
done

if [ "$started" = yes ]; then
  set_status restarted "gateway answering again (http=$code) after attempt $((recent + 1))/$MAX_RESTARTS"
  event 1 "versitygw was unresponsive and has been restarted successfully"
else
  set_status restart-failed "restart attempt $((recent + 1))/$MAX_RESTARTS did not come up within ${START_TIMEOUT}s (disk=ok)"
  event 2 "versitygw restart attempt $((recent + 1))/$MAX_RESTARTS failed to come up"
fi
exit 0
