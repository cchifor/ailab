#!/bin/sh
# Runs ON THE QNAP (root cron, every minute): restart the iSCSI target service when its targets are
# bound to a portal address the NAS no longer has.
# Installed by scripts/qnap-iscsi-portal-watchdog-install.sh. Do not hand-edit the deployed copy.
#
# WHY (2026-10-30, and again 2026-11-24). Every qnap-iscsi target is bound to interfaces (tbtbr0,lo).
# At boot, `scst_util --load` (S46iSCSItrgt, at uptime ~190 s) resolves each interface to its IPv4
# ONCE, through `iscsiadmin add_allow_portal`. On a cold boot tbtbr0 still holds a link-local
# placeholder (169.254.x.x) at that moment. QNAP's Thunderbolt-net init then removes tbtbr0 (~260 s)
# and rebuilds it with the service IP 10.55.0.254, and nothing re-resolves the targets. Every target
# then answers SendTargets with "No portals found", and every initiator login is rejected with
# "target not found (02/03)" (iscsiadm exit 19). No qnap-iscsi volume in the cluster mounts: infra-pg,
# gitea, openbao, prometheus and the rest stay down, and the whole estate 502s. Both times the fix was
# one `/etc/init.d/iscsitrgt.sh restart` (~80 s), which re-resolves the interfaces.
#
# Each run:
#   - A target is STALE when one of its allowed portals is an IPv4 that no local interface holds.
#     0.0.0.0 ("any"), 127.0.0.1 and non-IPv4 values are ignored.
#   - It acts only when the SAME picture (stale addresses + local addresses) shows on two runs in a
#     row. During a bridge rebuild the addresses move for a moment, and a restart in that moment
#     would bind to the half-built state again. A run that cannot judge breaks the sequence.
#   - It cannot judge, and so does nothing, when the service is (re)loading (scst_util or
#     iscsitrgt.sh running), no local address or no target can be listed, or any target's portal
#     query fails or lists nothing. A failed query is never read as "healthy".
#   - It never restarts while any iSCSI session is logged in, because a restart drops every session
#     and turns into EIO on live volumes. The count is read again right before the restart, and an
#     unparseable count counts as "sessions exist". It logs an error and a QuLog event instead.
#   - At most MAX_RESTARTS per boot, at least MIN_GAP_SECS apart. The ledger lives under /tmp, which
#     a boot clears.
#
# `--check` is read-only: it prints the verdict and exits 0 (ok), 1 (stale) or 2 (cannot judge).
# It never restarts anything and never touches the ledger. The installer uses it as its assertion.
PATH="${ISCSI_WD_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH

INIT="${ISCSI_INIT:-/etc/init.d/iscsitrgt.sh}"
STATE="${ISCSI_WD_STATE:-/tmp/iscsi-portal-watchdog}"
LOG_FILE="${ISCSI_WD_LOG:-$(dirname "$0")/watchdog.log}"
LOG_TOOL="${LOG_TOOL:-/sbin/log_tool}"
MAX_RESTARTS="${MAX_RESTARTS:-3}"
MIN_GAP_SECS="${MIN_GAP_SECS:-600}"
TAG=iscsi-portal-watchdog
RUNBOOK="runbook qnap-storage-setup.md section 12"

# Anything but no argument or exactly --check is refused: a typo in the read-only command must not
# arm a live run.
case "${1:-}" in
  "") MODE=run ;;
  --check) MODE=check ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

# Persistent action log, next to the script on the pool. The NAS's /var/log does not survive the
# reboot that this watchdog exists for. Only actions and refusals are written, never "healthy".
note() {  # $1 = syslog priority (notice|warning|err), rest = message
  pri=$1; shift
  logger -p "user.$pri" -t "$TAG" "$*" 2>/dev/null || true
  if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)" -gt 262144 ]; then
    mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
  fi
  printf '%s %s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$pri" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

# QuLog event, so the NAS's own notification pipeline sees it even while the cluster is down.
# log_tool is QNAP-specific and absent on a test box. -t 1 = warning, -t 2 = error.
event() {  # $1 = 1|2, $2 = message
  [ -x "$LOG_TOOL" ] || return 0
  "$LOG_TOOL" -t "$1" -a "$TAG: $2" >/dev/null 2>&1 || true
}

