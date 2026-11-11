#!/bin/sh
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
case "${FROM_SCHEMA_VERSION:-}" in
  5) migrations='006 007';;
  6) migrations='007';;
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
  echo 'Provenance upgrade requires a stopped reviewed schema 5/6 database' >&2
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
check='SELECT NOT EXISTS (SELECT 1 FROM worker_provenance.runtime_presence) AND NOT EXISTS (SELECT 1 FROM worker_provenance.admissions WHERE requires_runtime_presence AND cleanup_started AND NOT closed)'
if [ "$FROM_SCHEMA_VERSION" = 6 ]; then
  check="$check AND NOT EXISTS (SELECT 1 FROM worker_provenance.runtime_presence_ledger) AND NOT EXISTS (SELECT 1 FROM worker_provenance.runtime_presence_ledger_head)"
fi
compatible=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -c "$check")
if [ "$compatible" != t ]; then
  echo 'Provenance upgrade requires empty presence history and compatible admission phases' >&2
  exit 78
fi
# This persistent latch prevents automatic startup or retry after interruption.
# The operator must also fence every other PVC consumer; RWO is not that fence.
(set -C; printf 'from=%s to=7\n' "$FROM_SCHEMA_VERSION" > "$pending")
sync
for n in $migrations; do
  psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f "/schema/$n.sql"
done
stop
trap - EXIT INT TERM
# Publish the new marker only after both transactions and clean shutdown.
(set -C; printf '7\n' > "$version.next")
sync
mv "$version.next" "$version"
sync
rm "$pending"
sync
