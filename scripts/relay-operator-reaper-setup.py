#!/usr/bin/env python3
"""relay-operator-reaper-setup.py: create or refresh the relay-operator reaper's database objects (idempotent).

    python scripts/relay-operator-reaper-setup.py            # apply kubernetes/apps/apps/relay/operator-reaper/setup.sql

Why a script and not a Job: relay-postgres rejects the postgres superuser over the network (pg_hba
`host all postgres all reject`, a deliberate hardening; postgres-bootstrap.yaml: "Admin recovery uses kubectl exec
and the local socket"), and no runtime role may hold CREATEROLE. So the reviewed setup.sql runs as postgres over
the pod's LOCAL socket: `kubectl exec -i relay-postgres-0 -c postgres -- psql -U postgres`.

The role password comes from the SOPS-managed Secret relay-operator-reaper, read in this process and sent ONLY on
psql's stdin (`\\set pw`), never on any argv, log or file; the session disables statement logging first. Prints the
setup's own readback ("reaper objects ready: 1") and nothing else from psql.

Run it once after the reaper is deployed, again whenever setup.sql changes, and after ANY relay-postgres restore or
re-initialization (roles are cluster globals a restore can lose): docs/runbooks/relay-operator-reaper.md.
"""
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "kubernetes" / "apps" / "apps" / "relay" / "operator-reaper" / "setup.sql"
CTX = ["kubectl", "--context", "admin@ai", "-n", "relay"]
PSQL = ["exec", "-i", "relay-postgres-0", "-c", "postgres", "--", "psql", "-X", "-q", "-At", "-U", "postgres",
        "-d", "relay", "-v", "ON_ERROR_STOP=1", "-f", "-"]
PASSWORD = re.compile(r"[A-Za-z0-9_-]{32,128}")  # secrets.token_urlsafe: never needs quoting inside '...'


def secret_password(run=subprocess.run) -> str:
    p = run(CTX + ["get", "secret", "relay-operator-reaper", "-o", "json"], capture_output=True, timeout=60)
    if p.returncode != 0:
        sys.exit("relay-operator-reaper-setup: cannot read the Secret relay-operator-reaper")
    pw = base64.b64decode(json.loads(p.stdout)["data"]["password"]).decode()
    if not PASSWORD.fullmatch(pw):
        sys.exit("relay-operator-reaper-setup: the Secret's password is not a secrets.token_urlsafe value (32-128 of "
                 "[A-Za-z0-9_-]); refusing. Regenerate it (docs/runbooks/relay-operator-reaper.md, 'Rotate the role "
                 "password'), let Flux apply the Secret, then run this again.")
    return pw


def script(pw: str, sql: str) -> bytes:
    """The psql input: logging off for this session, the password as a psql variable, then setup.sql without its
    in-cluster \\getenv line (there is no REAPER_PASSWORD in the postgres container's environment)."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("\\getenv pw"))
    if ":'pw'" not in body:
        sys.exit("relay-operator-reaper-setup: setup.sql no longer sets the password from :'pw'; refusing")
    return ("SET log_statement = 'none';\nSET log_min_error_statement = 'panic';\nSET log_min_duration_statement = -1;\n"
            "\\set pw '" + pw + "'\n" + body + "\n").encode()


TOKENISH = re.compile(r"[A-Za-z0-9_-]{16,}")


def safe_errors(stderr: bytes, pw: str) -> str:
    """psql can echo the failing statement (with :'pw' interpolated), and its `LINE n:` display may TRUNCATE it, so an
    exact replace is not enough: drop every LINE/context line, redact the password and any token-like run of 16+
    characters from what is left."""
    keep = []
    for line in stderr.decode(errors="replace").replace(pw, "<redacted>").splitlines():
        if line.lstrip().startswith(("LINE ", "^", "QUERY:", "CONTEXT:", "STATEMENT:")):
            continue
        keep.append(TOKENISH.sub("<redacted>", line.strip()))
    return " | ".join(keep[-3:])


def main(run=subprocess.run) -> int:
    sql = SETUP.read_text(encoding="utf-8")
    try:
        pw = secret_password(run)
        p = run(CTX + PSQL, input=script(pw, sql), capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:  # a hung exec, a missing kubectl: one line, no traceback
        print(f"relay-operator-reaper-setup: FAILED: {type(e).__name__} (kubectl unreachable or timed out)",
              file=sys.stderr)
        return 1
    out = [line for line in p.stdout.decode(errors="replace").splitlines() if line.startswith("reaper objects ready")]
    if p.returncode != 0 or out != ["reaper objects ready: 1"]:
        print("relay-operator-reaper-setup: FAILED: " + safe_errors(p.stderr, pw), file=sys.stderr)
        return 1
    print("relay-operator-reaper-setup: " + out[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
