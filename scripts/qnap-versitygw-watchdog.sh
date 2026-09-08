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
#      probe while DELIBERATELY LEAVING THE LOCK HELD in the name of that probe's PROCESS GROUP.
#      The group, not the subshell: mkdir/cat/rm run as its CHILDREN, so SIGKILL can reap the
#      subshell while the command actually stuck on the disk lives on. Every later cron run sees a
#      live lock holder and exits immediately, converting the old 25-copy pile-up into a hard
#      ceiling of one. When the disk recovers the abandoned group empties and the next run reclaims
#      the stale lock -- self-healing with no manual step.
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
# `versitygw-probe` (kubernetes/apps/backup/talos-backup/versitygw-probe.yaml),
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
MAX_LOG_BYTES="${MAX_LOG_BYTES:-20971520}" # 20 MiB, then copy-truncate; keep 1 saved generation
STALE_LOCK_SECS="${STALE_LOCK_SECS:-3600}" # backstop for a lock that never got an ownership stamp
RECLAIM_GUARD_SECS="${RECLAIM_GUARD_SECS:-300}" # the reclaim mutex is held for ms; older = a run died in it
ABANDONED_LOCK_SECS="${ABANDONED_LOCK_SECS:-86400}" # backstop so a recycled pgid cannot disable the watchdog forever

STATE="$BASE/state"
LOCK="$STATE/lock"
RECLAIM="$STATE/reclaim"   # second mutex: serialises stale-lock reclamation only
STATUS="$STATE/status"
ATTEMPTS="$STATE/attempts"
WD_LOG="$BASE/watchdog.log"
VGW_LOG="$BASE/versitygw.log"   # versitygw's OWN stdout, moved OFF the USB (see redirect below)

CURL="${CURL:-/sbin/curl}"
# Overridable ONLY so the /proc scanners below can be unit-tested against a synthetic tree.
# Nothing in normal operation should ever set this.
PROC_ROOT="${PROC_ROOT:-/proc}"

