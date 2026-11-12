#!/bin/sh
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
[ "${FROM_SCHEMA_VERSION:-}" = 9 ] || exit 78
pin=${SCHEMA_010_SHA256:-}
[ "${#pin}" = 64 ] || exit 78
case "$pin" in *[!0-9a-f]*) exit 78;; esac
# Bind the expected function bodies below to the exact reviewed SQL artifact.
# A different merged SQL file requires review of this script, not an operator edit.
[ "$pin" = 86fd1612f0d9aac62902825d7e4fb290ac6c2c030f89d3e4a764a23230a125e1 ] || exit 78
printf '%s  /schema/010.sql\n' "$pin" | sha256sum -c - >/dev/null
version="$PGDATA/relay-provenance-journal-schema-version"
pending="$PGDATA/relay-provenance-journal-upgrade-pending"
if [ ! -d "$PGDATA" ] || [ -L "$PGDATA" ] ||
   [ "$(readlink -f "$PGDATA")" != "$PGDATA" ] ||
   [ ! -f "$PGDATA/PG_VERSION" ] || [ -L "$PGDATA/PG_VERSION" ] ||
   [ "$(cat "$PGDATA/PG_VERSION")" != 17 ] ||
   [ ! -f "$version" ] || [ -L "$version" ] ||
   [ "$(cat "$version")" != 9 ] ||
   [ -e "$PGDATA/postmaster.pid" ] || [ -e "$pending" ] || [ -L "$pending" ] ||
   [ -e "$version.next" ] || [ -L "$version.next" ] ||
   [ -e "$PGDATA/standby.signal" ] || [ -e "$PGDATA/recovery.signal" ]; then
  echo 'Provenance upgrade requires a stopped reviewed schema 9 database' >&2
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
# Require the exact reviewed pre-010 or already-repaired presence functions.
# SQL 010 replaces only these bodies and preserves all retained evidence.
source_ok=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*)=2 AND bool_and(
 p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
 AND p.prorettype=to_regtype(v.return_type)
 AND array_length(p.proconfig,1)=1
 AND regexp_replace(p.proconfig[1],'\s','','g')='search_path=pg_catalog,worker_provenance,pg_temp'
 AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') IN (v.old_sha256,v.new_sha256)
) AND EXISTS (
 SELECT 1 FROM pg_trigger WHERE tgrelid='worker_provenance.admissions'::regclass
 AND tgname='admission_runtime_presence_latch' AND NOT tgisinternal
 AND tgenabled='O' AND tgtype=7 AND tgqual IS NULL
 AND tgfoid='worker_provenance.latch_runtime_presence_requirement()'::regprocedure
) FROM (VALUES
 ('latch_runtime_presence_requirement()','trigger',
  'd9673e802acdf7f46050e52bb81c9c2193dd0ab6d365f84d153fb2a1fb0f92d8',
  'a6cac8ef924eed6aa8511e5821d63b0049145f7b973cc48a46327a757d50e3b1'),
 ('activate_runtime_presence_policy(text,text)','void',
  '8b931fee11e6e11db36c3522575a70c621c776d4e675197ab082fc11cd4daf84',
  '7ab5b5406d44fc254c2e41e446d5eff41655833f3a1b03b06b10a706967cb146')
) AS v(signature,return_type,old_sha256,new_sha256)
JOIN pg_proc p ON p.oid=to_regprocedure('worker_provenance.'||v.signature);
SQL
)
[ "$source_ok" = t ] || {
  echo 'Schema 10 requires reviewed runtime-presence functions' >&2
  exit 78
}
function_shape() {
  psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT jsonb_agg(jsonb_build_array(p.oid::regprocedure::text,p.prorettype::regtype::text,p.proconfig,p.proacl)
 ORDER BY p.oid::regprocedure::text) FROM pg_proc p
WHERE p.oid IN ('worker_provenance.latch_runtime_presence_requirement()'::regprocedure,
 'worker_provenance.activate_runtime_presence_policy(text,text)'::regprocedure);
SQL
}
before_shape=$(function_shape)
# SQL 010 is idempotent, but uncertain maintenance is never automatically replayed.
(set -C; printf 'from=9 to=10\n' > "$pending")
sync
psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f /schema/010.sql &
migration_pid=$!
wait "$migration_pid"
repaired=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*)=2 AND bool_and(
 p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
 AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=v.prosrc_sha256
) AND EXISTS (
 SELECT 1 FROM pg_trigger WHERE tgrelid='worker_provenance.admissions'::regclass
 AND tgname='admission_runtime_presence_latch' AND NOT tgisinternal
 AND tgenabled='O' AND tgtype=7 AND tgqual IS NULL
 AND tgfoid='worker_provenance.latch_runtime_presence_requirement()'::regprocedure
) FROM (VALUES
 ('latch_runtime_presence_requirement()','a6cac8ef924eed6aa8511e5821d63b0049145f7b973cc48a46327a757d50e3b1'),
 ('activate_runtime_presence_policy(text,text)','7ab5b5406d44fc254c2e41e446d5eff41655833f3a1b03b06b10a706967cb146')
) AS v(signature,prosrc_sha256)
JOIN pg_proc p ON p.oid=to_regprocedure('worker_provenance.'||v.signature);
SQL
)
[ "$repaired" = t ] || {
  echo 'Schema 10 function verification failed; preserve maintenance latch' >&2
  exit 78
}
[ "$(function_shape)" = "$before_shape" ] || {
  echo 'Schema 10 changed function configuration or grants; preserve maintenance latch' >&2
  exit 78
}
stop
trap - EXIT INT TERM
(set -C; printf '10\n' > "$version.next")
sync
mv "$version.next" "$version"
sync
rm "$pending"
sync
