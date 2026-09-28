#!/bin/sh
# cnpg-lost-slot-reclone: re-clone a CNPG replica whose physical replication slot is LOST.
#
# Runs as the main container of the cnpg-lost-slot-reclone CronJob (kubernetes/apps/databases/
# cnpg-lost-slot-reclone.yaml, which holds the why). An init container has already written SLOTS_FILE
# with the primary's slot state (see probe in that manifest):
#   line 1:  in_recovery=<true|false>
#   lines 2+: <slot_name>|<slot_type>|<active true/false>|<wal_status>|<safe_wal_size>|<invalidation_reason>
# (booleans concatenated into text come out as true/false; bare psql -At output would be t/f - both accepted)
# This script only talks to the Kubernetes API (kubectl, namespaced RBAC). POSIX sh so that
# scripts/tests/cnpg-lost-slot-reclone-mock.py can drive it with a fake kubectl on PATH.
#
# Decision (plans/2026-09-28-gitea-actions-retention-and-cnpg-reclone-plan.md):
#   for each cluster in CLUSTERS:
#     phase must be "Cluster in healthy state", a currentPrimary must exist, in_recovery must be f
#     marker ailab.io/reclone-in-progress present  -> observe only: clear it once the replacement is
#       verified (instances complete, old instance gone, no lost slot, every physical slot active);
#       older than STUCK_AFTER_SECONDS and still unverified -> exit 1 (KubeJobFailed pages)
#     expected slot names are derived from .status.instanceNames + the cluster's slotPrefix; a slot is
#       actionable only if it maps to exactly one instance, is physical, inactive and lost
#     the instance must not be currentPrimary, its pod must carry cnpg.io/instanceRole=replica, and
#       ailab.io/last-reclone must be older than MIN_INTERVAL_SECONDS
#     re-read primary/phase/role immediately before mutation; then marker -> delete PVCs -> delete pod
#       -> bounded wait for pod + PVC UIDs to disappear. One re-clone per execution.
# Exit: 0 acted or nothing to do; 1 failure/stuck; 3 probe saw a non-primary (switchover in flight).
set -eu

: "${NAMESPACE:?}" "${CLUSTERS:?}"
: "${SLOTS_FILE:=/work/slots.tsv}" "${DRY_RUN:=true}" "${MIN_INTERVAL_SECONDS:=21600}"
: "${STUCK_AFTER_SECONDS:=2700}" "${DELETE_WAIT_SECONDS:=150}" "${POLL_SECONDS:=5}"
IN_PROGRESS=ailab.io/reclone-in-progress
LAST=ailab.io/last-reclone

k() { kubectl -n "$NAMESPACE" "$@"; }
now() { date -u +%s; }
ts() { date -u +%FT%TZ; }
epoch_of() { # 2026-09-28T10:00:00Z -> epoch (GNU date or busybox)
  date -u -d "$1" +%s 2>/dev/null || date -u -D %Y-%m-%dT%H:%M:%SZ -d "$1" +%s 2>/dev/null || echo 0
}
cluster_field() { k get cluster "$1" -o jsonpath="$2" 2>/dev/null || true; }
pod_role() { k get pod "$1" -o "jsonpath={.metadata.labels.cnpg\.io/instanceRole}" 2>/dev/null || true; }

[ -s "$SLOTS_FILE" ] || { echo "probe output $SLOTS_FILE missing or empty"; exit 1; }
in_recovery=$(sed -n '1s/^in_recovery=//p' "$SLOTS_FILE")
case "$in_recovery" in
  f|false) ;;
  t|true) echo "probe reached a server in recovery (rw service mid-switchover) - not acting"; exit 3 ;;
  *) echo "probe output malformed: first line '$(sed -n 1p "$SLOTS_FILE")'"; exit 1 ;;
esac

