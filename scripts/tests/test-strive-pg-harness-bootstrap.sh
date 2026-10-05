#!/bin/bash
# Integration test of the strive-pg-harness bootstrap script
# (kubernetes/apps/infrastructure/strive-pg-harness/bootstrap.py) under the Job's OWN image, against a
# throwaway Postgres started inside the container. It proves:
#   1. no Secret yet (HARNESS_PASSWORD unset): exit 0 and NOTHING created.
#   2. a too-short password: exit 1 and nothing created.
#   3. first run: role `harness` LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION,
#      database `harness` OWNED by it; the role logs in with the password and can create a table in
#      the database's public schema (what the chart's migration init container does).
#   4. second run: a no-op -- "password kept", the stored verifier is byte-identical.
#   5. drift: SUPERUSER/BYPASSRLS/CREATEDB granted by hand, a membership in `app`, the database
#      handed to `app` -- all repaired by the next run.
#   6. rotation: a new HARNESS_PASSWORD is applied; the old one no longer logs in.
#   7. a database that already exists (owned by someone else) before the role does: adopted.
#   7b. a CREATE ROLE ... PASSWORD that FAILS does not log its statement (and verifier) either.
#   8. no password ever appears in the script's output or in the server log, although the server
#      logs EVERY statement (log_statement = all): the session silences logging, and the server only
#      ever receives a SCRAM verifier.
# Requires docker (the manifests CI job has it). Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="$REPO_ROOT/kubernetes/apps/infrastructure/strive-pg-harness"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

IMAGE="$(sed -n 's/^ *image: *\(ghcr.io\/cloudnative-pg\/postgresql:[^ ]*\).*/\1/p' "$DIR/bootstrap-job.yaml" | head -1)"
[ -n "$IMAGE" ] || { echo "cannot find the Job image in $DIR/bootstrap-job.yaml" >&2; exit 1; }
cp "$DIR/bootstrap.py" "$WORK/bootstrap.py"

cat > "$WORK/driver.sh" <<'DRIVER'
set -euo pipefail
BIN=/usr/lib/postgresql/16/bin
export PGDATA=/tmp/pgdata
fail() { echo "FAIL: $*" >&2; exit 1; }
echo "su-pw-for-test" > /tmp/pw
"$BIN/initdb" -U postgres --auth-local=trust --auth-host=scram-sha-256 --pwfile=/tmp/pw >/tmp/initdb.log 2>&1
cat >> "$PGDATA/postgresql.conf" <<CONF
listen_addresses = '127.0.0.1'
unix_socket_directories = '/tmp'
password_encryption = scram-sha-256
log_statement = 'all'
logging_collector = off
CONF
"$BIN/pg_ctl" -w -o "-p 5432" -l /tmp/pg.log start >/dev/null
PSQL="$BIN/psql -h /tmp -U postgres -v ON_ERROR_STOP=1 -qtA"
$PSQL -d postgres -c "CREATE ROLE app LOGIN PASSWORD 'app-pw' CREATEROLE"

PW1="harness-first-0123456789abcdef"
PW2="harness-second-fedcba9876543210"
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=postgres PGPASSWORD=su-pw-for-test PGDATABASE=postgres PGSSLMODE=disable
N=0
run() { # run <password-or-empty> ; output in /tmp/run.$N
  N=$((N + 1))
  if [ -n "$1" ]; then HARNESS_PASSWORD="$1" python3 -u /w/bootstrap.py >/tmp/run.$N 2>&1; else
    env -u HARNESS_PASSWORD python3 -u /w/bootstrap.py >/tmp/run.$N 2>&1; fi
}
expect() { grep -q "$2" /tmp/run.$1 || { cat /tmp/run.$1 >&2; fail "run $1: expected '$2'"; }; }
q() { $PSQL -d postgres -c "$1"; }
attrs() { q "SELECT rolcanlogin::text||rolsuper::text||rolbypassrls::text||rolcreatedb::text||rolcreaterole::text||rolreplication::text FROM pg_roles WHERE rolname='harness'"; }
owner() { q "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='harness'"; }
login() { PGPASSWORD="$1" "$BIN/psql" -h 127.0.0.1 -U harness -d harness -qtAc "SELECT current_user" 2>/dev/null; }
verifier() { q "SELECT rolpassword FROM pg_authid WHERE rolname='harness'"; }

# 1. no Secret yet
run "" || fail "a missing Secret must exit 0"
expect $N "not rendered yet"
[ -z "$(q "SELECT 1 FROM pg_roles WHERE rolname='harness'")" ] || fail "role created without a password"
[ -z "$(owner)" ] || fail "database created without a password"
echo "1: no Secret -> nothing done, exit 0"

# 2. too short
if run "short"; then fail "a short password must fail"; fi
expect $N "shorter than"
[ -z "$(q "SELECT 1 FROM pg_roles WHERE rolname='harness'")" ] || fail "role created from a short password"
echo "2: short password -> refused"

# 3. first run
run "$PW1" || { cat /tmp/run.$N >&2; fail "first run"; }
expect $N "role harness created"
expect $N "database harness created, owner harness"
[ "$(attrs)" = "truefalsefalsefalsefalsefalse" ] || fail "attributes after create: $(attrs)"
[ "$(owner)" = harness ] || fail "owner after create: $(owner)"
[ "$(login "$PW1")" = harness ] || fail "harness cannot log in with the Secret's password"
PGPASSWORD="$PW1" "$BIN/psql" -h 127.0.0.1 -U harness -d harness -v ON_ERROR_STOP=1 -qtAc "CREATE TABLE migrations_probe (id int)" \
  || fail "harness cannot create a table in its own database"
