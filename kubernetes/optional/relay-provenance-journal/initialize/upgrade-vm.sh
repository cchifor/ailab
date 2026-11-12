#!/bin/sh
# Attended, one-shot schema 11 -> VM15 maintenance. The two 011 SQL files
# have distinct purposes: runtime-presence current readback is already present;
# vm011 is the first KubeVirt extension. No credential or authority is created.
set -eu
umask 077
root=/var/lib/postgresql/data
[ "$PGDATA" = "$root/pgdata" ] || exit 78
[ "${FROM_SCHEMA_VERSION:-}" = 11 ] || exit 78
for name in SCHEMA_008_REPAIR_SHA256 VM_SCHEMA_011_SHA256 VM_SCHEMA_012_SHA256 VM_SCHEMA_013_SHA256 VM_SCHEMA_014_SHA256; do
  eval "pin=\${$name:-}"
  [ "${#pin}" = 64 ] || exit 78
  case "$pin" in *[!0-9a-f]*) exit 78;; esac
done
# Bind the replay and all four VM migrations to the exact reviewed bytes.
[ "$SCHEMA_008_REPAIR_SHA256" = 9e7b48a1f624c83e421bd187354318f589f788229908bc283d4e4e0abf07f95c ] || exit 78
[ "$VM_SCHEMA_011_SHA256" = 02a3d6459458304f69ded3c3601d2b3df53ea84ca5cd59b3c4df99f94d66993d ] || exit 78
[ "$VM_SCHEMA_012_SHA256" = cabffdf93c9759110d31480481d90b4cf9996514d1fb217080e2350ed8155c3a ] || exit 78
[ "$VM_SCHEMA_013_SHA256" = 4d0397e70d144104d0a991b2f4475b9fa8d5d52ed03f1a63de8eb6023d679fcd ] || exit 78
[ "$VM_SCHEMA_014_SHA256" = 17721e0021c88d76bf68157fbe6f28bb1ac44119fd92ecb291e001644d6eddf0 ] || exit 78
printf '%s  /schema/%s\n' \
  "$SCHEMA_008_REPAIR_SHA256" 008-repair.sql \
  "$VM_SCHEMA_011_SHA256" vm011.sql \
  "$VM_SCHEMA_012_SHA256" vm012.sql \
  "$VM_SCHEMA_013_SHA256" vm013.sql \
  "$VM_SCHEMA_014_SHA256" vm014.sql | sha256sum -c - >/dev/null

version="$PGDATA/relay-provenance-journal-schema-version"
pending="$PGDATA/relay-provenance-journal-upgrade-pending"
if [ ! -d "$PGDATA" ] || [ -L "$PGDATA" ] ||
   [ "$(readlink -f "$PGDATA")" != "$PGDATA" ] ||
   [ ! -f "$PGDATA/PG_VERSION" ] || [ -L "$PGDATA/PG_VERSION" ] ||
   [ "$(cat "$PGDATA/PG_VERSION")" != 17 ] ||
   [ ! -f "$version" ] || [ -L "$version" ] || [ "$(cat "$version")" != 11 ] ||
   [ -e "$PGDATA/postmaster.pid" ] || [ -e "$pending" ] || [ -L "$pending" ] ||
   [ -e "$version.next" ] || [ -L "$version.next" ] ||
   [ -e "$PGDATA/standby.signal" ] || [ -e "$PGDATA/recovery.signal" ]; then
  echo 'VM upgrade requires a stopped reviewed schema 11 database' >&2
  exit 78
fi
[ "$(stat -c '%u' "$PGDATA")" = 70 ] || exit 78
case "$(stat -c '%a' "$PGDATA")" in 700|750|2700|2750) ;; *) exit 78;; esac

