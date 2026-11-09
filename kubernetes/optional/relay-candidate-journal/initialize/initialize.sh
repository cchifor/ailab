#!/bin/sh
set -eu
umask 077
data_root=/var/lib/postgresql/data
[ "$PGDATA" = "$data_root/pgdata" ] || exit 78
# Independently reviewed hashes are Job arguments, not read from the schema mount.
# Reject missing/malformed pins and mismatching SQL BEFORE any PGDATA operation.
for pin in "$SCHEMA_001_SHA256" "$SCHEMA_002_SHA256"; do
  [ "${#pin}" = 64 ] || exit 78
  case "$pin" in *[!0-9a-f]*) exit 78;; esac
done
printf '%s  %s\n%s  %s\n' "$SCHEMA_001_SHA256" /schema/001-journal.sql \
  "$SCHEMA_002_SHA256" /schema/002-signed-clients.sql | sha256sum -c - >/dev/null
[ ! -e "$PGDATA" ] || { echo 'Candidate data already exists; refusing initialization' >&2; exit 78; }
mkdir "$data_root/.initialization-started" || exit 78
initdb -D "$PGDATA" --username=postgres --auth-local=peer --auth-host=scram-sha-256 --data-checksums >/dev/null
stop() { pg_ctl -D "$PGDATA" -m fast -w stop; }
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c fsync=on -c full_page_writes=on -c synchronous_commit=on -c password_encryption=scram-sha-256"
trap stop EXIT
trap 'exit 1' INT TERM
psql -X -h /var/run/postgresql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE relay_candidate;
REVOKE ALL ON DATABASE relay_candidate FROM PUBLIC;
SQL
# Reviewed 001 and 002 each contain BEGIN/COMMIT. An interrupted second phase
# deliberately leaves no schema marker and requires operator inspection.
psql -X -h /var/run/postgresql -U postgres -d relay_candidate -v ON_ERROR_STOP=1 -f /schema/001-journal.sql
psql -X -h /var/run/postgresql -U postgres -d relay_candidate -v ON_ERROR_STOP=1 -f /schema/002-signed-clients.sql
psql -X -h /var/run/postgresql -U postgres -d relay_candidate -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE ROLE relay_candidate_operator_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE relay_candidate_agent_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE relay_candidate_holder_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT relay_candidate_operator TO relay_candidate_operator_login;
GRANT relay_candidate_agent TO relay_candidate_agent_service;
GRANT relay_candidate_holder TO relay_candidate_holder_service;
GRANT CONNECT ON DATABASE relay_candidate TO relay_candidate_operator_login,relay_candidate_agent_service,relay_candidate_holder_service;
-- Passwords remain NULL. No authority, run, launch, client or witness is seeded.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM worker_candidate.authority) OR
     EXISTS (SELECT 1 FROM worker_candidate.runs) OR
     EXISTS (SELECT 1 FROM worker_candidate.launches) OR
     EXISTS (SELECT 1 FROM worker_candidate.clients) THEN
    RAISE EXCEPTION 'Journal unexpectedly contains authority or history';
  END IF;
END $$;
COMMIT;
SQL
# Durable clean shutdown precedes the success marker. Never mark a live/failed DB.
stop
trap - EXIT INT TERM
printf '2\n' > "$PGDATA/relay-candidate-journal-schema-version"
sync
