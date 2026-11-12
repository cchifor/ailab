#!/bin/sh
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
[ "${FROM_SCHEMA_VERSION:-}" = 8 ] || exit 78
pin=${SCHEMA_009_SHA256:-}
[ "${#pin}" = 64 ] || exit 78
case "$pin" in *[!0-9a-f]*) exit 78;; esac
printf '%s  /schema/009.sql\n' "$pin" | sha256sum -c - >/dev/null
version="$PGDATA/relay-provenance-journal-schema-version"
pending="$PGDATA/relay-provenance-journal-upgrade-pending"
if [ ! -d "$PGDATA" ] || [ -L "$PGDATA" ] ||
   [ "$(readlink -f "$PGDATA")" != "$PGDATA" ] ||
   [ ! -f "$PGDATA/PG_VERSION" ] || [ -L "$PGDATA/PG_VERSION" ] ||
   [ "$(cat "$PGDATA/PG_VERSION")" != 17 ] ||
   [ ! -f "$version" ] || [ -L "$version" ] ||
   [ "$(cat "$version")" != 8 ] ||
   [ -e "$PGDATA/postmaster.pid" ] || [ -e "$pending" ] || [ -L "$pending" ] ||
   [ -e "$version.next" ] || [ -L "$version.next" ] ||
   [ -e "$PGDATA/standby.signal" ] || [ -e "$PGDATA/recovery.signal" ]; then
  echo 'Provenance upgrade requires a stopped reviewed schema 8 database' >&2
  exit 78
fi
[ "$(stat -c '%u' "$PGDATA")" = 70 ] || exit 78
case "$(stat -c '%a' "$PGDATA")" in 700|750|2700|2750) ;; *) exit 78;; esac
stop() { pg_ctl -D "$PGDATA" -m fast -w stop; }
trap stop EXIT
trap 'exit 1' INT TERM
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c ssl=off -c fsync=on -c full_page_writes=on -c synchronous_commit=on"
# The maintenance connection must see all retained rows despite FORCE RLS.
superuser=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -c 'SELECT rolsuper FROM pg_roles WHERE rolname = current_user')
[ "$superuser" = t ] || exit 78
# These reviewed schema-008 function hashes are also enforced by SQL 009.
# Recheck the exact SQL source at merge and update both hashes if needed.
source_ok=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*)=2 AND bool_and(
 p.proowner='relay_provenance_owner'::regrole AND p.prosecdef AND p.prorettype='jsonb'::regtype
 AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=v.prosrc_sha256
) FROM (VALUES
 ('transition','96f5fbc1440e80e791c628f08812790c3a1cb5255e7bd6efac8e0fba88472eb7'),
 ('lookup','1e1314f11045c5f53603dc1da1a0a66f224d215d0e365c811bcecf71a6d2eef9')
) AS v(name,prosrc_sha256)
JOIN pg_proc p ON p.oid=to_regprocedure('worker_provenance.'||v.name||'(text,text,text)');
SQL
)
[ "$source_ok" = t ] || {
  echo 'Schema 9 requires the reviewed schema 8 authority functions' >&2
  exit 78
}
# Drain gates run before the persistent latch; SQL 009 repeats them under lock.
compatible=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT EXISTS (
  SELECT 1 FROM worker_provenance.schemas
  WHERE name='reserve'
    AND definition #> '{properties,requestDigest}' IS NOT NULL
    AND definition #> '{properties,preparedSpecDigest}' IS NULL
) AND NOT EXISTS (
  SELECT 1 FROM worker_provenance.effects
  WHERE family='pod-create' AND state IN ('reserved','armed')
) AND NOT EXISTS (
  SELECT 1 FROM worker_provenance.admissions
  WHERE execution IS NOT NULL AND NOT closed
) AND to_regprocedure('worker_provenance.require_original_pod_spec_commitment()') IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid='worker_provenance.effects'::regclass
      AND tgname='require_original_pod_spec_commitment'
      AND NOT tgisinternal
  );
SQL
)
if [ "$compatible" != t ]; then
  echo 'Schema 9 requires drained original Pod effects and the pre-009 reserve schema' >&2
  exit 78
fi
# SQL 009 is one-shot. The latch is not automatically cleared or replayed.
(set -C; printf 'from=8 to=9\n' > "$pending")
sync
psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f /schema/009.sql &
migration_pid=$!
wait "$migration_pid"
stop
trap - EXIT INT TERM
(set -C; printf '9\n' > "$version.next")
sync
mv "$version.next" "$version"
sync
rm "$pending"
sync
