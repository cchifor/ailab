#!/bin/sh
# cnpg-lost-slot-reclone probe: the init container of the cnpg-lost-slot-reclone CronJob
# (cnpg-lost-slot-reclone.yaml holds the why). Runs in the cluster's own postgresql image, as the
# LOGIN-only `slotwatch` role, against the -rw Service (the CURRENT primary), and writes SLOTS_FILE:
#   line 1:  in_recovery=<true|false>
#   lines 2+: <slot_name>|<slot_type>|<active>|<wal_status>|<safe_wal_size>|<invalidation_reason>
#
# psql exit 2 (EXIT_BADCONN: the connection could not be made, or was lost) is tried up to PROBE_ATTEMPTS
# times, PROBE_RETRY_SECONDS apart. If it persists, the probe writes PROBE_UNREACHABLE_FILE, removes any
# partial SLOTS_FILE, and exits 0; the main container then decides from the Cluster's status, and only a
# bounded CNPG transition is excused (see the main script's header). Why: infra-pg runs
# primaryUpdateMethod: restart, so a rolling update restarts the primary in place ("Restarting primary
# instance without a switchover first") and -rw has no endpoint until it is back. On 2026-10-09 that
# happened six times during the operator upgrades, for 1.5 to 3 minutes each; 3 probes landed in those
# windows and each failed Job held KubeJobFailed for two days. The retries cover the seconds at the edge
# of a restart: the 08:40:00 probe failed one second after CNPG set the primary label again.
# Exit 2 also covers authentication, pg_hba and TLS refusals, not only network errors; psql's own stderr
# line above each "could not connect" says which.
# Any other exit (1 = an SQL error in a -c command, or psql's own error) fails the run as before.
# POSIX sh so scripts/tests/cnpg-lost-slot-reclone-mock.py can drive it with a fake psql.
set -eu
: "${SLOTS_FILE:=/work/slots.tsv}" "${PROBE_UNREACHABLE_FILE:=/work/probe-unreachable}"
: "${PROBE_ATTEMPTS:=3}" "${PROBE_RETRY_SECONDS:=10}"

rm -f "$SLOTS_FILE" "$PROBE_UNREACHABLE_FILE"
n=0
while :; do
  n=$((n + 1))
  rc=0
  psql -v ON_ERROR_STOP=1 -At -o "$SLOTS_FILE" \
    -c "select 'in_recovery=' || pg_is_in_recovery()" \
    -c "select slot_name || '|' || slot_type || '|' || active || '|' || coalesce(wal_status, '') || '|' || coalesce(safe_wal_size::text, '') || '|' || coalesce(invalidation_reason, '') from pg_replication_slots order by slot_name" \
    || rc=$?
  [ "$rc" -eq 0 ] && break
  if [ "$rc" -ne 2 ]; then
    echo "probe: psql failed (exit $rc)"
    exit "$rc"
  fi
  if [ "$n" -ge "$PROBE_ATTEMPTS" ]; then
    rm -f "$SLOTS_FILE"
    : > "$PROBE_UNREACHABLE_FILE"
    echo "probe: could not connect to the primary in $n attempt(s) - handing the decision to the main container"
    exit 0
  fi
  echo "probe: could not connect to the primary (attempt $n/$PROBE_ATTEMPTS) - retrying in ${PROBE_RETRY_SECONDS}s"
  sleep "$PROBE_RETRY_SECONDS"
done
echo "probe: $(wc -l < "$SLOTS_FILE") line(s)"
cat "$SLOTS_FILE"
