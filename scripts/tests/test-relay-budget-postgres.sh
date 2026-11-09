#!/usr/bin/env bash
# Uses the real pinned image and explicitly supplied, reviewed Relay schema.
# Local Docker only; never reaches a Kubernetes context or production database.
set -euo pipefail
[ "$#" -eq 1 ] || { echo "usage: $0 <reviewed-schema-bundle-directory>" >&2; exit 64; }
schema=$(realpath "$1")
[ -s "$schema/SHA256SUMS" ] || exit 64
schema_pin=$(sha256sum "$schema/SHA256SUMS" | cut -d " " -f1)
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_dir="$repo/kubernetes/optional/relay-budget-authority"
image=$(awk '/image:/ {print $2; exit}' "$source_dir/postgres.yaml")
[[ "$image" == *@sha256:* ]] || exit 65
[ "$(awk '/image:/ {print $2; exit}' "$source_dir/initialize/job.yaml")" = "$image" ] || exit 65
[ "$(awk '/image:/ {print $2; exit}' "$source_dir/initialize/upgrade-job.yaml")" = "$image" ] || exit 65
test_dir=$(mktemp -d "$repo/.budget-postgres-test.XXXXXX")
# Public bundle projections must be traversable by the image's UID70, just like
# the read-only ConfigMap mount. Keep fixture TLS private even inside this tree.
chmod 755 "$test_dir"
mkdir "$test_dir/schema"
cp "$schema/"{source-commit,SHA256SUMS,001-authority.sql,002-preflight.sql,003-scoped-requests.sql,004-preflight-errors.sql} "$test_dir/schema/"
chmod 644 "$test_dir/schema/"*
schema="$test_dir/schema"
test_name="relay-budget-test-$(date +%s)-$$"
data_volume="$test_name-data"
tls_volume="$test_name-tls"
cleanup() {
  docker rm -fv "$test_name" >/dev/null 2>&1 || true
  docker volume rm "$data_volume" "$tls_volume" >/dev/null 2>&1 || true
  rm -rf -- "$test_dir"
}
trap cleanup EXIT
trap 'exit 1' INT TERM
docker volume create "$data_volume" >/dev/null
docker volume create "$tls_volume" >/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj /CN=localhost -addext subjectAltName=DNS:localhost \
  -keyout "$test_dir/tls.key" -out "$test_dir/tls.crt" >/dev/null 2>&1
chmod 644 "$test_dir/tls.crt"
# Reproduce a root:70 projected key (0440), not a world-readable test shortcut.
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
  -v "$data_volume:/var/lib/postgresql/data" -v "$tls_volume:/run/tls" \
  -v "$test_dir:/fixture:ro" "$image" -ec '
  chown 70:70 /var/lib/postgresql/data
  cp /fixture/tls.crt /fixture/tls.key /run/tls/
  chown 0:70 /run/tls/tls.crt /run/tls/tls.key
  chmod 0440 /run/tls/tls.crt /run/tls/tls.key'
run_args=(--rm --network none --user 70:70 --read-only --cap-drop ALL
  --security-opt no-new-privileges:true
  --tmpfs /var/run/postgresql:uid=70,gid=70,mode=0700
  --tmpfs /tmp:uid=70,gid=70,mode=0700
  -v "$data_volume:/var/lib/postgresql/data"
  -v "$tls_volume:/run/tls:ro"
  -v "$source_dir:/etc/relay-budget:ro"
  -e PGDATA=/var/lib/postgresql/data/pgdata
  -e RELAY_BUDGET_SCHEMA_MANIFEST_SHA256="$schema_pin")

# Exercise refusal before PostgreSQL or any entrypoint could initialize storage.
for state in empty wrong-major recovery standby legacy-schema; do
  set +e
  docker run --rm --network none --user 70:70 --read-only --cap-drop ALL \
    --tmpfs /tmp:uid=70,gid=70,mode=0700 -v "$source_dir:/etc/relay-budget:ro" \
    -e PGDATA=/tmp/pgdata -e PROBE_STATE="$state" --entrypoint /bin/sh "$image" -ec '
    if [ "$PROBE_STATE" != empty ]; then
      mkdir "$PGDATA"
      printf "17\n" > "$PGDATA/PG_VERSION"
      printf "4\n" > "$PGDATA/relay-budget-schema-version"
      case "$PROBE_STATE" in
        legacy-schema) printf "1\n" > "$PGDATA/relay-budget-schema-version" ;;
        wrong-major) printf "16\n" > "$PGDATA/PG_VERSION" ;;
        recovery) touch "$PGDATA/recovery.signal" ;;
        standby) touch "$PGDATA/standby.signal" ;;
      esac
    fi
    exec /bin/sh /etc/relay-budget/start.sh' > "$test_dir/refusal.log" 2>&1
  result=$?
  set -e
  [ "$result" -eq 78 ] || { cat "$test_dir/refusal.log" >&2; exit 1; }