acted=0; rc=0
for cluster in $CLUSTERS; do
  phase=$(cluster_field "$cluster" '{.status.phase}')
  primary=$(cluster_field "$cluster" '{.status.currentPrimary}')
  instances=$(cluster_field "$cluster" '{.status.instanceNames[*]}')
  want=$(cluster_field "$cluster" '{.spec.instances}')
  prefix=$(cluster_field "$cluster" '{.spec.replicationSlots.highAvailability.slotPrefix}')
  [ -n "$prefix" ] || prefix="_cnpg_"
  marker=$(cluster_field "$cluster" "{.metadata.annotations.ailab\.io/reclone-in-progress}")
  last=$(cluster_field "$cluster" "{.metadata.annotations.ailab\.io/last-reclone}")
  if [ -z "$primary" ] || [ -z "$instances" ]; then echo "$cluster: not found or no status - skipping"; rc=1; continue; fi

  # expected slot per instance: prefix + instance name with '-' -> '_'
  lost_inst=""; lost_slot=""; any_lost=0; all_physical_active=1
  tail -n +2 "$SLOTS_FILE" > "$SLOTS_FILE.body"
  # shellcheck disable=SC2034  # safe/reason are logged by the probe, not used in the decision
  while IFS='|' read -r slot stype active wal safe reason; do
    [ -n "$slot" ] || continue
    case "$slot" in "$prefix"*) ;; *) continue ;; esac
    match=""; n=0
    for inst in $instances; do
      exp="$prefix$(printf '%s' "$inst" | tr '-' '_')"
      if [ "$exp" = "$slot" ]; then match="$inst"; n=$((n + 1)); fi
    done
    case "$active" in t|true) active=t ;; *) active=f ;; esac
    if [ "$stype" = physical ] && [ "$active" != t ]; then all_physical_active=0; fi
    if [ "$wal" = lost ]; then
      any_lost=1
      if [ "$n" -ne 1 ]; then echo "$cluster: slot $slot is lost but maps to $n instances - ignoring"; continue; fi
      if [ "$stype" != physical ]; then echo "$cluster: slot $slot is lost but not physical ($stype) - ignoring"; continue; fi
      if [ "$active" = t ]; then echo "$cluster: slot $slot is lost but ACTIVE - ignoring"; continue; fi
      if [ -z "$lost_inst" ]; then lost_inst="$match"; lost_slot="$slot"; else echo "$cluster: slot $slot also lost (instance $match) - one re-clone per execution"; fi
    fi
  done < "$SLOTS_FILE.body"

  # --- marker present: observe, verify, clear; never act --------------------------------------------
  if [ -n "$marker" ]; then
    mts=${marker%%/*}; minst=${marker#*/}; age=$(( $(now) - $(epoch_of "$mts") ))
    # shellcheck disable=SC2086  # one name per line on purpose
    ninst=$(printf '%s\n' $instances | grep -c . || true)
    gone=1; for inst in $instances; do [ "$inst" = "$minst" ] && gone=0; done
    if [ "$phase" = "Cluster in healthy state" ] && [ "$ninst" = "$want" ] && [ "$gone" = 1 ] && [ "$any_lost" = 0 ] && [ "$all_physical_active" = 1 ]; then
      echo "$cluster: re-clone of $minst verified (instances=$ninst/$want, no lost slot, all physical slots active) - clearing marker"
      if [ "$DRY_RUN" != "true" ]; then
        k annotate cluster "$cluster" "$LAST=$(ts)/$minst" "$IN_PROGRESS-" --overwrite >/dev/null
      fi
    elif [ "$age" -gt "$STUCK_AFTER_SECONDS" ]; then
      echo "$cluster: re-clone of $minst started ${age}s ago and is NOT verified (phase='$phase' instances=$ninst/$want gone=$gone lost=$any_lost active=$all_physical_active) - operator needed"; rc=1
    else
      echo "$cluster: re-clone of $minst in progress (${age}s, phase='$phase' instances=$ninst/$want) - observing"
    fi
    continue
  fi

  if [ "$phase" != "Cluster in healthy state" ]; then echo "$cluster: phase='$phase' - not acting"; continue; fi
  if [ -z "$lost_inst" ]; then echo "$cluster: no lost slot (primary $primary, instances: $instances)"; continue; fi
  if [ "$acted" = 1 ]; then echo "$cluster: $lost_inst has a lost slot but this execution already acted - next run"; continue; fi

  # --- guards ----------------------------------------------------------------------------------------
  if [ "$lost_inst" = "$primary" ]; then echo "$cluster: $lost_inst is the PRIMARY - refusing"; rc=1; continue; fi
  role=$(pod_role "$lost_inst")
  if [ "$role" != replica ]; then echo "$cluster: $lost_inst role='$role' (pod missing or not a replica) - refusing"; rc=1; continue; fi
  if [ -n "$last" ]; then
    lage=$(( $(now) - $(epoch_of "${last%%/*}") ))
    if [ "$lage" -lt "$MIN_INTERVAL_SECONDS" ]; then echo "$cluster: last re-clone ($last) was ${lage}s ago < $MIN_INTERVAL_SECONDS - refusing (budget)"; rc=1; continue; fi
  fi
  pvcs=$(k get pvc "$lost_inst" "$lost_inst-wal" --ignore-not-found -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}={.spec.volumeName}{"\n"}{end}' 2>/dev/null || true)
  [ -n "$pvcs" ] || { echo "$cluster: no PVC named $lost_inst - refusing"; rc=1; continue; }
  echo "$cluster: slot $lost_slot LOST -> replica $lost_inst must be re-cloned; claims: $(printf '%s' "$pvcs" | tr '\n' ' ')"
  if [ "$DRY_RUN" = "true" ]; then echo "$cluster: DRY_RUN - would annotate $IN_PROGRESS, delete pvc(s) and pod $lost_inst"; acted=1; continue; fi

  # --- final re-read, then mutate -----------------------------------------------------------------------
  p2=$(cluster_field "$cluster" '{.status.currentPrimary}'); ph2=$(cluster_field "$cluster" '{.status.phase}'); r2=$(pod_role "$lost_inst")
  if [ "$p2" != "$primary" ] || [ "$ph2" != "Cluster in healthy state" ] || [ "$r2" != replica ]; then
    echo "$cluster: state changed before mutation (primary $primary->$p2, phase '$ph2', role '$r2') - aborting"; rc=1; continue
  fi
  k annotate cluster "$cluster" "$IN_PROGRESS=$(ts)/$lost_inst" --overwrite >/dev/null
  names=$(printf '%s\n' "$pvcs" | cut -d= -f1 | tr '\n' ' ')
  # PVCs first (they stay Terminating under pvc-protection until the pod is gone), then the pod
  # shellcheck disable=SC2086  # $names is a space-separated list on purpose
  k delete pvc $names --ignore-not-found --wait=false
  k delete pod "$lost_inst" --wait=false --ignore-not-found
  acted=1
  deadline=$(( $(now) + DELETE_WAIT_SECONDS )); done_wait=0
  while [ "$(now)" -lt "$deadline" ]; do
    remaining=0
    if k get pod "$lost_inst" -o name >/dev/null 2>&1; then remaining=1; fi
    for line in $pvcs; do
      name=${line%%=*}; rest=${line#*=}; uid=${rest%%=*}
      cur=$(k get pvc "$name" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)
      [ "$cur" = "$uid" ] && remaining=1
    done
    if [ "$remaining" = 0 ]; then done_wait=1; break; fi
    sleep "$POLL_SECONDS"
  done
  pvs=$(printf '%s\n' "$pvcs" | cut -d= -f3 | tr '\n' ' ')
  if [ "$done_wait" = 1 ]; then
    echo "$cluster: pod $lost_inst and its claim(s) are gone; CNPG will join a NEW instance. Released PV(s) to clean up with the tridentctl recipe (docs/runbooks/infra-pg.md): $pvs"
  else
    echo "$cluster: pod/claims of $lost_inst still present after ${DELETE_WAIT_SECONDS}s - marker left in place, operator needed. PV(s): $pvs"; rc=1
  fi
done
exit $rc
