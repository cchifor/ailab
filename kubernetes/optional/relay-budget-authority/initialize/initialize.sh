#!/bin/sh
set -eu
umask 077
data_root=/var/lib/postgresql/data
[ "$PGDATA" = "$data_root/pgdata" ] || exit 78
. "$(dirname "$0")/verify-schema.sh"
verify_schema || { echo 'Reviewed schema bundle does not match its independent pin' >&2; exit 78; }
# An interrupted first attempt also needs owner investigation; never reseed on retry.
[ ! -e "$PGDATA" ] || { echo 'Budget data already exists; refusing initialization' >&2; exit 78; }
mkdir "$data_root/.initialization-started" || exit 78
initdb -D "$PGDATA" --username=postgres --auth-local=peer --auth-host=scram-sha-256 --data-checksums
stop() { pg_ctl -D "$PGDATA" -m fast -w stop; }
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c fsync=on -c full_page_writes=on -c synchronous_commit=on -c password_encryption=scram-sha-256"
trap stop EXIT
trap 'exit 1' INT TERM
psql -X -h /var/run/postgresql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE relay_budget;
REVOKE ALL ON DATABASE relay_budget FROM PUBLIC;
SQL
for schema in 001-authority.sql 002-preflight.sql 003-scoped-requests.sql 004-preflight-errors.sql; do
  psql -X -h /var/run/postgresql -U postgres -d relay_budget -v ON_ERROR_STOP=1 -f "/schema/$schema"
done
psql -X -h /var/run/postgresql -U postgres -d relay_budget -v ON_ERROR_STOP=1 <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE ROLE relay_budget_allocator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE relay_budget_router LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT relay_budget_client TO relay_budget_allocator;
GRANT relay_budget_reader TO relay_budget_router;
GRANT CONNECT ON DATABASE relay_budget TO relay_budget_allocator,relay_budget_router;
-- Passwords remain NULL. Operator sets them interactively via \password after startup.
-- No authority, clients, readers, qualifications, allocations or witness is seeded here.
SQL
stop
trap - EXIT
record_schema