# A run that cannot judge forgets the last picture: the debounce needs two CONSECUTIVE judged runs,
# and "stale, then unknown, then stale" is not that. Loud reasons are logged once per reason key.
cannot_judge() {  # $1 = reason key, $2 = message, $3 = "loud" to log it
  if [ "$MODE" = check ]; then echo "cannot judge: $2"; exit 2; fi
  rm -f "$STATE/seen"
  if [ "${3:-}" = loud ] && [ "$(cat "$STATE/unjudged" 2>/dev/null)" != "$1" ]; then
    printf '%s' "$1" > "$STATE/unjudged"
    note err "cannot judge: $2; not acting"
  fi
  exit 0
}

# Total logged-in sessions from `iscsiadmin get_target` on stdin, or "unknown" when any target line
# lacks a parseable sess_cnt (or there are none). The restart is the risky action, so an unknown
# count must block it exactly like a live session does.
count_sessions() {
  awk '/^target = / { n++; if (match($0, /sess_cnt = [0-9]+/)) { s += substr($0, RSTART + 11, RLENGTH - 11); k++ } }
       END { if (n > 0 && k == n) print s + 0; else print "unknown" }'
}

# --- 1. one run at a time (run mode only; --check is read-only) ----------------------------------
if [ "$MODE" = run ]; then
  mkdir -p "$STATE" 2>/dev/null || exit 0
  LOCK="$STATE/lock"
  if ! mkdir "$LOCK" 2>/dev/null; then
    pid=$(cat "$LOCK/pid" 2>/dev/null)
    if [ -n "$pid" ]; then
      kill -0 "$pid" 2>/dev/null && exit 0     # a live run (a restart takes ~80 s) holds it
    else
      # Created but no pid yet: give its owner three runs to write one before reclaiming.
      n=$(( $(cat "$STATE/lock.skips" 2>/dev/null || echo 0) + 1 ))
      echo "$n" > "$STATE/lock.skips"
      [ "$n" -lt 3 ] && exit 0
    fi
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || exit 0
  fi
  rm -f "$STATE/lock.skips"
  echo $$ > "$LOCK/pid"
  trap 'rm -rf "$LOCK"' EXIT
  trap 'exit 1' INT TERM HUP
fi

# --- 2. never judge while the service is (re)loading ---------------------------------------------
if ps 2>/dev/null | grep -v grep | grep -qE 'scst_util|iscsitrgt\.sh'; then
  cannot_judge busy "the iSCSI target service is (re)loading"
fi

# --- 3. what the NAS holds, and what the targets are bound to ------------------------------------
addrs=$(ip -4 -o addr show 2>/dev/null | awk '{ sub(/\/.*/, "", $4); if ($4 != "") print $4 }' | sort -u)
[ -n "$addrs" ] || cannot_judge addresses "no local IPv4 address could be listed" loud

targets=$(iscsiadmin get_target 2>/dev/null)
iqns=$(printf '%s\n' "$targets" | sed -n 's/^target = \([^,]*\),.*/\1/p')
# No targets: the service is stopped or still starting. Not the failure this watchdog handles.
[ -n "$iqns" ] || cannot_judge no-targets "iscsiadmin lists no targets"
ntargets=$(printf '%s\n' "$iqns" | wc -l | tr -d ' ')
sessions=$(printf '%s\n' "$targets" | count_sessions)

# Each target's query must succeed (exit 0; an unknown target exits 2 with "Target doesn't exist")
# AND list at least one portal. Anything else is a failed inspection, never an empty = healthy one.
stale=""
unjudged=0
first_unjudged=""
for q in $iqns; do
  if ! out=$(iscsiadmin get_allow_portal "$q" 2>/dev/null); then out=""; fi
  portals=$(printf '%s\n' "$out" | sed -n 's/^Allow portal = *//p')
  if [ -z "$portals" ]; then
    unjudged=$((unjudged + 1))
    [ -n "$first_unjudged" ] || first_unjudged=$q
    continue
  fi
  for p in $portals; do
    case "$p" in 0.0.0.0|127.0.0.1|*:*) continue ;; esac
    case "$p" in [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;; *) continue ;; esac
    printf '%s\n' "$addrs" | grep -qxF "$p" && continue
    stale="$stale$q $p