done

# An independently wrong pin must refuse before creating PGDATA or an attempt marker.
set +e
docker run "${run_args[@]}" -v "$schema:/schema:ro" \
  -e RELAY_BUDGET_SCHEMA_MANIFEST_SHA256=0000000000000000000000000000000000000000000000000000000000000000 \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/initialize.sh > "$test_dir/pin.log" 2>&1
result=$?
set -e
[ "$result" -eq 78 ] || { cat "$test_dir/pin.log" >&2; exit 1; }
docker run "${run_args[@]}" --entrypoint /bin/sh "$image" -ec '[ ! -e "$PGDATA" ] && [ ! -e /var/lib/postgresql/data/.initialization-started ]'

# A valid manifest pin cannot excuse missing or altered SQL. An independently
# pinned manifest with an extra path must also fail the fixed-inventory check.
for corruption in altered missing extra; do
  cp "$schema/004-preflight-errors.sql" "$test_dir/original.sql"
  cp "$schema/SHA256SUMS" "$test_dir/original.sums"
  test_pin=$schema_pin
  case "$corruption" in
    altered) printf '\n-- altered\n' >> "$schema/004-preflight-errors.sql" ;;
    missing) mv "$schema/004-preflight-errors.sql" "$test_dir/missing.sql" ;;
    extra)
      printf '%064d  ../not-allowed\n' 0 >> "$schema/SHA256SUMS"
      test_pin=$(sha256sum "$schema/SHA256SUMS" | cut -d ' ' -f1) ;;
  esac
  set +e
  docker run "${run_args[@]}" -v "$schema:/schema:ro" \
    -e RELAY_BUDGET_SCHEMA_MANIFEST_SHA256="$test_pin" \
    --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/initialize.sh > "$test_dir/corrupt.log" 2>&1
  result=$?
  set -e
  [ "$result" -eq 78 ] || { cat "$test_dir/corrupt.log"; exit 1; }
  cp "$test_dir/original.sql" "$schema/004-preflight-errors.sql"
  cp "$test_dir/original.sums" "$schema/SHA256SUMS"
  docker run "${run_args[@]}" --entrypoint /bin/sh "$image" -ec '[ ! -e "$PGDATA" ] && [ ! -e /var/lib/postgresql/data/.initialization-started ]'
done

docker run "${run_args[@]}" -v "$schema:/schema:ro" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/initialize.sh > "$test_dir/initialize.log" 2>&1 || {
  cat "$test_dir/initialize.log" >&2; exit 1;
}
set +e
docker run "${run_args[@]}" -v "$schema:/schema:ro" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/initialize.sh > "$test_dir/repeat.log" 2>&1
result=$?
set -e
[ "$result" -eq 78 ] || { cat "$test_dir/repeat.log" >&2; exit 1; }

docker run -d "${run_args[@]}" --name "$test_name" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/start.sh >/dev/null
ready=false
for ((i=0;i<60;i++)); do
  if docker exec "$test_name" pg_isready -h /var/run/postgresql -U postgres -d relay_budget >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
