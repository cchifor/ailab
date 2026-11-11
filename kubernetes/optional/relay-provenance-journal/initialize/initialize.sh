#!/bin/sh
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
# Verify every reviewed input before creating the data directory.
for n in 001 002 003 004 005; do
  eval "pin=\${SCHEMA_${n}_SHA256:-}"
  [ "${#pin}" = 64 ] || exit 78
  case "$pin" in *[!0-9a-f]*) exit 78;; esac
done
printf '%s  /schema/%s.sql\n' \
  "$SCHEMA_001_SHA256" 001 \
  "$SCHEMA_002_SHA256" 002 \
  "$SCHEMA_003_SHA256" 003 \
  "$SCHEMA_004_SHA256" 004 \
  "$SCHEMA_005_SHA256" 005 | sha256sum -c - >/dev/null
[ ! -e "$PGDATA" ] || { echo 'Provenance data already exists; refusing initialization' >&2; exit 78; }
mkdir "$root/.initialization-started"
initdb -D "$PGDATA" --username=postgres --auth-local=peer --auth-host=scram-sha-256 --data-checksums >/dev/null
stop() { pg_ctl -D "$PGDATA" -m fast -w stop; }
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c fsync=on -c full_page_writes=on -c synchronous_commit=on -c password_encryption=scram-sha-256"
trap stop EXIT
trap 'exit 1' INT TERM
psql -X -h /var/run/postgresql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE relay_provenance;
REVOKE ALL ON DATABASE relay_provenance FROM PUBLIC;
SQL
for n in 001 002 003 004 005; do
  psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f "/schema/$n.sql"
done
# SQL creates NOLOGIN purpose groups. No login or authority is installed.
psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM worker_provenance.authority) OR
     EXISTS (SELECT 1 FROM worker_provenance.epochs) OR
     EXISTS (SELECT 1 FROM worker_provenance.policies) OR
     EXISTS (SELECT 1 FROM worker_provenance.clients) THEN
    RAISE EXCEPTION 'Provenance authority or history unexpectedly populated';
  END IF;
END $$;
SQL
stop
trap - EXIT INT TERM
printf '5\n' > "$PGDATA/relay-provenance-journal-schema-version"
sync