case "$(verifier)" in SCRAM-SHA-256\$*) ;; *) fail "stored password is not a SCRAM verifier" ;; esac
# The session silences statement logging: even the VERIFIER (an offline-crackable hash) must not
# reach the statement log the platform's pods/log readers can see.
grep -q -F -- "$(verifier)" /tmp/pg.log && fail "the SCRAM verifier reached the statement log"
echo "3: first run -> role + database, login works, migrations can create tables"

# 4. second run is a no-op
V1="$(verifier)"
run "$PW1" || fail "second run"
expect $N "password kept"
expect $N "database harness present, owner harness"
[ "$(verifier)" = "$V1" ] || fail "a no-op run rewrote the password verifier"
echo "4: re-run -> no-op, verifier unchanged"

# 5. drift is repaired
q "ALTER ROLE harness SUPERUSER BYPASSRLS CREATEDB CREATEROLE REPLICATION"
q "GRANT app TO harness"
q "ALTER DATABASE harness OWNER TO app"
run "$PW1" || fail "drift run"
expect $N "revoked unexpected membership app"
expect $N "owner repaired: app -> harness"
[ "$(attrs)" = "truefalsefalsefalsefalsefalse" ] || fail "attributes after repair: $(attrs)"
[ "$(owner)" = harness ] || fail "owner after repair: $(owner)"
[ -z "$(q "SELECT 1 FROM pg_auth_members WHERE member='harness'::regrole")" ] || fail "membership survived"
echo "5: drift (attributes, membership, owner) -> repaired"

# 5b. NOLOGIN by hand: LOGIN comes back and the password still works
q "ALTER ROLE harness NOLOGIN"
run "$PW1" || fail "nologin run"
[ "$(login "$PW1")" = harness ] || fail "LOGIN not restored"
echo "5b: NOLOGIN -> LOGIN restored"

# 6. rotation
run "$PW2" || fail "rotation run"
expect $N "password set from the Secret"
[ "$(login "$PW2")" = harness ] || fail "new password does not log in"
[ -z "$(login "$PW1")" ] || fail "old password still logs in"
echo "6: rotation -> new password in, old out"

# 7. a pre-existing database owned by someone else, before the role exists
q "DROP DATABASE harness"; q "DROP ROLE harness"
q "CREATE DATABASE harness OWNER app"
run "$PW1" || fail "adopt run"
expect $N "role harness created"
expect $N "owner repaired: app -> harness"
[ "$(owner)" = harness ] || fail "pre-existing database not adopted"
echo "7: pre-existing database -> adopted"

# 7b. a password statement that FAILS must not log its text either (log_min_error_statement): a
# reserved role name makes CREATE ROLE ... PASSWORD error out after the verifier was bound.
if HARNESS_ROLE=pg_harness_reserved HARNESS_DATABASE=harness_reserved HARNESS_PASSWORD="$PW1" python3 -u /w/bootstrap.py >/tmp/run.err 2>&1; then
  fail "CREATE ROLE with a reserved name must fail"
fi
grep -q 'is reserved' /tmp/pg.log || fail "test setup: the server did not log the reserved-name error"
grep -q 'SCRAM-SHA-256' /tmp/pg.log && fail "a failed password statement wrote its verifier to the server log"
echo "7b: failed password statement -> nothing of it in the server log"

# 8. no password anywhere it can be read
for pw in "$PW1" "$PW2"; do
  if grep -l -F -- "$pw" /tmp/run.* /tmp/pg.log >/dev/null 2>&1; then
    fail "a password appeared in: $(grep -l -F -- "$pw" /tmp/run.* /tmp/pg.log | tr '\n' ' ')"
  fi
done
# Statically too: the only values bound to a PASSWORD clause are client-computed verifiers. A
# cleartext bind would be invisible above (the server hashes it, and the session log is off), but it
# would sit in pg_stat_activity for every pg_monitor member to read.
[ "$(grep -c 'PASSWORD %s' /w/bootstrap.py)" = 2 ] || fail "expected exactly two PASSWORD binds in bootstrap.py"
[ "$(grep -c 'PASSWORD %s").format(role), (verifier,))' /w/bootstrap.py)" = 2 ] || fail "a PASSWORD bind is not the SCRAM verifier"
grep -q "CREATE ROLE app" /tmp/pg.log || fail "the server log is not recording statements; check 8 proves nothing"
echo "8: no password in any script output or the statement log"

"$BIN/pg_ctl" -w stop >/dev/null 2>&1 || true
echo "driver: all scenarios passed"
DRIVER

HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
chmod 755 "$WORK"; chmod 644 "$WORK/bootstrap.py" "$WORK/driver.sh"

# The image's own user (uid 26, postgres): initdb refuses root. /w is read-only.
docker run --rm -u 26:999 -v "$HOSTWORK:/w:ro" --entrypoint bash "$IMAGE" /w/driver.sh
echo "test-strive-pg-harness-bootstrap: OK (absent/short Secret, create, no-op re-run, drift repair, rotation, adopt, no password in logs)"
