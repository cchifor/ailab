#!/bin/sh
# One offline, operator-owned upgrade of a retained version-1 ledger to version 5 (002-005).
set -eu
umask 077
[ "$PGDATA" = /var/lib/postgresql/data/pgdata ] || exit 78
. "$(dirname "$0")/verify-schema.sh"
verify_schema || { echo 'Reviewed schema bundle does not match its independent pin' >&2; exit 78; }
# The original initializer did not retain a source receipt. Require the operator's
# recorded original 001 digest; a changed baseline needs a separate reviewed path.
legacy=$(sha256sum /schema/001-authority.sql)
[ "${legacy%% *}" = "${RELAY_BUDGET_LEGACY_SCHEMA_SHA256:-}" ] || exit 78
[ -f "$PGDATA/PG_VERSION" ] && [ "$(cat "$PGDATA/PG_VERSION")" = 17 ] || exit 78
[ -f "$PGDATA/relay-budget-schema-version" ] &&
  [ "$(cat "$PGDATA/relay-budget-schema-version")" = 1 ] || exit 78
[ ! -e "$PGDATA/recovery.signal" ] && [ ! -e "$PGDATA/standby.signal" ] || exit 78
# Reject a live postmaster, including a stale pid file that needs owner inspection.
[ ! -e "$PGDATA/postmaster.pid" ] || exit 78
# Interrupted migrations require inspection. A SQL preflight refusal does
# not consume this marker; create it durably immediately before the first SQL.
[ ! -e "$PGDATA/relay-budget-upgrade-started" ] || exit 78
stop() { pg_ctl -D "$PGDATA" -m fast -w -t 3600 stop; }
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c fsync=on -c full_page_writes=on -c synchronous_commit=on"
trap stop EXIT
trap 'exit 1' INT TERM
psql -X -h /var/run/postgresql -U postgres -d relay_budget -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM worker_budget.authority WHERE ready) THEN
    RAISE EXCEPTION 'Withdraw authority before the offline schema upgrade';
  END IF;
  IF to_regprocedure('worker_budget.preflight(uuid,uuid,text,jsonb)') IS NOT NULL
     OR to_regclass('worker_budget.scope_runs') IS NOT NULL
     OR to_regprocedure('worker_budget.prepare_provenance(uuid,uuid,text,jsonb)') IS NOT NULL THEN
    RAISE EXCEPTION 'Retained schema is not the expected version 1; inspect migration history';
  END IF;
END $$;
SQL
mkdir "$PGDATA/relay-budget-upgrade-started" || exit 78
sync
for schema in 002-preflight.sql 003-scoped-requests.sql 004-preflight-errors.sql \
  005-provenance-purpose.sql; do
  psql -X -h /var/run/postgresql -U postgres -d relay_budget -v ON_ERROR_STOP=1 -f "/schema/$schema"
done
stop
trap - EXIT
record_schema
