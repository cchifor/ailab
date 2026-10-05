#!/usr/bin/env python3
"""Ensure the harness service's login role and database exist on the platform's strive-pg cluster.

Owner runbook step 12 (cchifor/platform plans/2026-10-05-platform-modularization-owner-runbook.md):
role `harness` LOGIN NOSUPERUSER NOBYPASSRLS, database `harness` OWNED by it. The chart's own
migration init container then applies services/harness/migrations as `harness`.

Runs in the CNPG operand image (python 3.9 + psycopg2 2.9) as the CNPG superuser, against the
PRIMARY (it refuses a replica). Every step is idempotent, so a re-run is a no-op and the
reap-and-re-apply loop in bootstrap-job.yaml is safe:

  * the role is created if absent and its attributes are re-asserted every run (NOSUPERUSER,
    NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOREPLICATION, LOGIN); any role membership it holds is
    revoked (a membership in `app` or pg_write_all_data would widen it while every attribute still
    looked right);
  * the password is set only when the one in the ESO-rendered Secret does NOT already log in, and
    then as a client-computed SCRAM verifier (libpq's PQencryptPasswordConn), never cleartext:
    every dev-worker's platform login holds pg_monitor, which shows other sessions' query text in
    pg_stat_activity. Statement logging is also off for the session, including the statement text
    a FAILED statement would log (log_min_error_statement);
  * the database is created OWNER harness if absent, and its owner is repaired if it is not;
  * nothing is ever dropped.

THE SECRET MAY NOT EXIST YET. HARNESS_PASSWORD comes from Secret strive-pg-harness through an
`optional: true` secretKeyRef. Until External Secrets has rendered it (OpenBao af/strive/pg-harness,
seeded by security/openbao/strive-pg-harness-provision-job.yaml), the variable is unset and this
exits 0 having changed nothing -- the Job is re-applied after its TTL and converges once the Secret
exists. A Secret that IS present but short is a misconfiguration and fails loudly.

NEVER PRINTS A PASSWORD. Role, database and step names only.
"""
import os
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

ROLE = os.environ.get("HARNESS_ROLE", "harness")
DATABASE = os.environ.get("HARNESS_DATABASE", "harness")
MIN_PASSWORD_LENGTH = 16
ATTRIBUTES = "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT"


def log(msg):
    print("[strive-pg-harness] " + msg, flush=True)


def password_logs_in(password):
    """True when `password` already authenticates as ROLE. Only an authentication refusal (SQLSTATE
    class 28) counts as "no": any other failure is re-raised, so a transport or capacity error
    cannot turn into a password write."""
    try:
        probe = psycopg2.connect(dbname="postgres", user=ROLE, password=password, connect_timeout=10)
    except psycopg2.OperationalError as exc:
        code = getattr(exc, "pgcode", None) or ""
        text = str(exc)
        if code.startswith("28") or "password authentication failed" in text or "is not permitted to log in" in text:
            return False
        raise
    probe.close()
    return True


def main():
    password = os.environ.get("HARNESS_PASSWORD", "")
    if not password:
        log("Secret strive-pg-harness is not rendered yet (OpenBao af/strive/pg-harness absent, or ESO "
            "has not synced); nothing changed. The Job is re-applied after its TTL.")
        return 0
    if len(password) < MIN_PASSWORD_LENGTH:
        log("the password in Secret strive-pg-harness is shorter than %d characters; refusing"
            % MIN_PASSWORD_LENGTH)
        return 1

    conn = psycopg2.connect("")  # PG* environment: the CNPG superuser on the -rw Service
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction block
    cur = conn.cursor()
    cur.execute("SELECT pg_is_in_recovery()")
    if cur.fetchone()[0]:
        log("connected to a replica; refusing (PGHOST must be the -rw Service)")
        return 1
    cur.execute("SET log_statement = 'none'")
    cur.execute("SET log_min_duration_statement = -1")
    # A FAILING statement is logged with its text at log_min_error_statement (default ERROR): a
    # CREATE/ALTER ROLE that errors would put the verifier in the server log. Only PANIC is above it.
    cur.execute("SET log_min_error_statement = 'panic'")

    role = sql.Identifier(ROLE)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
    if cur.fetchone() is None:
        verifier = encrypt_password(password, ROLE, conn, "scram-sha-256")
        cur.execute(sql.SQL("CREATE ROLE {} WITH " + ATTRIBUTES + " PASSWORD %s").format(role), (verifier,))
        log("role %s created" % ROLE)
    else:
        cur.execute(sql.SQL("ALTER ROLE {} WITH " + ATTRIBUTES).format(role))
        if password_logs_in(password):
            log("role %s present; attributes re-asserted, password kept" % ROLE)
        else:
            verifier = encrypt_password(password, ROLE, conn, "scram-sha-256")
            cur.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(role), (verifier,))
            log("role %s present; attributes re-asserted, password set from the Secret" % ROLE)

    cur.execute(
        "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
        "WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = %s) ORDER BY 1",
        (ROLE,),
    )
    for (granted,) in cur.fetchall():
        cur.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(granted), role))
        log("revoked unexpected membership %s from %s" % (granted, ROLE))

    cur.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (DATABASE,))
    row = cur.fetchone()
    if row is None:
        cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(DATABASE), role))
        log("database %s created, owner %s" % (DATABASE, ROLE))
    elif row[0] != ROLE:
        cur.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(DATABASE), role))
        log("database %s owner repaired: %s -> %s" % (DATABASE, row[0], ROLE))
    else:
        log("database %s present, owner %s" % (DATABASE, ROLE))

    cur.close()
    conn.close()
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
