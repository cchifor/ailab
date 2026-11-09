#!/usr/bin/env python3
"""Explicit local-only smoke; one disposable no-network PG17 container, no credentials."""
from pathlib import Path
import hashlib
import subprocess
import sys
from uuid import uuid4

root = Path(__file__).resolve().parents[2]
source = Path(sys.argv[1]).resolve()
schema = source / 'deploy/worker-candidate-journal'
assert schema.is_dir()
image = 'docker.io/library/postgres:17.11-alpine@sha256:3540c0362c809df084ffac0ae69fe794515f9de92692f02178bcdce0ef0302ac'
container_name = 'relay-candidate-journal-smoke-' + uuid4().hex[:12]
args = ['docker', 'run', '--name', container_name, '--rm', '--pull', 'never', '--network', 'none', '--user', '70:70',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--read-only',
        '--tmpfs', '/var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=192m,uid=70,gid=70,mode=0700',
        '--tmpfs', '/var/run/postgresql:rw,noexec,nosuid,nodev,size=16m,uid=70,gid=70,mode=0700',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=32m,uid=70,gid=70,mode=0700',
        '-e', 'PGDATA=/var/lib/postgresql/data/pgdata',
        '-v', str(schema) + ':/schema:ro',
        '-v', str(root / 'kubernetes/optional/relay-candidate-journal') + ':/bundle:ro']
for key, name in [('SCHEMA_001_SHA256', '001-journal.sql'), ('SCHEMA_002_SHA256', '002-signed-clients.sql')]:
    args += ['-e', key + '=' + hashlib.sha256((schema / name).read_bytes()).hexdigest()]
script = r'''set -eu
# Bad source is refused before even the initialization-attempt marker exists.
if SCHEMA_001_SHA256=$(printf '%064d' 0) /bin/sh /bundle/initialize/initialize.sh 2>/dev/null; then exit 1; fi
[ ! -e /var/lib/postgresql/data/.initialization-started ]
[ ! -e "$PGDATA" ]
if /bin/sh /bundle/start.sh 2>/dev/null; then exit 1; fi
/bin/sh /bundle/initialize/initialize.sh
[ "$(cat "$PGDATA/relay-candidate-journal-schema-version")" = 2 ]
if pg_ctl -D "$PGDATA" status; then exit 1; fi
# An exact retry is refused without recreating state.
if /bin/sh /bundle/initialize/initialize.sh 2>/dev/null; then exit 1; fi
pg_ctl -D "$PGDATA" -w start -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql"
trap 'pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null' EXIT
psql -X -h /var/run/postgresql -U postgres -d relay_candidate -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM worker_candidate.authority) OR EXISTS (SELECT 1 FROM worker_candidate.runs) OR
 EXISTS (SELECT 1 FROM worker_candidate.clients) OR EXISTS (SELECT 1 FROM worker_candidate.launches) THEN
 RAISE EXCEPTION 'unexpected initialized authority'; END IF;
 IF (SELECT count(*) FROM pg_authid WHERE rolname IN ('relay_candidate_operator_login','relay_candidate_agent_service','relay_candidate_holder_service')
 AND rolcanlogin AND rolpassword IS NULL AND NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls) <> 3 THEN
 RAISE EXCEPTION 'runtime login scope'; END IF;
 IF has_table_privilege('relay_candidate_agent_service','worker_candidate.authority','UPDATE') OR
 has_table_privilege('relay_candidate_holder_service','worker_candidate.launches','INSERT') OR
 pg_has_role('relay_candidate_agent_service','relay_candidate_operator','member') OR
 pg_has_role('relay_candidate_holder_service','relay_candidate_agent','member') THEN
 RAISE EXCEPTION 'unscoped runtime privilege'; END IF;
END $$;
SQL
printf 'Local PostgreSQL journal smoke passed; no live Kubernetes or durability qualification.\n'
'''
try:
    subprocess.run(args + [image, '/bin/sh', '-c', script], check=True, timeout=180)
finally:
    # Also clean up if the Docker client times out before the container exits.
    subprocess.run(['docker', 'rm', '-fv', container_name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False, timeout=15)