"
  done
done
if [ "$unjudged" -gt 0 ]; then
  cannot_judge portals "get_allow_portal failed or listed no portal for $unjudged of $ntargets targets (first: $first_unjudged)" loud
fi

local_list=$(printf '%s\n' "$addrs" | tr '\n' ' ' | sed 's/ *$//')

# --- 4. healthy ----------------------------------------------------------------------------------
if [ -z "$stale" ]; then
  if [ "$MODE" = check ]; then
    echo "ok: $ntargets targets, every allowed portal is a live local address; sessions=$sessions"
    exit 0
  fi
  # The restart ledger stays for the rest of the boot. Only the "already said so" flags reset, so a
  # later, separate stale event is reported again.
  rm -f "$STATE/seen" "$STATE/refused" "$STATE/exhausted" "$STATE/unjudged"
  if [ -f "$STATE/pending" ]; then
    rm -f "$STATE/pending"
    note notice "recovered: all $ntargets targets are bound to live addresses again (sessions=$sessions)"
    event 1 "recovered: all $ntargets iSCSI targets are bound to live addresses again"
  fi
  exit 0
fi

nstale=$(printf '%s' "$stale" | grep -c .)
stale_ips=$(printf '%s' "$stale" | awk '{ print $2 }' | sort -u | tr '\n' ' ' | sed 's/ *$//')
what="$nstale binding(s) on $ntargets targets point at $stale_ips, which no local interface holds (local: $local_list)"

if [ "$MODE" = check ]; then
  echo "STALE: $what; sessions=$sessions"
  exit 1
fi

# --- 5. stale: debounce, then the safety gates, then at most one restart -------------------------
sig="$stale_ips|$local_list"
prev=$(cat "$STATE/seen" 2>/dev/null)
printf '%s' "$sig" > "$STATE/seen"
rm -f "$STATE/unjudged"
if [ "$prev" != "$sig" ]; then
  note warning "$what; re-checking on the next run before acting"
  exit 0
fi

refuse() {  # $1 = session count (a number above 0, or "unknown")
  # Once per picture: this repeats every minute until a person acts, and a repeat adds nothing.
  if [ "$(cat "$STATE/refused" 2>/dev/null)" != "$sig" ]; then
    printf '%s' "$sig" > "$STATE/refused"
    note err "$what, but sessions=$1; NOT restarting (a restart drops every live session). Restart by hand ($RUNBOOK)"
    event 2 "$nstale target binding(s) on vanished $stale_ips with sessions=$1; not restarting automatically ($RUNBOOK)"
  fi
  exit 0
}
[ "$sessions" = 0 ] || refuse "$sessions"

done_n=$(grep -c . "$STATE/restarts" 2>/dev/null)
done_n=${done_n:-0}
if [ "$done_n" -ge "$MAX_RESTARTS" ]; then
  if [ ! -f "$STATE/exhausted" ]; then
    : > "$STATE/exhausted"
    note err "$what; restart budget spent ($done_n this boot). Not restarting again ($RUNBOOK)"
    event 2 "iSCSI targets still bound to vanished $stale_ips after $done_n restarts; manual action needed ($RUNBOOK)"
  fi
  exit 0
fi

now=$(date +%s)
last=$(tail -n 1 "$STATE/restarts" 2>/dev/null)
if [ -n "$last" ] && [ $((now - last)) -lt "$MIN_GAP_SECS" ]; then
  exit 0
fi

# The count above was read before the portal scan. An initiator can log in to a still-reachable
# target since then, so read it again as the very last thing before the restart. (What remains is
# the moment between this read and the restart itself.)
sessions=$(iscsiadmin get_target 2>/dev/null | count_sessions)
[ "$sessions" = 0 ] || refuse "$sessions"

echo "$now" >> "$STATE/restarts"
note warning "$what; sessions=0; restarting the iSCSI target service (restart $((done_n + 1))/$MAX_RESTARTS)"
event 1 "restarting the iSCSI target service: $nstale target binding(s) on vanished $stale_ips"
"$INIT" restart >> "$STATE/restart.log" 2>&1 < /dev/null
rc=$?
rm -f "$STATE/seen"
: > "$STATE/pending"
note notice "$INIT restart exited $rc; the next runs verify the bindings"
exit 0
