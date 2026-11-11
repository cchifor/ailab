#!/bin/sh
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
case "${FROM_SCHEMA_VERSION:-}" in
  5) migrations='006 007 008';;
  6) migrations='007 008';;
  7) migrations='008';;
  *) exit 78;;
esac
# Validate every pin before checking or changing retained data.
for n in $migrations; do
  eval "pin=\${SCHEMA_${n}_SHA256:-}"
  [ "${#pin}" = 64 ] || exit 78
  case "$pin" in *[!0-9a-f]*) exit 78;; esac
done
for n in $migrations; do
  eval "pin=\${SCHEMA_${n}_SHA256}"
  printf '%s  /schema/%s.sql\n' "$pin" "$n" | sha256sum -c - >/dev/null
done
version="$PGDATA/relay-provenance-journal-schema-version"
pending="$PGDATA/relay-provenance-journal-upgrade-pending"
if [ ! -d "$PGDATA" ] || [ -L "$PGDATA" ] ||
   [ "$(readlink -f "$PGDATA")" != "$PGDATA" ] ||
   [ ! -f "$PGDATA/PG_VERSION" ] || [ -L "$PGDATA/PG_VERSION" ] || [ "$(cat "$PGDATA/PG_VERSION")" != 17 ] ||
   [ ! -f "$version" ] || [ -L "$version" ] ||
   [ "$(cat "$version")" != "$FROM_SCHEMA_VERSION" ] ||
   [ -e "$PGDATA/postmaster.pid" ] || [ -e "$pending" ] || [ -L "$pending" ] ||
   [ -e "$version.next" ] || [ -L "$version.next" ] ||
   [ -e "$PGDATA/standby.signal" ] || [ -e "$PGDATA/recovery.signal" ]; then
  echo 'Provenance upgrade requires a stopped reviewed schema 5/6/7 database' >&2
  exit 78
fi
[ "$(stat -c '%u' "$PGDATA")" = 70 ] || exit 78
case "$(stat -c '%a' "$PGDATA")" in 700|750|2700|2750) ;; *) exit 78;; esac
stop() { pg_ctl -D "$PGDATA" -m fast -w stop; }
trap stop EXIT
trap 'exit 1' INT TERM
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c ssl=off -c fsync=on -c full_page_writes=on -c synchronous_commit=on"
# FORCE RLS must not hide incompatible rows from the maintenance preflight.
superuser=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -c 'SELECT rolsuper FROM pg_roles WHERE rolname = current_user')
[ "$superuser" = t ] || exit 78
# Refuse known incompatible history before schema 006 can commit. The offline
# consumer fence and Unix-only listener prevent concurrent application writers.
if [ "$FROM_SCHEMA_VERSION" = 7 ]; then
  # 008 does not reinterpret presence evidence; preserve populated history.
  check="SELECT to_regprocedure('worker_provenance.runtime_presence_admission_monotone()') IS NOT NULL
    AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='worker_provenance.admissions'::regclass
      AND conname='runtime_presence_cleanup_requires_closed')"
else
check='SELECT NOT EXISTS (SELECT 1 FROM worker_provenance.runtime_presence) AND NOT EXISTS (SELECT 1 FROM worker_provenance.admissions WHERE requires_runtime_presence AND cleanup_started AND NOT closed)'
check="$check AND NOT EXISTS (
  SELECT 1 FROM pg_constraint WHERE conrelid='worker_provenance.admissions'::regclass
  AND conname='runtime_presence_cleanup_requires_closed'
) AND NOT EXISTS (
  SELECT 1 FROM pg_trigger WHERE tgrelid='worker_provenance.admissions'::regclass
  AND tgname='runtime_presence_admission_monotone'
) AND NOT EXISTS (
  SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname='worker_provenance' AND p.proname='runtime_presence_admission_monotone'
)"
if [ "$FROM_SCHEMA_VERSION" = 6 ]; then
  check="$check AND NOT EXISTS (SELECT 1 FROM worker_provenance.runtime_presence_ledger) AND NOT EXISTS (SELECT 1 FROM worker_provenance.runtime_presence_ledger_head)"
else
  # A stale marker after out-of-band DDL is not an upgradeable schema 5.
  check="$check AND NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname IN (
      'relay_provenance_cleanup_verifier','relay_provenance_ledger','relay_provenance_recovery_operator'
    )
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='worker_provenance' AND c.relname ~ '^runtime_presence_ledger'
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
    WHERE n.nspname='worker_provenance' AND t.typname IN ('runtime_presence_ledger','runtime_presence_ledger_head')
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='worker_provenance' AND p.proname IN (
      'read_runtime_presence_for_cleanup','read_runtime_presence_ledger_head',
      'read_runtime_presence_ledger_entry','read_runtime_presence_ledger_max_sequence',
      'lock_runtime_presence_admission','append_runtime_presence_ledger','restore_runtime_presence_ledger_head'
    )
  )"
fi
fi
compatible=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -c "$check")
if [ "$compatible" != t ]; then
  echo 'Provenance upgrade requires compatible history, phases and migration prerequisites' >&2
  exit 78
fi
# Source hashes match candidate Relay 008 at ba496c1a; historical bodies are from 2aae6c14.
# The same exact known-source hashes are rechecked under locks by SQL 008.
# Refuse before 006/007 commit or a latch is created if installed code drifted.
source_ok=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*)=2 AND bool_and(
 p.proowner='relay_provenance_owner'::regrole AND p.prosecdef AND p.prorettype='jsonb'::regtype
 AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') IN (v.previous_digest,v.repaired_digest)
) FROM (VALUES
 ('transition','585207182af8ebd3142894a14006da7b046d9803576ee2a08d20af2d5f5a25c7','96f5fbc1440e80e791c628f08812790c3a1cb5255e7bd6efac8e0fba88472eb7'),
 ('lookup','3a3b6055deb3486bd88a484790fe33767d1286ffe0f7f6be9f4c01a505fa657b','1e1314f11045c5f53603dc1da1a0a66f224d215d0e365c811bcecf71a6d2eef9')
) AS v(name,previous_digest,repaired_digest)
JOIN pg_proc p ON p.oid=to_regprocedure('worker_provenance.'||v.name||'(text,text,text)');
SQL
)
if [ "$source_ok" != t ]; then
  echo 'Provenance upgrade requires reviewed authority function history' >&2
  exit 78
fi
# This persistent latch prevents automatic startup or retry after interruption.
# The operator must also fence every other PVC consumer; RWO is not that fence.
(set -C; printf 'from=%s to=8\n' "$FROM_SCHEMA_VERSION" > "$pending")
sync
for n in $migrations; do
  # Waiting on a background child lets POSIX sh handle TERM promptly. The EXIT
  # trap's fast PostgreSQL stop interrupts its transaction and keeps the latch.
  psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f "/schema/$n.sql" &
  migration_pid=$!
  wait "$migration_pid"
done
stop
trap - EXIT INT TERM
# Publish the new marker only after all transactions and clean shutdown.
(set -C; printf '8\n' > "$version.next")
sync
mv "$version.next" "$version"
sync
rm "$pending"
sync