#--- logging ------------------------------------------------------------------------------------
rotate() {  # $1 = logfile. Size-capped so an unbounded log can never fill the internal pool.
  # COPY-then-TRUNCATE, never rename. versitygw is started below with `>>` and holds an open fd on
  # $VGW_LOG for its entire lifetime, so `mv` would leave it appending to the RENAMED file forever:
  # unbounded, and never size-checked again because rotate() only ever stats the original path.
  # That is exactly the 478 MB unrotated log this script exists to prevent, merely relocated onto
  # the internal pool. Truncating in place keeps the writer's fd on the same inode, and because the
  # redirect is `>>` (O_APPEND) the next write lands at offset 0 rather than leaving a sparse hole.
  local f=$1 sz
  [ -f "$f" ] || return 0
  sz=$(wc -c < "$f" 2>/dev/null || echo 0)
  [ "${sz:-0}" -gt "$MAX_LOG_BYTES" ] || return 0
  rm -f "$f.1"
  cp -f "$f" "$f.1" 2>/dev/null || return 0   # keep one generation; never truncate what we failed to save
  : > "$f"
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

# Resolve a path's backing device WITHOUT touching that path's filesystem.
#
# `df`/`stat` issue a statfs() against the mount, and statfs on a wedged USB BLOCKS. This guard runs
# in the parent, before the lock is taken, so a blocking call here would let every 3-minute cron run
# stack up with no mutual exclusion -- reproducing the original pile-up in the very code meant to
# prevent it. /proc/mounts is generated by the kernel from its mount table, so reading it never
# touches the filesystem being described. Longest-prefix match, because mount points nest.
#
# This compares the configured paths literally; it does not resolve symlinks (readlink -f would
# stat the target, which is the thing we must not do here). The installer does the thorough,
# statfs-based check at install time, when the disk is known healthy.
dev_of_path() {
  awk -v p="$1" '
    { dev = $1; mp = $2 }
    mp == "/" || index(p "/", mp "/") == 1 {
      if (length(mp) > n) { n = length(mp); d = dev }
    }
    END { print d }
  ' "$PROC_ROOT/mounts" 2>/dev/null
}

# Prove the base is on a different block device than the gateway's disk. A symlink or a remount
# could otherwise quietly put our lock and logs back onto the USB, re-arming the original bug.
base_dev=$(dev_of_path "$BASE")
vgw_dev=$(dev_of_path "$VGW_DIR")
if [ -n "$base_dev" ] && [ "$base_dev" = "$vgw_dev" ]; then
  echo "FATAL: supervisor base and gateway disk are the same device ($base_dev) -- refusing" >&2
  exit 1
fi
rotate "$WD_LOG"
rotate "$VGW_LOG"

#--- invariant 2: a single atomic lock, held across abandonment -----------------------------------
# `mkdir` is atomic on ext4/ZFS, which is why it is the lock primitive here: the NAS has no flock.
#
# The lock names an OWNER, and which owner it names changes over the run:
#
#   kind=supervisor       this script, identified by pid + /proc start-time (so a recycled pid
#                         number cannot make a dead lock look alive forever). Held for the WHOLE
#                         run, including the stop/restart sequence. If the lock named only the
#                         probe, the probe's normal exit would make the lock look stale while the
#                         supervisor was still mid-restart, and a concurrent run could start a
#                         second gateway on :7070.
#
#   kind=abandoned-probe  a probe left behind in uninterruptible sleep, identified by its PROCESS
#                         GROUP. Tracking the group rather than the subshell's pid is essential:
#                         mkdir/cat/rm run as CHILDREN of the probe subshell, so SIGKILL can reap
#                         the subshell while the command actually blocked on the disk survives.
#                         Watching only the subshell would conclude "killable", release the lock,
#                         and let probes accumulate again -- which is the original bug.
proc_starttime() { awk '{print $22}' "$PROC_ROOT/$1/stat" 2>/dev/null; }

# Is ANY process still in process group $1? Pure bash over /proc; never touches the USB.
#
# TOLERANT BY CONSTRUCTION, and that is the whole point. An earlier version passed the glob to a
# single `awk /proc/[0-9]*/stat`, but the glob is expanded by the SHELL before awk opens anything, so
# any process exiting in that window makes awk fail to open a file and exit non-zero. The caller
# reads that as "the group is gone" and would reclaim an abandoned probe's lock while its D-state
# descendant is still alive -- turning a routine event on a busy NAS into a breach of the
# single-probe invariant. Here an unreadable or vanished entry is skipped, and only a genuine
# absence of every member returns failure.
#
# No forks: `read < file` and ${...} expansions are builtins, so this stays cheap enough to call
# once a second against ~500 processes. /proc/<pid>/stat field 5 is pgrp, but comm can contain
# spaces and parentheses, so strip through the LAST ") " -- greedy, which is correct even for a comm
# containing ") ". The remainder starts "state ppid pgrp session ...", none of which can glob.
group_alive() {
  local want=${1:-} d line rest
  [ -n "$want" ] || return 1
  for d in "$PROC_ROOT"/[0-9]*; do
    read -r line < "$d/stat" 2>/dev/null || continue   # exited mid-scan: skip, never conclude "dead"
    [ -n "$line" ] || continue
    rest=${line##*') '}
    set -- $rest
    [ "${3:-}" = "$want" ] && return 0
  done
  return 1
}

holder_alive() {
  local kind pid st
  kind=$(cat "$LOCK/kind" 2>/dev/null)
  case "$kind" in
    abandoned-*)   # abandoned-probe or abandoned-start: both are owned by a process GROUP
      local pgid lead_st age
      pgid=$(cat "$LOCK/pgid" 2>/dev/null)
      group_alive "$pgid" || return 1
      # The group looks alive -- but a pgid is just a number, and it can be RECYCLED by an unrelated
      # group. Without a check, that would keep the supervisor exiting on every run, forever, with
      # nothing running. Two independent guards:
      #   a) if the group LEADER still exists, its /proc start-time must match what was recorded at
      #      abandonment. A different start-time means a different process wearing the same number.
      #   b) a wall-clock backstop, because when the leader is gone the members cannot be checked
      #      that way. Being wrong here costs one extra probe (which simply wedges and re-abandons
      #      if the disk is still bad); never being able to run again costs the whole watchdog.
      lead_st=$(cat "$LOCK/pgid_starttime" 2>/dev/null)
      if [ -n "${lead_st:-}" ] && [ -d "$PROC_ROOT/$pgid" ]; then
        [ "$(proc_starttime "$pgid")" = "$lead_st" ] || return 1
      fi
      age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
      [ "$age" -lt "$ABANDONED_LOCK_SECS" ]
      return $? ;;   # explicit: without it, execution falls past esac into the supervisor checks,
                     # which look for a pid file an abandoned lock does not have and report "dead"
  esac
  [ -f "$LOCK/pid" ] || return 1
  pid=$(cat "$LOCK/pid" 2>/dev/null)
  [ -n "${pid:-}" ] || return 1
  [ -d "$PROC_ROOT/$pid" ] || return 1
  st=$(proc_starttime "$pid")
  # No stored start-time (older lock) -> fall back to pid liveness alone.
  [ -f "$LOCK/starttime" ] || return 0
  [ "$st" = "$(cat "$LOCK/starttime" 2>/dev/null)" ]
}

# Publishing ownership is part of ACQUIRING the lock, not something that happens afterwards. An
# unstamped lock is indistinguishable from a dead one, so any gap between `mkdir` and these writes is
# a window in which a concurrent run can delete a lock that was just legitimately taken. Every
# successful mkdir below is followed immediately by this, and when reclaiming it happens while the
# RECLAIM guard is still held.
claim_ownership() {
  printf '%s' supervisor             > "$LOCK/kind"
  printf '%s' "$$"                   > "$LOCK/pid"
  printf '%s' "$(proc_starttime $$)" > "$LOCK/starttime"
}

# A lock that exists but carries NO ownership stamp belongs to a run caught between mkdir and
# claim_ownership. It must be left alone, not reclaimed. The age is re-read at each call site rather
# than reused, because a snapshot taken before another run recreated the lock describes a directory
# that no longer exists -- which is precisely how a "fresh" lock gets mistaken for an ancient one.
lock_being_claimed() {
  local age
  [ -f "$LOCK/kind" ] && return 1
  [ -f "$LOCK/pid" ]  && return 1
  age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
  [ "$age" -lt "$STALE_LOCK_SECS" ]
}

if mkdir "$LOCK" 2>/dev/null; then
  claim_ownership
else
  if holder_alive; then
    # THE pile-up guard. This is the normal, healthy response while a probe is outstanding -- and
    # the load-bearing one while a probe is wedged in D-state. Exit silently: at 3-minute cron
    # granularity, logging here would itself become the noise.
    exit 0
  fi
  # Stale: the holder is gone. But a lock still being claimed is not stale -- back off.
  # (A lock carrying a pid but no kind is one left by the PREVIOUS version of this script;
  # holder_alive has already established it is dead, so it is reclaimable immediately rather than
  # stalling every run for STALE_LOCK_SECS after an upgrade.)
  lock_being_claimed && exit 0
  lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))

  # Reclaim under a SECOND mutex, held until ownership is PUBLISHED.
  #
  # Neither a bare `rm -rf` + `mkdir` nor a rename is sufficient, because the dangerous interleaving
  # is not two runs racing to delete -- it is the second run deleting the FIRST run's freshly created
  # lock, acting on an observation it made before that lock existed. Serialising reclamation closes
  # it only if the guard is still held when the winner becomes identifiable: releasing RECLAIM before
  # claim_ownership would leave the winner's lock unstamped and therefore deletable by the next run
  # through the guard. So RECLAIM spans mkdir AND the ownership writes, and the guarded re-check
  # additionally refuses to delete a lock that is mid-claim.
  if ! mkdir "$RECLAIM" 2>/dev/null; then
    # The guard is held for milliseconds, so an old one means a run died inside it.
    reclaim_age=$(( $(date +%s) - $(stat -c %Y "$RECLAIM" 2>/dev/null || date +%s) ))
    [ "$reclaim_age" -lt "$RECLAIM_GUARD_SECS" ] && exit 0
    log "breaking abandoned reclaim guard (age ${reclaim_age}s)"
    rm -rf "$RECLAIM" 2>/dev/null
    mkdir "$RECLAIM" 2>/dev/null || exit 0
  fi
  if mkdir "$LOCK" 2>/dev/null; then
    claim_ownership                     # the holder vanished entirely; the lock is simply ours now
  elif holder_alive; then
    rm -rf "$RECLAIM" 2>/dev/null       # another run reclaimed while we waited -- it owns it
    exit 0
  elif lock_being_claimed; then
    rm -rf "$RECLAIM" 2>/dev/null       # a run took it between our first check and this one
    exit 0
  else
    rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { rm -rf "$RECLAIM" 2>/dev/null; exit 0; }
    claim_ownership
  fi
  rm -rf "$RECLAIM" 2>/dev/null
  log "reclaimed stale lock (age ${lock_age}s)"