[ "$ready" = true ] || { docker logs "$test_name"; exit 1; }
admin() { docker exec -i "$test_name" psql -X -h /var/run/postgresql -U postgres -d relay_budget -At -v ON_ERROR_STOP=1 "$@"; }
settings=$(admin -c "SELECT current_setting('fsync'),current_setting('full_page_writes'),current_setting('synchronous_commit'),current_setting('ssl'),current_setting('data_checksums'),pg_is_in_recovery()")
[ "$settings" = 'on|on|on|on|on|f' ] || { echo 'Incorrect durability or TLS settings' >&2; exit 1; }
[ "$(admin -c 'SELECT count(*) FROM worker_budget.authority')" = 0 ] || exit 1
[ "$(admin -c 'SELECT count(*) FROM worker_budget.qualifications')" = 0 ] || exit 1
[ "$(admin -c 'SELECT count(*) FROM worker_budget.scope_runs')" = 0 ] || exit 1
[ "$(admin -c 'SELECT count(*) FROM worker_budget.allocation_scopes')" = 0 ] || exit 1
[ "$(admin -c "SELECT to_regprocedure('worker_budget.preflight(uuid,uuid,text,jsonb)') IS NOT NULL")" = t ] || exit 1
[ "$(docker exec "$test_name" cat /var/lib/postgresql/data/pgdata/relay-budget-schema-version)" = 4 ] || exit 1
[ "$(admin -c "SELECT count(*) FROM pg_authid WHERE rolname IN ('relay_budget_allocator','relay_budget_router') AND rolpassword IS NULL")" = 2 ] || exit 1
# Synthetic test-only password. No production credentials are used by this script.
admin >/dev/null <<'SQL'
ALTER ROLE relay_budget_allocator PASSWORD 'synthetic-budget-test-only';
ALTER ROLE relay_budget_router PASSWORD 'synthetic-budget-test-only';
SQL
client() {
  local login=$1 mode=$2; shift 2
  docker exec -i -e PGPASSWORD=synthetic-budget-test-only "$test_name" psql -X \
    "host=localhost user=$login dbname=relay_budget sslmode=$mode sslrootcert=/run/tls/tls.crt" -At -v ON_ERROR_STOP=1 "$@"
}
wait_removed() {
  # --rm cleanup can finish just after `docker stop` returns on the worker daemon.
  for ((i=0;i<100;i++)); do
    if ! docker inspect "$test_name" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  echo 'Disposable PostgreSQL container removal did not complete' >&2
  return 1
}
[ "$(client relay_budget_allocator verify-full -c 'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')" = t ] || exit 1
[ "$(client relay_budget_router verify-full -c "SELECT pg_has_role(current_user,'relay_budget_reader','member'),pg_has_role(current_user,'relay_budget_owner','member'),has_table_privilege(current_user,'worker_budget.allocations','SELECT')")" = 't|f|f' ] || exit 1
[ "$(client relay_budget_allocator verify-full -c "SELECT pg_has_role(current_user,'relay_budget_client','member'),pg_has_role(current_user,'relay_budget_owner','member'),has_table_privilege(current_user,'worker_budget.allocations','INSERT')")" = 't|f|f' ] || exit 1
if client relay_budget_allocator disable -c 'SELECT 1' > "$test_dir/denied.log" 2>&1; then exit 1; fi
if client postgres verify-full -c 'SELECT 1' > "$test_dir/denied.log" 2>&1; then exit 1; fi
if client relay_budget_router verify-full -c 'SELECT * FROM worker_budget.allocations' > "$test_dir/denied.log" 2>&1; then exit 1; fi
if client relay_budget_allocator verify-full -c "SET ROLE relay_budget_owner" > "$test_dir/denied.log" 2>&1; then exit 1; fi
if client relay_budget_allocator verify-full -c "SELECT worker_budget.current_epoch('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002',repeat('a',64))" > "$test_dir/denied.log" 2>&1; then exit 1; fi
# The pinned official image declares SIGINT, so older Docker engines also use
# PostgreSQL's fast shutdown without requiring Docker 25's stop --signal option.
[ "$(docker inspect --format '{{.Config.StopSignal}}' "$test_name")" = SIGINT ] || exit 1
docker stop --timeout 90 "$test_name" >/dev/null
wait_removed
echo 'PASS: empty/recovery refusal, one-time schema initialization, projected-key TLS, durability settings, narrow roles, unseeded gate'

# A separate local fixture reproduces the retained version-1 format. It has no
# connection to an operator database or any Kubernetes context.
docker volume rm "$data_volume" >/dev/null
docker volume create "$data_volume" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
  -v "$data_volume:/var/lib/postgresql/data" "$image" -ec 'chown 70:70 /var/lib/postgresql/data'
