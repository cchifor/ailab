#!/usr/bin/env bash
# Uses the real pinned image and explicitly supplied, reviewed Relay schema.
# Local Docker only; never reaches a Kubernetes context or production database.
set -euo pipefail
[ "$#" -eq 1 ] || { echo "usage: $0 <reviewed-001-authority.sql>" >&2; exit 64; }
schema=$(realpath "$1")
[ -s "$schema" ] || exit 64
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_dir="$repo/kubernetes/optional/relay-budget-authority"
image=$(awk '/image:/ {print $2; exit}' "$source_dir/postgres.yaml")
[[ "$image" == *@sha256:* ]] || exit 65
[ "$(awk '/image:/ {print $2; exit}' "$source_dir/initialize/job.yaml")" = "$image" ] || exit 65
test_dir=$(mktemp -d "$repo/.budget-postgres-test.XXXXXX")
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
  -e PGDATA=/var/lib/postgresql/data/pgdata)

# Exercise refusal before PostgreSQL or any entrypoint could initialize storage.
for state in empty wrong-major recovery standby; do
  set +e
  docker run --rm --network none --user 70:70 --read-only --cap-drop ALL \
    --tmpfs /tmp:uid=70,gid=70,mode=0700 -v "$source_dir:/etc/relay-budget:ro" \
    -e PGDATA=/tmp/pgdata -e PROBE_STATE="$state" --entrypoint /bin/sh "$image" -ec '
    if [ "$PROBE_STATE" != empty ]; then
      mkdir "$PGDATA"
      printf "17\n" > "$PGDATA/PG_VERSION"
      printf "1\n" > "$PGDATA/relay-budget-schema-version"
      case "$PROBE_STATE" in
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

docker run "${run_args[@]}" -v "$schema:/schema/001-authority.sql:ro" \
  --entrypoint /bin/sh "$image" /etc/relay-budget/initialize/initialize.sh > "$test_dir/initialize.log" 2>&1 || {
  cat "$test_dir/initialize.log" >&2; exit 1;
}
set +e
docker run "${run_args[@]}" -v "$schema:/schema/001-authority.sql:ro" \
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
echo 'PASS: empty/recovery refusal, one-time schema initialization, projected-key TLS, durability settings, narrow roles, unseeded gate'