fi
# The lock is ours and stamped. It is released on every exit EXCEPT deliberate abandonment (below).
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
# `set -m` (job control) makes bash place each background job in its OWN process group, whose id
# equals the job's pid. That is what makes the group addressable as a unit below -- both to signal
# (`kill -9 -PGID`) and, more importantly, to ASK whether anything from the probe is still alive.
# Without it, mkdir/cat/rm would sit in the supervisor's own group and could not be distinguished
# from it. Job-control notifications are suppressed; cron discards stderr anyway, but the installer
# runs this in the foreground and they would be noise there.
set -m
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
) >/dev/null 2>&1 &
probe_pgid=$!   # == the group id, because of set -m
# stdout is closed off deliberately, not just for tidiness: the probe's children INHERIT it, so a
# child blocked on the disk would hold the supervisor's stdout open for as long as it is stuck.
# Anything reading that stream -- the installer's verify step, a cron MAILTO, an operator running
# this by hand -- would then block on the wedged disk despite the supervisor itself having exited
# cleanly. The probe reports through $PROBE_RESULT, never through stdout, so nothing is lost.
set +m
printf '%s' "$probe_pgid" > "$LOCK/pgid"

# Poll the GROUP -- never `wait`. `wait` on a D-state child blocks forever and would hang the
# supervisor, which is the exact failure being engineered out. Polling the group rather than the
# subshell also means a blocked mkdir/cat/rm keeps the probe "outstanding" even after its parent
# subshell has gone.
waited=0
while [ "$waited" -lt "$DISK_TIMEOUT" ]; do
  group_alive "$probe_pgid" || break
  sleep 1
  waited=$((waited + 1))
