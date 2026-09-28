#!/bin/sh
# cnpg-lost-slot-reclone: re-clone a CNPG replica whose physical replication slot is LOST.
#
# Runs as the main container of the cnpg-lost-slot-reclone CronJob (kubernetes/apps/databases/
# cnpg-lost-slot-reclone.yaml, which holds the why). An init container has already written SLOTS_FILE
# with the primary's slot state (see probe in that manifest):
#   line 1:  in_recovery=<true|false>
#   lines 2+: <slot_name>|<slot_type>|<active true/false>|<wal_status>|<safe_wal_size>|<invalidation_reason>
# (booleans concatenated into text come out as true/false; bare psql -At output would be t/f - both accepted)
# This script only talks to the Kubernetes API (kubectl + jq, namespaced RBAC). POSIX sh so that
# scripts/tests/cnpg-lost-slot-reclone-mock.py can drive it with a fake kubectl on PATH.
#
# Decision (plans/2026-09-28-gitea-actions-retention-and-cnpg-reclone-plan.md):
#   for each cluster in CLUSTERS (the Cluster is read ONCE as JSON; a failed read is a failure, never
#   "no annotations"):
#     in_recovery must be false; phase must be "Cluster in healthy state"; a currentPrimary must exist
#     every REPLICA in .status.instanceNames is mapped to its expected slot (slotPrefix + name with
#       '-' -> '_'); a replica is healthy when that slot is physical, active and reserved; it is
#       actionable when the slot is physical, INACTIVE and LOST; anything else is logged, never acted on
#     marker ailab.io/reclone-in-progress present -> observe only: clear it (and stamp last-reclone)
#       once instances are complete, the old instance is gone and EVERY replica is healthy; older
#       than STUCK_AFTER_SECONDS and still unverified -> exit 1 (KubeJobFailed pages)
#     the pod must carry cnpg.io/instanceRole=replica, its PVC(s) must exist, and ailab.io/last-reclone
#       must be older than MIN_INTERVAL_SECONDS
#     re-read the Cluster and the pod immediately before mutation (primary, phase, both annotations,
#       role unchanged), take the marker with a resourceVersion precondition (a competing execution
#       cannot overwrite it), then delete PVCs -> pod -> bounded wait until pod and PVC UIDs are gone;
#       an API error while waiting is "unknown", never "gone". One re-clone per execution.
# Exit: 0 acted or nothing to do; 1 failure/refusal/stuck; 3 probe saw a non-primary (switchover).
set -eu

: "${NAMESPACE:?}" "${CLUSTERS:?}"
: "${SLOTS_FILE:=/work/slots.tsv}" "${DRY_RUN:=true}" "${MIN_INTERVAL_SECONDS:=21600}"
: "${STUCK_AFTER_SECONDS:=2700}" "${DELETE_WAIT_SECONDS:=150}" "${POLL_SECONDS:=5}" "${WORK:=/tmp}"
IN_PROGRESS=ailab.io/reclone-in-progress
LAST=ailab.io/last-reclone
HEALTHY="Cluster in healthy state"

k() { kubectl -n "$NAMESPACE" --request-timeout=20s "$@"; }
now() { date -u +%s; }
ts() { date -u +%FT%TZ; }
epoch_of() { # 2026-09-28T10:00:00Z -> epoch (GNU date or busybox); non-zero (nothing printed) when unparseable
  date -u -d "$1" +%s 2>/dev/null || date -u -D %Y-%m-%dT%H:%M:%SZ -d "$1" +%s 2>/dev/null
}
read_cluster() { # name -> $WORK/cluster.json; non-zero on ANY API failure
  k get cluster "$1" -o json > "$WORK/cluster.json" 2> "$WORK/err" || { echo "$1: cannot read Cluster: $(head -c 200 "$WORK/err" | tr -d '\n\r')"; return 1; }
  jq -e . "$WORK/cluster.json" > /dev/null 2>&1 || { echo "$1: Cluster response is not JSON"; return 1; }
}
cf() { jq -r "$1" "$WORK/cluster.json" | tr -d '\r'; }
pod_role() { k get pod "$1" -o "jsonpath={.metadata.labels.cnpg\.io/instanceRole}" 2>/dev/null | tr -d '\r' || true; }
present() { # kind name [uid] -> 0 present, 1 gone (NotFound, or replaced by a same-named object), 2 unknown (API error)
  if k get "$1" "$2" -o jsonpath='{.metadata.uid}' > "$WORK/out" 2> "$WORK/err"; then
    if [ -n "${3:-}" ] && [ "$(tr -d '\r' < "$WORK/out")" != "$3" ]; then return 1; fi
    return 0
  fi
  if grep -q NotFound "$WORK/err"; then return 1; fi
  return 2
}
expected_slot() { printf '%s%s' "$prefix" "$(printf '%s' "$1" | tr '-' '_')"; }

