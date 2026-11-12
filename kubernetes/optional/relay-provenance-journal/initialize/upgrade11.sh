#!/bin/sh
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
[ "${FROM_SCHEMA_VERSION:-}" = 10 ] || exit 78
pin=${SCHEMA_011_SHA256:-}
[ "${#pin}" = 64 ] || exit 78
case "$pin" in *[!0-9a-f]*) exit 78;; esac
# Bind the new function body below to the exact reviewed SQL artifact.
# A different merged SQL file requires review of this script, not an operator edit.
[ "$pin" = 094e6639b07d497401f86ab6af5ccd14813238d2beb1d538bcd1ac7f105caebd ] || exit 78
printf '%s  /schema/011.sql\n' "$pin" | sha256sum -c - >/dev/null
version="$PGDATA/relay-provenance-journal-schema-version"
pending="$PGDATA/relay-provenance-journal-upgrade-pending"
if [ ! -d "$PGDATA" ] || [ -L "$PGDATA" ] ||
   [ "$(readlink -f "$PGDATA")" != "$PGDATA" ] ||
   [ ! -f "$PGDATA/PG_VERSION" ] || [ -L "$PGDATA/PG_VERSION" ] ||
   [ "$(cat "$PGDATA/PG_VERSION")" != 17 ] ||
   [ ! -f "$version" ] || [ -L "$version" ] ||
   [ "$(cat "$version")" != 10 ] ||
   [ -e "$PGDATA/postmaster.pid" ] || [ -e "$pending" ] || [ -L "$pending" ] ||
   [ -e "$version.next" ] || [ -L "$version.next" ] ||
   [ -e "$PGDATA/standby.signal" ] || [ -e "$PGDATA/recovery.signal" ]; then
  echo 'Provenance upgrade requires a stopped reviewed schema 10 database' >&2
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
# Existing authority functions and purpose roles must match the reviewed
# schema10 snapshot. Reject drift before setting a maintenance latch.
source_ok=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*)=4 AND bool_and((
 p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
 AND p.prorettype=to_regtype(v.return_type)
 AND array_length(p.proconfig,1)=1
 AND regexp_replace(p.proconfig[1],'\s','','g')='search_path=pg_catalog,worker_provenance,pg_temp'
 AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=v.body_sha256
) IS TRUE)
AND to_regprocedure('worker_provenance.read_runtime_presence_current_bound(uuid)') IS NULL
AND NOT EXISTS (SELECT 1 FROM pg_event_trigger)
AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE roleid='relay_provenance_owner'::regrole)
AND (SELECT count(*)=2 AND bool_and(NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls)
 FROM pg_roles WHERE rolname IN ('relay_provenance_owner','relay_provenance_ledger'))
FROM (VALUES
 ('lock_runtime_presence_admission(uuid)','void','f0558885be87e4aa6afbf98f96881c0bb38d74aae5c38c3b4faa1995c5cf5306'),
 ('read_runtime_presence(uuid)','jsonb','dd6ee60030ad8383cc36ef7a8561d46c9023248811bacee7b3f8c6f5da84c209'),
 ('latch_runtime_presence_requirement()','trigger','a6cac8ef924eed6aa8511e5821d63b0049145f7b973cc48a46327a757d50e3b1'),
 ('activate_runtime_presence_policy(text,text)','void','7ab5b5406d44fc254c2e41e446d5eff41655833f3a1b03b06b10a706967cb146')
) AS v(signature,return_type,body_sha256)
JOIN pg_proc p ON p.oid=to_regprocedure('worker_provenance.'||v.signature);
SQL
)
[ "$source_ok" = t ] || {
  echo 'Schema 11 requires the reviewed schema 10 functions and roles' >&2
  exit 78
}
# Preserve all existing prerequisite metadata and purpose membership. Ledger
# logins can already be intentionally provisioned; none may gain authority here.
prerequisite_shape() {
 psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT jsonb_build_object(
 'functions',(SELECT jsonb_agg(to_jsonb(p) ORDER BY p.oid) FROM pg_proc p WHERE p.oid IN (
  'worker_provenance.lock_runtime_presence_admission(uuid)'::regprocedure,
  'worker_provenance.read_runtime_presence(uuid)'::regprocedure,
  'worker_provenance.latch_runtime_presence_requirement()'::regprocedure,
  'worker_provenance.activate_runtime_presence_policy(text,text)'::regprocedure)),
 'roles',(SELECT jsonb_agg(to_jsonb(r) ORDER BY r.oid) FROM pg_roles r
  WHERE rolname IN ('relay_provenance_owner','relay_provenance_ledger')),
 'memberships',(SELECT jsonb_agg(to_jsonb(m) ORDER BY m.oid) FROM pg_auth_members m
  WHERE roleid IN ('relay_provenance_owner'::regrole,'relay_provenance_ledger'::regrole)
     OR member IN ('relay_provenance_owner'::regrole,'relay_provenance_ledger'::regrole)),
 'eventTriggers',(SELECT count(*) FROM pg_event_trigger));
SQL
}
before_shape=$(prerequisite_shape)
# CREATE FUNCTION is forward-only; an unknown result is never replayed.
(set -C; printf 'from=10 to=11\n' > "$pending")
sync
psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f /schema/011.sql &
migration_pid=$!
wait "$migration_pid"
installed=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT count(*)=1 AND bool_and((
 p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
 AND p.prorettype='jsonb'::regtype AND NOT p.proretset AND p.provolatile='v'
 AND array_length(p.proconfig,1)=1
 AND regexp_replace(p.proconfig[1],'\s','','g')='search_path=pg_catalog,worker_provenance,pg_temp'
 AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='a5e9d2c63a0dd731c581ab9714fd102f66079f3125f6a0b1aaf93deca6454999'
 AND (SELECT count(*)=2 AND count(DISTINCT grantee)=2
      AND bool_and(privilege_type='EXECUTE' AND grantor='relay_provenance_owner'::regrole
      AND ((grantee='relay_provenance_owner'::regrole)
        OR (grantee='relay_provenance_ledger'::regrole AND NOT is_grantable)))
      FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))))
) IS TRUE)
FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.read_runtime_presence_current_bound(uuid)');
SQL
)
[ "$installed" = t ] || {
  echo 'Schema 11 function verification failed; preserve maintenance latch' >&2
  exit 78
}
[ "$(prerequisite_shape)" = "$before_shape" ] || {
  echo 'Schema 11 changed prerequisite metadata or authority; preserve maintenance latch' >&2
  exit 78
}
stop
trap - EXIT INT TERM
(set -C; printf '11\n' > "$version.next")
sync
mv "$version.next" "$version"
sync
rm "$pending"
sync
