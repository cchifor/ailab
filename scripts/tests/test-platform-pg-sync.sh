#!/bin/bash
# Integration test of the openbao-platform-pg-sync script (the ConfigMap in
# kubernetes/apps/infrastructure/platform-access/pg-sync.yaml) under the CronJob's OWN image, against
# a throwaway Postgres started inside the container and a stub OpenBao (python http.server that
# records every request and keeps the KV in a JSON file the test can read and edit). It proves the
# ADR 0028 contract without a cluster:
#   1. first run: roles created LOGIN/NOSUPERUSER/NOBYPASSRLS, published once per slot (create-only
#      CAS) with the three fields; as the new role: a keyword-named table and a keyword-named schema
#      read; an RLS table reads EMPTY without app.tenant_id and its rows with it; an INSERT dies as a
#      read-only transaction under the role default, and INSERT + CREATE TABLE are STILL refused —
#      `permission denied` — after `SET default_transaction_read_only = off` (privilege, not
#      setting); the excluded database's table is `permission denied`; a tenant schema in
#      platform_managed reads; a table the migrator creates AFTER the run reads at once (default
#      privileges); a table a tenant role creates after the run does NOT read until the next run.
#   2. second run: every slot KEPT — same password still logs in, no KV write at all; the
#      tenant-created table now reads.
#   3. validity inside the rotate window (the test edits the stub's KV) -> rotated; the old
#      password no longer logs in, the published one does.
#   4. FORCE_ROTATE=1 -> rotated.  5. a role dropped by hand -> recreated + rotated ("no longer logs
#      in"), grants back.  6. retiring a slot: DROP OWNED in every database + DROP ROLE; the re-run
#      converges.  7. `keycloak` in the allowlist aborts before touching Postgres or KV.
#   8. no run's stdout/stderr ever contains a password; the stub saw passwords ONLY in KV write bodies.
# Requires docker (the manifests CI job has it). Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/platform-access/pg-sync.yaml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

IMAGE="$(sed -n 's/^ *image: *\(ghcr.io\/cloudnative-pg\/postgresql:[^ ]*\).*/\1/p' "$MANIFEST" | head -1)"
[ -n "$IMAGE" ] || { echo "cannot find the CronJob image in $MANIFEST" >&2; exit 1; }

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$MANIFEST" "$WORK/sync.py" <<'PY'
import sys, yaml
manifest, out = sys.argv[1:3]
docs = [d for d in yaml.safe_load_all(open(manifest, encoding="utf-8")) if d]
cm = next(d for d in docs if d.get("kind") == "ConfigMap" and "sync.py" in d.get("data", {}))
open(out, "w", encoding="utf-8", newline="\n").write(cm["data"]["sync.py"])
PY

# ---- stub OpenBao: k8s-auth login, KV v2 read / merge-patch / create-only CAS, request log ---------
cat > "$WORK/stub_bao.py" <<'STUB'
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
STATE = os.environ["STUB_STATE"]
KV = os.path.join(STATE, "kv.json")
LOG = os.path.join(STATE, "requests.log")

def load():
    return json.load(open(KV)) if os.path.exists(KV) else {}

def save(d):
    json.dump(d, open(KV, "w"), indent=1)

class H(BaseHTTPRequestHandler):
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}
    def _send(self, code, doc=None):
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        if doc is not None: self.wfile.write(json.dumps(doc).encode())
    def _log(self, body):
        with open(LOG, "a") as fh: fh.write("%s %s %s\n" % (self.command, self.path, json.dumps(body, sort_keys=True)))
    def log_message(self, *a): pass
    def do_POST(self):
        body = self._body(); self._log(body)
        if self.path == "/v1/auth/kubernetes/login":
            return self._send(200, {"auth": {"client_token": "stub-token"}})
        w = self.path.rsplit("/", 1)[-1]; kv = load()
        if body.get("options", {}).get("cas") == 0 and w in kv:
            return self._send(400, {"errors": ["check-and-set parameter did not match the current version"]})
        kv[w] = dict(body["data"]); save(kv); return self._send(200, {"data": {"version": 1}})
    def do_GET(self):
        self._log({}); w = self.path.rsplit("/", 1)[-1]; kv = load()
        if w not in kv: return self._send(404, {"errors": []})
        return self._send(200, {"data": {"data": kv[w], "metadata": {"version": 1}}})
    def do_PATCH(self):
        body = self._body(); self._log(body); w = self.path.rsplit("/", 1)[-1]; kv = load()
        if w not in kv: return self._send(404, {"errors": []})
        kv[w].update(body["data"]); save(kv); return self._send(200, {"data": {"version": 2}})