[ -s "$SLOTS_FILE" ] || { echo "probe output $SLOTS_FILE missing or empty"; exit 1; }
in_recovery=$(sed -n '1s/^in_recovery=//p' "$SLOTS_FILE" | tr -d '\r')
case "$in_recovery" in
  f|false) ;;
  t|true) echo "probe reached a server in recovery (rw service mid-switchover) - not acting"; exit 3 ;;
  *) echo "probe output malformed: first line '$(sed -n 1p "$SLOTS_FILE")'"; exit 1 ;;
esac
tail -n +2 "$SLOTS_FILE" | tr -d '\r' > "$WORK/slots.body"

acted=0; rc=0
for cluster in $CLUSTERS; do
  read_cluster "$cluster" || { rc=1; continue; }
  phase=$(cf '.status.phase // ""'); primary=$(cf '.status.currentPrimary // ""')
  instances=$(cf '(.status.instanceNames // []) | join(" ")'); want=$(cf '.spec.instances // 0')
  prefix=$(cf '.spec.replicationSlots.highAvailability.slotPrefix // "_cnpg_"')
  marker=$(cf ".metadata.annotations[\"$IN_PROGRESS\"] // \"\""); last=$(cf ".metadata.annotations[\"$LAST\"] // \"\"")
  if [ -z "$primary" ] || [ -z "$instances" ]; then echo "$cluster: no currentPrimary/instanceNames in status - skipping"; rc=1; continue; fi

  # --- every replica against its expected slot ------------------------------------------------------
  lost_inst=""; lost_slot=""; replicas_healthy=1; nrep=0
  for inst in $instances; do
    [ "$inst" = "$primary" ] && continue
    nrep=$((nrep + 1)); exp=$(expected_slot "$inst")
    line=$(grep "^$exp|" "$WORK/slots.body" | head -n 1 || true)
    if [ -z "$line" ]; then echo "$cluster: replica $inst has no slot $exp on the primary"; replicas_healthy=0; continue; fi
    stype=$(printf '%s' "$line" | cut -d'|' -f2); active=$(printf '%s' "$line" | cut -d'|' -f3); wal=$(printf '%s' "$line" | cut -d'|' -f4)
    case "$active" in t|true) active=t ;; *) active=f ;; esac
    if [ "$stype" = physical ] && [ "$active" = t ] && [ "$wal" = reserved ]; then continue; fi
    replicas_healthy=0
    if [ "$stype" = physical ] && [ "$active" = f ] && [ "$wal" = lost ]; then
      if [ -z "$lost_inst" ]; then lost_inst="$inst"; lost_slot="$exp"; else echo "$cluster: slot $exp of $inst is also lost - one re-clone per execution"; fi
    else
      echo "$cluster: slot $exp of $inst is $stype/active=$active/$wal - not actionable"
    fi
  done
  # slots with our prefix that belong to no replica: the primary's own or a stale one - logged only
  pexp=$(expected_slot "$primary")
  while IFS='|' read -r slot stype active wal _rest; do
    [ -n "$slot" ] || continue
    case "$slot" in "$prefix"*) ;; *) continue ;; esac
    mapped=0; for inst in $instances; do [ "$inst" != "$primary" ] && [ "$(expected_slot "$inst")" = "$slot" ] && mapped=1; done
    [ "$mapped" = 1 ] && continue
    if [ "$slot" = "$pexp" ]; then echo "$cluster: slot $slot belongs to the PRIMARY $primary ($stype/active=$active/$wal) - never actionable"
    else echo "$cluster: slot $slot maps to no replica ($stype/active=$active/$wal) - ignoring"; fi
  done < "$WORK/slots.body"

  # --- marker present: observe, verify, clear; never act --------------------------------------------
  if [ -n "$marker" ]; then
    mts=${marker%%/*}; minst=${marker#*/}
    # an unparseable marker timestamp is treated as stuck (fail closed), never as fresh
    if mep=$(epoch_of "$mts"); then age=$(( $(now) - mep )); else age=$((STUCK_AFTER_SECONDS + 1)); fi
    gone=1; for inst in $instances; do [ "$inst" = "$minst" ] && gone=0; done
    if [ "$phase" = "$HEALTHY" ] && [ "$((nrep + 1))" = "$want" ] && [ "$gone" = 1 ] && [ "$replicas_healthy" = 1 ]; then
      echo "$cluster: re-clone of $minst verified (replicas=$nrep/$((want - 1)) all active+reserved, old instance gone) - clearing marker"
      if [ "$DRY_RUN" != "true" ]; then
        k annotate cluster "$cluster" "$LAST=$(ts)/$minst" "$IN_PROGRESS-" --overwrite > /dev/null || { echo "$cluster: could not clear the marker"; rc=1; }
      fi
    elif [ "$age" -gt "$STUCK_AFTER_SECONDS" ]; then
      echo "$cluster: re-clone of $minst started ${age}s ago and is NOT verified (phase='$phase' replicas=$nrep/$((want - 1)) gone=$gone healthy=$replicas_healthy) - operator needed"; rc=1
    else
      echo "$cluster: re-clone of $minst in progress (${age}s, phase='$phase' replicas=$nrep/$((want - 1)) healthy=$replicas_healthy) - observing"
    fi
    continue
  fi

  if [ "$phase" != "$HEALTHY" ]; then echo "$cluster: phase='$phase' - not acting"; continue; fi
  if [ -z "$lost_inst" ]; then echo "$cluster: no lost slot (primary $primary, instances: $instances)"; continue; fi
  if [ "$acted" = 1 ]; then echo "$cluster: $lost_inst has a lost slot but this execution already acted - next run"; continue; fi

  # --- guards ----------------------------------------------------------------------------------------
  role=$(pod_role "$lost_inst")
  if [ "$role" != replica ]; then echo "$cluster: $lost_inst role='$role' (pod missing or not a replica) - refusing"; rc=1; continue; fi
  if [ -n "$last" ]; then
    # an unparseable last-reclone timestamp is a refusal, not "long ago" (the budget must fail closed)
    if ! lep=$(epoch_of "${last%%/*}"); then echo "$cluster: last re-clone annotation '$last' has no parseable timestamp - refusing"; rc=1; continue; fi
    lage=$(( $(now) - lep ))
    if [ "$lage" -lt "$MIN_INTERVAL_SECONDS" ]; then echo "$cluster: last re-clone ($last) was ${lage}s ago < $MIN_INTERVAL_SECONDS - refusing (budget)"; rc=1; continue; fi
  fi
  pvcs=$(k get pvc "$lost_inst" "$lost_inst-wal" --ignore-not-found -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}={.spec.volumeName}{"\n"}{end}' 2>/dev/null | tr -d '\r' || true)
  case "$pvcs" in "$lost_inst="*) ;; *) echo "$cluster: no PVC named $lost_inst - refusing"; rc=1; continue ;; esac
  echo "$cluster: slot $lost_slot LOST -> replica $lost_inst must be re-cloned; claims: $(printf '%s' "$pvcs" | tr '\n' ' ')"
  if [ "$DRY_RUN" = "true" ]; then echo "$cluster: DRY_RUN - would annotate $IN_PROGRESS, delete pvc(s) and pod $lost_inst"; acted=1; continue; fi

  # --- final re-read, then mutate under a resourceVersion precondition ----------------------------------
  read_cluster "$cluster" || { rc=1; continue; }
  p2=$(cf '.status.currentPrimary // ""'); ph2=$(cf '.status.phase // ""'); r2=$(pod_role "$lost_inst")
  m2=$(cf ".metadata.annotations[\"$IN_PROGRESS\"] // \"\""); l2=$(cf ".metadata.annotations[\"$LAST\"] // \"\""); rv=$(cf '.metadata.resourceVersion // ""')
  if [ "$p2" != "$primary" ] || [ "$ph2" != "$HEALTHY" ] || [ "$r2" != replica ] || [ -n "$m2" ] || [ "$l2" != "$last" ] || [ -z "$rv" ]; then
    echo "$cluster: state changed before mutation (primary $primary->$p2, phase '$ph2', role '$r2', marker '$m2', last '$l2') - aborting"; rc=1; continue
  fi
  if ! k annotate cluster "$cluster" "$IN_PROGRESS=$(ts)/$lost_inst" --resource-version="$rv" > /dev/null 2> "$WORK/err"; then
    echo "$cluster: could not take the marker at resourceVersion $rv ($(head -c 160 "$WORK/err" | tr -d '\n\r')) - aborting"; rc=1; continue
  fi
  names=$(printf '%s\n' "$pvcs" | cut -d= -f1 | tr '\n' ' ')
  # PVCs first (they stay Terminating under pvc-protection until the pod is gone), then the pod
  # shellcheck disable=SC2086  # $names is a space-separated list on purpose
  k delete pvc $names --ignore-not-found --wait=false
  k delete pod "$lost_inst" --wait=false --ignore-not-found
  acted=1
  deadline=$(( $(now) + DELETE_WAIT_SECONDS )); done_wait=0
  while :; do
    remaining=0
    pr=0; present pod "$lost_inst" || pr=$?
    [ "$pr" = 1 ] || remaining=1
    for line in $pvcs; do
      name=${line%%=*}; rest=${line#*=}; uid=${rest%%=*}
      pr=0; present pvc "$name" "$uid" || pr=$?
      [ "$pr" = 1 ] || remaining=1
    done
    if [ "$remaining" = 0 ]; then done_wait=1; break; fi
    [ "$(now)" -lt "$deadline" ] || break
    sleep "$POLL_SECONDS"
  done
  pvs=$(printf '%s\n' "$pvcs" | cut -d= -f3 | tr '\n' ' ')
  if [ "$done_wait" = 1 ]; then
    echo "$cluster: pod $lost_inst and its claim(s) are gone; CNPG will join a NEW instance. Released PV(s) to clean up with the tridentctl recipe (docs/runbooks/infra-pg.md): $pvs"
  else
    echo "$cluster: pod/claims of $lost_inst not confirmed gone after ${DELETE_WAIT_SECONDS}s - marker left in place, operator needed. PV(s): $pvs"; rc=1
  fi
done
exit $rc