cat > "$test_dir/legacy.sh" <<'SH'
set -eu
initdb -D "$PGDATA" --username=postgres --auth-local=peer --auth-host=scram-sha-256 --data-checksums >/dev/null
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql" >/dev/null
trap 'pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null' EXIT
psql -X -h /var/run/postgresql -U postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE relay_budget' >/dev/null
psql -X -h /var/run/postgresql -U postgres -d relay_budget -v ON_ERROR_STOP=1 -f /schema/001-authority.sql >/dev/null
psql -X -h /var/run/postgresql -U postgres -d relay_budget -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE relay_budget_allocator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE relay_budget_router LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT relay_budget_client TO relay_budget_allocator;
GRANT relay_budget_reader TO relay_budget_router;
INSERT INTO worker_budget.authority(instance_id,recovery_epoch,witness_digest,ready)
VALUES ('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002',repeat('a',64),false);
INSERT INTO worker_budget.allocations(allocation_id,issuer,tenant_id,worker_id,connection_id,deployment_digest,token_limit,original_deadline,recovery_epoch)
VALUES ('00000000-0000-4000-8000-000000000003','synthetic-test','00000000-0000-4000-8000-000000000004','00000000-0000-4000-8000-000000000005','00000000-0000-4000-8000-000000000006',repeat('b',64),123,2000000000000,'00000000-0000-4000-8000-000000000002');
INSERT INTO worker_budget.outbox(allocation_id,sequence,operation_id,request)
VALUES ('00000000-0000-4000-8000-000000000003',1,'00000000-0000-4000-8000-000000000007','{"fixture":"retained"}');
INSERT INTO worker_budget.receipts(operation_id) VALUES ('00000000-0000-4000-8000-000000000007');
SQL
printf '1\n' > "$PGDATA/relay-budget-schema-version"
SH
chmod 644 "$test_dir/legacy.sh"
# Fixture directory is traversable by container UID70; it contains no real secrets.
chmod 755 "$test_dir"
docker run "${run_args[@]}" -v "$schema:/schema:ro" -v "$test_dir/legacy.sh:/legacy.sh:ro" \
  --entrypoint /bin/sh "$image" /legacy.sh > "$test_dir/legacy.log" 2>&1 || { cat "$test_dir/legacy.log"; exit 1; }
legacy_pin=$(sha256sum "$schema/001-authority.sql" | cut -d ' ' -f1)
upgrade() {
  docker run "${run_args[@]}" -v "$schema:/schema:ro" \
    -e RELAY_BUDGET_LEGACY_SCHEMA_SHA256="$legacy_pin" \
    --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/upgrade.sh
}
upgrade > "$test_dir/upgrade.log" 2>&1 || { cat "$test_dir/upgrade.log"; exit 1; }
set +e
upgrade > "$test_dir/upgrade-repeat.log" 2>&1
result=$?
set -e
[ "$result" -eq 78 ] || { cat "$test_dir/upgrade-repeat.log"; exit 1; }
docker run -d "${run_args[@]}" --name "$test_name" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/start.sh >/dev/null
ready=false
for ((i=0;i<60;i++)); do
  if docker exec "$test_name" pg_isready -h /var/run/postgresql -U postgres -d relay_budget >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
[ "$ready" = true ] || { docker logs "$test_name"; exit 1; }
[ "$(admin -c 'SELECT ready FROM worker_budget.authority')" = f ] || exit 1
[ "$(admin -c 'SELECT token_limit,original_deadline,request_limit IS NULL,allocation_scope_digest IS NULL FROM worker_budget.allocations')" = '123|2000000000000|t|t' ] || exit 1
[ "$(admin -c "SELECT sequence,operation_id,request->>'fixture' FROM worker_budget.outbox")" = '1|00000000-0000-4000-8000-000000000007|retained' ] || exit 1
[ "$(admin -c 'SELECT operation_id FROM worker_budget.receipts')" = '00000000-0000-4000-8000-000000000007' ] || exit 1
[ "$(admin -c 'SELECT count(*) FROM worker_budget.scope_runs')" = 0 ] || exit 1
[ "$(admin -c "SELECT has_function_privilege('relay_budget_allocator','worker_budget.prepare_token_only(uuid,uuid,text,jsonb)','EXECUTE')")" = f ] || exit 1
[ "$(admin -c "SELECT has_function_privilege('relay_budget_allocator','worker_budget.prepare(uuid,uuid,text,jsonb)','EXECUTE')")" = t ] || exit 1
docker stop --timeout 90 "$test_name" >/dev/null
wait_removed
echo 'PASS: retained version-1 upgrade, unchanged debit/deadline/outbox/receipt, withdrawn authority, mediated prepare entry point, repeated upgrade refusal'

# A live authority is a hard refusal before a migration attempt starts. Retrying
# the unchanged preflight must still refuse, without leaving an attempt marker.
docker volume rm "$data_volume" >/dev/null
docker volume create "$data_volume" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
  -v "$data_volume:/var/lib/postgresql/data" "$image" -ec 'chown 70:70 /var/lib/postgresql/data'