HTTPServer(("127.0.0.1", 8200), H).serve_forever()
STUB

# ---- the driver, run INSIDE the container as the image's postgres user --------------------------------
cat > "$WORK/driver.sh" <<'DRIVER'
set -euo pipefail
BIN=/usr/lib/postgresql/16/bin
export PGDATA=/tmp/pgdata STATE=/w/state
[ -d "$STATE" ] || fail_early=1; [ -w "$STATE" ] || { echo "state dir not writable by uid $(id -u)" >&2; exit 1; }
echo "su-pw-for-test" > /tmp/pw
"$BIN/initdb" -U postgres --auth-local=trust --auth-host=scram-sha-256 --pwfile=/tmp/pw >/tmp/initdb.log 2>&1
cat >> "$PGDATA/postgresql.conf" <<CONF
listen_addresses = '127.0.0.1'
unix_socket_directories = '/tmp'
password_encryption = scram-sha-256
log_min_messages = warning
CONF
"$BIN/pg_ctl" -w -o "-p 5432" start >/tmp/pg.log 2>&1
STUB_STATE="$STATE" python3 /w/stub_bao.py & STUB_PID=$!
sleep 1
PSQL="$BIN/psql -h /tmp -U postgres -v ON_ERROR_STOP=1 -qtA"
$PSQL -d postgres <<'SQL'
CREATE ROLE app LOGIN PASSWORD 'app-pw' CREATEROLE;
CREATE ROLE tenant_abc LOGIN PASSWORD 'tenant-pw';
CREATE DATABASE airlock OWNER app;
CREATE DATABASE profile OWNER app;
CREATE DATABASE keycloak OWNER app;
CREATE DATABASE platform_managed OWNER app;
SQL
$PSQL -d airlock <<'SQL'
SET ROLE app;
CREATE TABLE app_table_drafts (id serial PRIMARY KEY, tenant_id text NOT NULL, note text);
INSERT INTO app_table_drafts (tenant_id, note) VALUES ('t1','a'),('t1','b'),('t2','c');
ALTER TABLE app_table_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_table_drafts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app_table_drafts FOR ALL TO public
  USING (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE "user" (id int, "select" text);
INSERT INTO "user" VALUES (1, 'kw');
CREATE SCHEMA "select";
CREATE TABLE "select"."order" (id int);
INSERT INTO "select"."order" VALUES (7);
CREATE TABLE outbox (id serial PRIMARY KEY);
SQL
$PSQL -d keycloak <<'SQL'
SET ROLE app;
CREATE TABLE user_entity (id int, secret text);
INSERT INTO user_entity VALUES (1, 'never-readable');
SQL
$PSQL -d platform_managed <<'SQL'
GRANT CREATE ON DATABASE platform_managed TO tenant_abc;
SET ROLE tenant_abc;
CREATE SCHEMA platform_data_abc;
CREATE TABLE platform_data_abc.rows (id int);
INSERT INTO platform_data_abc.rows VALUES (42);
SQL

# ---- helpers --------------------------------------------------------------------------------------
export SA_DIR=/tmp/sa; mkdir -p "$SA_DIR"; echo "stub-jwt" > "$SA_DIR/token"
export BAO_ADDR=http://127.0.0.1:8200 BAO_ROLE=platform-pg-sync BAO_KV_MOUNT=af
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=postgres PGPASSWORD=su-pw-for-test PGDATABASE=postgres PGSSLMODE=disable
export PLATFORM_DATABASES="airlock profile platform_managed" PG_PASSWORD_VALID_SECONDS=1209600 PG_ROTATE_BEFORE_SECONDS=604800
N=0
run() {  # run <LIVE_SLOTS> <RETIRED_SLOTS> [extra env...]; output -> $STATE/run-N.log; returns exit status
  N=$((N + 1)); local live="$1" retired="$2"; shift 2
  local rc=0
  env LIVE_SLOTS="$live" RETIRED_SLOTS="$retired" "$@" python3 /w/sync.py > "$STATE/run-$N.log" 2>&1 || rc=$?
  echo "run $N (LIVE='$live' RETIRED='$retired' $*): exit $rc"
  return $rc
}
fail() { echo "FAIL: $*" >&2; [ -f "$STATE/run-$N.log" ] && sed "s/^/  | /" "$STATE/run-$N.log" >&2; exit 1; }
expect_log() { grep -qF -- "$2" "$STATE/run-$1.log" || { cat "$STATE/run-$1.log" >&2; fail "run $1: missing '$2'"; }; }
pw() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["platform_pg_password"])' "$STATE/kv.json" "dev-worker-$1"; }
writes() { grep -cE '^(POST|PATCH) /v1/af/data/' "$STATE/requests.log" || true; }
# as <slot> <db> [PGOPTIONS] -- run a query as the slot's role over TCP with the PUBLISHED password
as() { local slot=$1 db=$2 opts="${3:-}"; shift 2; PGOPTIONS="$opts" PGPASSWORD="$(pw "$slot")" "$BIN/psql" -h 127.0.0.1 -U "dw${slot}_platform_ro" -d "$db" -qtA -v ON_ERROR_STOP=1; }
as_err() { as "$@" 2>&1 >/dev/null || true; }   # stderr of a query expected to fail