stop() { pg_ctl -D "$PGDATA" -m fast -w stop; }
trap stop EXIT
trap 'exit 1' INT TERM
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c ssl=off -c fsync=on -c full_page_writes=on -c synchronous_commit=on"
superuser=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -c 'SELECT rolsuper FROM pg_roles WHERE rolname=current_user')
[ "$superuser" = t ] || exit 78
# A marker alone is not a source pin. Accept only reviewed schema 11 bodies,
# including the prior and corrected 008 transition, before writing the latch.
source_ok=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT
 coalesce((SELECT p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
  AND p.prorettype='jsonb'::regtype
  AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='1e1314f11045c5f53603dc1da1a0a66f224d215d0e365c811bcecf71a6d2eef9'
  FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.lookup(text,text,text)')),false)
 AND coalesce((SELECT p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
  AND p.prorettype='jsonb'::regtype
  AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') IN
   ('96f5fbc1440e80e791c628f08812790c3a1cb5255e7bd6efac8e0fba88472eb7',
    '9e0882b523bd15e50ab243421052872c48f7cc6043ad1fc84af32982b27567fb')
  FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.transition(text,text,text)')),false)
 AND coalesce((SELECT p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
  AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='a5e9d2c63a0dd731c581ab9714fd102f66079f3125f6a0b1aaf93deca6454999'
  FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.read_runtime_presence_current_bound(uuid)')),false)
 AND to_regprocedure('worker_provenance.require_original_pod_spec_commitment()') IS NOT NULL
 AND worker_provenance.family_owner('vm-create') IS NULL
 AND NOT EXISTS(SELECT 1 FROM worker_provenance.policies WHERE document->>'version'='3')
 AND NOT EXISTS(SELECT 1 FROM pg_event_trigger)
 AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid='relay_provenance_owner'::regrole);
SQL
)
[ "$source_ok" = t ] || {
  echo 'VM upgrade requires reviewed schema 11 source and authority' >&2
  exit 78
}
history_before=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT (SELECT count(*) FROM worker_provenance.policies)||':'||
       (SELECT count(*) FROM worker_provenance.admissions)||':'||
       (SELECT count(*) FROM worker_provenance.effects)||':'||
       (SELECT count(*) FROM worker_provenance.events);
SQL
)
(set -C; printf 'from=11 to=vm15\n' > "$pending")
sync
for file in 008-repair.sql vm011.sql vm012.sql vm013.sql vm014.sql; do
  psql -X -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 -f "/schema/$file" &
  migration_pid=$!
  wait "$migration_pid"
done
source_ok=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT
 coalesce((SELECT p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
  AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='f9353b103e2099d4289fe283460b73fd0fbf77c4f2e0c41c0f37735db7089da7'
  FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.lookup(text,text,text)')),false)
 AND coalesce((SELECT p.proowner='relay_provenance_owner'::regrole AND p.prosecdef
  AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='89c62b98f29816886957b117b28e815b42ff5a72931301ae75cf2fc506a42fd4'
  FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.transition(text,text,text)')),false)
 AND worker_provenance.family_owner('vm-create')='infrastructure'
 AND worker_provenance.family_owner('vm-init-readback')='backing'
 AND coalesce((SELECT encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')='a5e9d2c63a0dd731c581ab9714fd102f66079f3125f6a0b1aaf93deca6454999'
  FROM pg_proc p WHERE p.oid=to_regprocedure('worker_provenance.read_runtime_presence_current_bound(uuid)')),false);
SQL
)
[ "$source_ok" = t ] || { echo 'VM upgrade output source verification failed; preserve maintenance latch' >&2; exit 78; }
history_after=$(psql -X -At -h /var/run/postgresql -U postgres -d relay_provenance -v ON_ERROR_STOP=1 <<'SQL'
SELECT (SELECT count(*) FROM worker_provenance.policies)||':'||
       (SELECT count(*) FROM worker_provenance.admissions)||':'||
       (SELECT count(*) FROM worker_provenance.effects)||':'||
       (SELECT count(*) FROM worker_provenance.events);
SQL
)
[ "$history_after" = "$history_before" ] || { echo 'VM upgrade changed retained history counts; preserve maintenance latch' >&2; exit 78; }
stop
trap - EXIT INT TERM
(set -C; printf '15\n' > "$version.next")
sync
mv "$version.next" "$version"
sync
rm "$pending"
sync