sed "s/repeat('a',64),false/repeat('a',64),true/" "$test_dir/legacy.sh" > "$test_dir/legacy-active.sh"
chmod 644 "$test_dir/legacy-active.sh"
docker run "${run_args[@]}" -v "$schema:/schema:ro" -v "$test_dir/legacy-active.sh:/legacy.sh:ro" \
  --entrypoint /bin/sh "$image" /legacy.sh > "$test_dir/legacy-active.log" 2>&1 || { cat "$test_dir/legacy-active.log"; exit 1; }
set +e
docker run "${run_args[@]}" -v "$schema:/schema:ro" \
  -e RELAY_BUDGET_LEGACY_SCHEMA_SHA256=0000000000000000000000000000000000000000000000000000000000000000 \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/upgrade.sh > "$test_dir/legacy-pin.log" 2>&1
result=$?
set -e
[ "$result" -eq 78 ] || exit 1
docker run "${run_args[@]}" --entrypoint /bin/sh "$image" -ec '[ ! -e "$PGDATA/relay-budget-upgrade-started" ]'
set +e
upgrade > "$test_dir/active-denied.log" 2>&1
result=$?
set -e
[ "$result" -ne 0 ] || exit 1
grep -q 'Withdraw authority before the offline schema upgrade' "$test_dir/active-denied.log"
docker run "${run_args[@]}" --entrypoint /bin/sh "$image" -ec '[ "$(cat "$PGDATA/relay-budget-schema-version")" = 1 ] && [ ! -e "$PGDATA/relay-budget-upgrade-started" ]'
set +e
upgrade > "$test_dir/active-retry.log" 2>&1
result=$?
set -e
[ "$result" -ne 0 ] || exit 1
grep -q 'Withdraw authority before the offline schema upgrade' "$test_dir/active-retry.log"
echo 'PASS: original schema pin checked before attempt; active authority repeatedly refuses preflight without consuming a migration attempt'

# A reviewed SQL file can fail after a prior migration committed. Simulate that
# on disposable data, then prove marker 1 and the persistent attempt refuse replay.
docker volume rm "$data_volume" >/dev/null
docker volume create "$data_volume" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
  -v "$data_volume:/var/lib/postgresql/data" "$image" -ec 'chown 70:70 /var/lib/postgresql/data'
docker run "${run_args[@]}" -v "$schema:/schema:ro" -v "$test_dir/legacy.sh:/legacy.sh:ro" \
  --entrypoint /bin/sh "$image" /legacy.sh > "$test_dir/partial-legacy.log" 2>&1 || { cat "$test_dir/partial-legacy.log"; exit 1; }
printf 'BEGIN; SELECT 1/0; COMMIT;\n' > "$schema/003-scoped-requests.sql"
(cd "$schema" && sha256sum source-commit 001-authority.sql 002-preflight.sql 003-scoped-requests.sql 004-preflight-errors.sql) > "$schema/SHA256SUMS"
partial_pin=$(sha256sum "$schema/SHA256SUMS" | cut -d ' ' -f1)
set +e
docker run "${run_args[@]}" -v "$schema:/schema:ro" \
  -e RELAY_BUDGET_LEGACY_SCHEMA_SHA256="$legacy_pin" \
  -e RELAY_BUDGET_SCHEMA_MANIFEST_SHA256="$partial_pin" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/upgrade.sh > "$test_dir/partial.log" 2>&1
result=$?
set -e
[ "$result" -ne 0 ] || exit 1
grep -q 'division by zero' "$test_dir/partial.log"
docker run "${run_args[@]}" --entrypoint /bin/sh "$image" -ec '[ "$(cat "$PGDATA/relay-budget-schema-version")" = 1 ] && [ -d "$PGDATA/relay-budget-upgrade-started" ]'
set +e
docker run "${run_args[@]}" -v "$schema:/schema:ro" \
  -e RELAY_BUDGET_LEGACY_SCHEMA_SHA256="$legacy_pin" \
  -e RELAY_BUDGET_SCHEMA_MANIFEST_SHA256="$partial_pin" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/upgrade.sh > "$test_dir/partial-retry.log" 2>&1
result=$?
set -e
[ "$result" -eq 78 ] || exit 1
echo 'PASS: partial migration failure preserves marker 1 and refuses automatic replay'