# ---- 1. first run ---------------------------------------------------------------------------------
run "1 2" "" || fail "first run failed"
expect_log 1 "rotated dev-worker-1: dw1_platform_ro"; expect_log 1 "fields missing"
expect_log 1 "published 2/2 slots (rotated 2, kept 0)"
[ "$(writes)" = 2 ] || fail "expected exactly 2 KV writes after run 1, saw $(writes)"
grep -q '^POST /v1/af/data/dev-workers/dev-worker-1 .*"cas": 0' "$STATE/requests.log" || fail "first publish was not create-only CAS"
python3 - "$STATE/kv.json" <<'PY'
import json, sys
kv = json.load(open(sys.argv[1]))
for w in ("dev-worker-1", "dev-worker-2"):
    assert sorted(kv[w]) == ["platform_pg_password", "platform_pg_user", "platform_pg_valid_until"], kv[w].keys()
    assert kv[w]["platform_pg_user"] == "dw%s_platform_ro" % w[-1]
    assert len(kv[w]["platform_pg_password"]) >= 40
print("kv shape ok")
PY
attrs="$($PSQL -d postgres -c "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolconnlimit, rolvaliduntil > now() + interval '13 days' FROM pg_roles WHERE rolname='dw1_platform_ro'")"
[ "$attrs" = "t|f|f|f|10|t" ] || fail "role attributes: $attrs"
[ "$(echo 'SELECT count(*) FROM app_table_drafts' | as 1 airlock)" = 0 ] || fail "RLS table must read EMPTY without app.tenant_id"
[ "$(echo 'SELECT count(*) FROM app_table_drafts' | as 1 airlock '-c app.tenant_id=t1')" = 2 ] || fail "RLS table must read the tenant's rows with app.tenant_id"
[ "$(echo 'SELECT "select" FROM "user"' | as 1 airlock)" = kw ] || fail "keyword-named table not readable"
[ "$(echo 'SELECT id FROM "select"."order"' | as 1 airlock)" = 7 ] || fail "keyword-named schema not readable"
[ "$(echo 'SELECT id FROM platform_data_abc.rows' | as 2 platform_managed)" = 42 ] || fail "tenant schema not readable"
# Two layers, checked separately: the role-level default makes a plain INSERT die as a read-only
# transaction; flipping that default off for the session (any role may) must leave the write refused
# BY PRIVILEGE — that is the layer the boundary actually rests on.
echo 'INSERT INTO outbox DEFAULT VALUES' | as_err 1 airlock | grep -q "read-only transaction" || fail "INSERT must die as a read-only transaction under the role default"
printf 'SET default_transaction_read_only = off;\nINSERT INTO outbox DEFAULT VALUES;\n' | as_err 1 airlock | grep -q "permission denied for table outbox" || fail "INSERT must STILL be permission denied after SET default_transaction_read_only=off"
printf 'SET default_transaction_read_only = off;\nCREATE TABLE t ();\n' | as_err 1 airlock | grep -q "permission denied for schema public" || fail "CREATE TABLE must be permission denied"
echo 'SELECT secret FROM user_entity' | as_err 1 keycloak | grep -q "permission denied" || fail "excluded database must be permission denied"
$PSQL -d airlock -c "SET ROLE app; CREATE TABLE later_by_app (id int); INSERT INTO later_by_app VALUES (1);"
[ "$(echo 'SELECT id FROM later_by_app' | as 1 airlock)" = 1 ] || fail "a table the migrator creates after the run must read at once"
$PSQL -d platform_managed -c "SET ROLE tenant_abc; CREATE TABLE platform_data_abc.later (id int); INSERT INTO platform_data_abc.later VALUES (9);"
echo 'SELECT id FROM platform_data_abc.later' | as_err 2 platform_managed | grep -q "permission denied" || fail "a tenant-created table must NOT read before the next run"
echo "1: first run ok"