done

if group_alive "$probe_pgid"; then
  # Deadline hit. Signal the WHOLE GROUP: killing only the subshell would leave the command that is
  # actually blocked on the disk running, and the check below would then wrongly report it gone.
  kill -9 -"$probe_pgid" 2>/dev/null
  sleep 1
  if group_alive "$probe_pgid"; then
    # Something in the group survived SIGKILL => uninterruptible sleep on hung I/O. Nothing local
    # can fix this: not a restart, not a kill. Hand the lock to the abandoned group so no successor
    # ever stacks on top of it, and leave. The lock is reclaimed automatically once the group dies.
    RELEASE_LOCK=no
    printf '%s' abandoned-probe            > "$LOCK/kind"
    printf '%s' "$(proc_starttime "$probe_pgid")" > "$LOCK/pgid_starttime"
    set_status disk-wedged \
      "disk probe group $probe_pgid unkillable in D-state after ${DISK_TIMEOUT}s (http=${http_code:-none}); NOT restarting -- a restart cannot repair hung I/O. Lock held until every member of that group clears. Storage intervention required."
    event 2 "USB disk wedged: probe group $probe_pgid stuck in D-state; versitygw NOT restarted (restart cannot fix hung I/O)"
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
# Monitor mode is OFF here, so `setsid` does not need to fork: it calls setsid(2) and execs, making
# $! both the new session leader and its process-group id. That is what lets a wedged STARTUP be
# tracked the same way a wedged probe is, below.
setsid "$VGW_DIR/start.sh" < /dev/null >> "$VGW_LOG" 2>&1 &
start_pgid=$!
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
  # The startup reads start.sh and the gateway binary FROM THE USB, so it can wedge exactly as a
  # probe can -- a disk probe that passed seconds ago is not a guarantee the disk still answers.
  # `ps | grep 'versitygw --port'` cannot see a process stuck BEFORE exec, so without this a blocked
  # startup would be invisible, the lock would be released, and the next run would stack another one
  # behind it. Same treatment as a wedged probe: reap the group; if anything survives SIGKILL, keep
  # the lock in that group's name so nothing can accumulate behind it.
  if group_alive "$start_pgid"; then
    kill -TERM -"$start_pgid" 2>/dev/null
    sleep 2
    kill -9 -"$start_pgid" 2>/dev/null
    sleep 1
    if group_alive "$start_pgid"; then
      RELEASE_LOCK=no
      printf '%s' abandoned-start            > "$LOCK/kind"
      printf '%s' "$start_pgid"              > "$LOCK/pgid"
      printf '%s' "$(proc_starttime "$start_pgid")" > "$LOCK/pgid_starttime"
      set_status start-wedged \
        "restart attempt $((recent + 1))/$MAX_RESTARTS never answered and its startup group $start_pgid is unkillable in D-state -- the disk wedged after the probe passed. Lock held until that group clears. Storage intervention required."
      event 2 "versitygw startup wedged in D-state (group $start_pgid); lock held so no further restarts stack behind it"
      exit 0
    fi
  fi
  set_status restart-failed "restart attempt $((recent + 1))/$MAX_RESTARTS did not come up within ${START_TIMEOUT}s (disk=ok)"
  event 2 "versitygw restart attempt $((recent + 1))/$MAX_RESTARTS failed to come up"
fi
exit 0