# ---- 2. idempotent re-run -------------------------------------------------------------------------
pw1_before="$(pw 1)"
run "1 2" "" || fail "second run failed"
expect_log 2 "kept dev-worker-1"; expect_log 2 "published 2/2 slots (rotated 0, kept 2)"
[ "$(writes)" = 2 ] || fail "a kept slot must not write KV (saw $(writes) writes)"
[ "$(pw 1)" = "$pw1_before" ] || fail "password changed on a kept slot"
[ "$(echo 'SELECT id FROM platform_data_abc.later' | as 2 platform_managed)" = 9 ] || fail "tenant-created table must read after the next run"
echo "2: re-run kept both slots, no KV write, tenant table now readable"

# ---- 3. validity inside the rotate window -> rotated -----------------------------------------------
python3 - "$STATE/kv.json" <<'PY'
import json, sys, time
p = sys.argv[1]; kv = json.load(open(p))
kv["dev-worker-1"]["platform_pg_valid_until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400))
json.dump(kv, open(p, "w"))
PY
run "1 2" "" || fail "run 3 failed"
expect_log 3 "rotated dev-worker-1"; expect_log 3 "h of validity left"; expect_log 3 "kept dev-worker-2"
[ "$(pw 1)" != "$pw1_before" ] || fail "run 3 did not rotate dev-worker-1"
PGPASSWORD="$pw1_before" "$BIN/psql" -h 127.0.0.1 -U dw1_platform_ro -d airlock -c 'select 1' >/dev/null 2>&1 && fail "old password still logs in after rotation"
[ "$(echo 'SELECT 1' | as 1 airlock)" = 1 ] || fail "new password does not log in"
echo "3: near-expiry slot rotated, old password dead"

# ---- 4. FORCE_ROTATE ------------------------------------------------------------------------------
pw2_before="$(pw 2)"
run "1 2" "" FORCE_ROTATE=1 || fail "run 4 failed"
expect_log 4 "published 2/2 slots (rotated 2, kept 0)"; expect_log 4 "FORCE_ROTATE"
[ "$(pw 2)" != "$pw2_before" ] || fail "FORCE_ROTATE did not rotate"
echo "4: FORCE_ROTATE rotated both"

# ---- 5. a role dropped by hand -> recreated and rotated ---------------------------------------------
$PSQL -d airlock -c "DROP OWNED BY dw2_platform_ro"; $PSQL -d profile -c "DROP OWNED BY dw2_platform_ro"; $PSQL -d platform_managed -c "DROP OWNED BY dw2_platform_ro"; $PSQL -d postgres -c "DROP OWNED BY dw2_platform_ro; DROP ROLE dw2_platform_ro"
run "1 2" "" || fail "run 5 failed"
expect_log 5 "rotated dev-worker-2"; expect_log 5 "published password no longer logs in"; expect_log 5 "kept dev-worker-1"
[ "$(echo 'SELECT id FROM platform_data_abc.rows' | as 2 platform_managed)" = 42 ] || fail "recreated role lost its grants"
echo "5: hand-dropped role recreated with grants and a fresh password"

# ---- 6. retire a slot -----------------------------------------------------------------------------
run "1" "2" || fail "run 6 failed"
expect_log 6 "retired dw2_platform_ro"
[ "$($PSQL -d postgres -c "SELECT count(*) FROM pg_roles WHERE rolname='dw2_platform_ro'")" = 0 ] || fail "retired role still exists"
[ "$($PSQL -d airlock -c "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee='dw2_platform_ro'")" = 0 ] || fail "retired role still holds grants"
run "1" "2" || fail "run 7 (retire converge) failed"
expect_log 7 "published 1/1 slots"
echo "6: retired slot revoked everywhere; re-run converges"

# ---- 7. keycloak in the allowlist aborts before anything ------------------------------------------
w_before="$(writes)"; pw1_before="$(pw 1)"
run "1" "" PLATFORM_DATABASES="airlock keycloak" && fail "keycloak in PLATFORM_DATABASES must abort"
expect_log 8 "keycloak is the identity store"
[ "$(writes)" = "$w_before" ] && [ "$(pw 1)" = "$pw1_before" ] || fail "the keycloak abort touched KV"
echo "7: keycloak abort is fail-closed"

# ---- 8. no password in any run log; passwords only in KV write bodies -------------------------------
python3 - "$STATE" <<'PY'
import glob, json, os, re, sys
state = sys.argv[1]
seen = set()
for line in open(os.path.join(state, "requests.log")):
    method, path, body = line.split(" ", 2)
    doc = json.loads(body)
    pw = (doc.get("data") or {}).get("platform_pg_password")
    if pw:
        assert method in ("POST", "PATCH") and path.startswith("/v1/af/data/dev-workers/"), line[:60]
        seen.add(pw)
    else:
        assert "platform_pg_password" not in body, "password outside a KV write: " + line[:60]
assert seen, "no password was ever published"
for log in glob.glob(os.path.join(state, "run-*.log")):
    text = open(log).read()
    for pw in seen:
        assert pw not in text, "%s contains a published password" % log
    assert not re.search(r"PASSWORD '", text), "%s echoes an ALTER ROLE ... PASSWORD statement" % log
print("8: %d passwords published, none in any run log (%d logs)" % (len(seen), len(glob.glob(os.path.join(state, 'run-*.log')))))
PY

kill "$STUB_PID" 2>/dev/null || true
"$BIN/pg_ctl" -w stop >/dev/null 2>&1 || true
echo "driver: all scenarios passed"
DRIVER

HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
mkdir -p "$WORK/state"
chmod 755 "$WORK"; chmod 644 "$WORK/sync.py" "$WORK/stub_bao.py" "$WORK/driver.sh"; chmod 777 "$WORK/state"

# The image's own user (uid 26, postgres) — initdb refuses root, and that is the CronJob's shape too
# (runAsNonRoot). /w is read-only except state/.
docker run --rm -u 26:999 -v "$HOSTWORK:/w" --entrypoint bash "$IMAGE" /w/driver.sh
echo "test-platform-pg-sync: OK (create/keep/rotate/force/recreate/retire/abort, RLS via GUC, read-only by privilege, no password in logs)"
