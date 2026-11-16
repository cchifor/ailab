#!/usr/bin/env python3
"""The Relay operator automation identity: just-in-time admin, no standing Relay membership.

    python scripts/relay-operator.py provision            # once: Keycloak user + password in OpenBao
    python scripts/relay-operator.py provision --rotate   # new password (Keycloak + OpenBao)
    python scripts/relay-operator.py call OP [ARGS]       # grant admin -> one allowlisted call -> revoke
    python scripts/relay-operator.py revoke               # delete its membership + sessions (idempotent)
    python scripts/relay-operator.py status               # membership/session counts, Keycloak user state

`call` OPs are relay-admin-call.py's allowlist (me, list-installations, get-installation ID,
enrollment-token ID [--ttl S]); its stdout passes through unchanged (enrollment-token: the raw token
only, for piping straight into a Secret). The identity holds Relay admin ONLY between the grant and
the revoke of one `call`; the revoke runs in a finally block and is read back. It must never approve
grants, creation policies or delegations: Relay re-checks the approver's CURRENT admin membership at
every use of those (docs/runbooks/relay.md, "Operator automation identity").

Requires kubectl context admin@ai (cluster admin is the real gate) and docker. Secrets travel only
through stdin pipes: the OpenBao breakglass token, the password. Nothing is written to disk.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

CTX = ["kubectl", "--context", "admin@ai"]
ISSUER = "https://id.chifor.me/realms/relay"
REALM = "relay"
EMAIL = "relay-operator@ailab.invalid"  # registrationEmailAsUsername: username == email; .invalid is unclaimable
NAME = "relay-operator (automation)"
BAO_PATH = "relay/operator-oidc"  # mount af
IMAGE = "python:3.13-bookworm@sha256:073ffebb96ae4d0ed73ccad59f08c47bd84af8a79f5258130f8458ac50ad9ff4"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KCADM = (
    'K=/opt/keycloak/bin/kcadm.sh; C=/tmp/kcadm-$$.cfg; trap "rm -f $C" EXIT; '
    '$K config credentials --config $C --server http://localhost:8080 --realm master '
    '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null && '
    '$K "$@" --config $C'
)
BAO = 'read -r t; export BAO_TOKEN="$t" BAO_TLS_SERVER_NAME=openbao.openbao.svc; '


def run(args, stdin: bytes | None = None, check=True) -> bytes:
    p = subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise SystemExit(f"relay-operator: {' '.join(args[:6])} ... failed ({p.returncode}): "
                         f"{p.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return p.stdout


def breakglass() -> bytes:
    raw = run(CTX + ["-n", "openbao", "get", "secret", "openbao-breakglass-token", "-o", "jsonpath={.data.root_token}"])
    return base64.b64decode(raw).strip()


def bao(script: str, payload: bytes = b"") -> bytes:
    return run(CTX + ["-n", "openbao", "exec", "-i", "openbao-0", "--", "sh", "-c", BAO + script],
               breakglass() + b"\n" + payload)


def kc(*args, stdin: bytes | None = None) -> bytes:
    return run(CTX + ["-n", "keycloak", "exec", "-i", "deploy/keycloak", "-c", "keycloak", "--",
                      "sh", "-c", KCADM, "kc", *args], stdin)


def psql(sql: str, **params: str) -> str:
    """SQL on stdin; values only as psql variables (`:'name'` in the SQL), never interpolated into it."""
    var = [a for k, v in params.items() for a in ("-v", f"{k}={v}")]
    return run(CTX + ["-n", "relay", "exec", "-i", "relay-postgres-0", "-c", "postgres", "--",
                      "psql", "-U", "postgres", "-d", "relay", "-v", "ON_ERROR_STOP=1", *var, "-tA"],
               sql.encode()).decode().strip()


def creds() -> dict:
    d = json.loads(bao(f"bao kv get -mount=af -format=json {BAO_PATH}"))["data"]["data"]
    if not re.fullmatch(re.escape(ISSUER) + r"\|" + UUID, d.get("subject", "")):
        raise SystemExit("relay-operator: stored subject is malformed")
    return d


def kc_user_id() -> str | None:
    out = kc("get", "users", "-r", REALM, "-q", f"username={EMAIL}", "-q", "exact=true", "--fields", "id")
    ids = re.findall(UUID, out.decode())
    if len(ids) > 1:
        raise SystemExit("relay-operator: more than one Keycloak user matches")
    return ids[0] if ids else None


def provision(rotate: bool) -> None:
    uid = kc_user_id()
    if rotate and not uid:
        raise SystemExit("relay-operator: --rotate, but no Keycloak user exists")
    if not rotate and uid:
        raise SystemExit("relay-operator: the Keycloak user already exists; use --rotate for a new password")
    if not uid:
        user = {"username": EMAIL, "email": EMAIL, "emailVerified": True, "enabled": True,
                "firstName": "Relay", "lastName": "Operator automation", "requiredActions": []}
        uid = re.findall(UUID, kc("create", "users", "-r", REALM, "-f", "-", "-i",
                                  stdin=json.dumps(user).encode()).decode())[0]
    pw = secrets.token_urlsafe(40)
    kc("update", f"users/{uid}/reset-password", "-r", REALM, "-f", "-",
       stdin=json.dumps({"type": "password", "value": pw, "temporary": False}).encode())
    subject = f"{ISSUER}|{uid}"
    bao(f"bao kv put -mount=af {BAO_PATH} username={EMAIL} subject='{subject}' keycloak_user_id={uid} "
        f"password=- >/dev/null", pw.encode())
    stored = creds()
    want = hashlib.sha256(pw.encode()).hexdigest()[:12]
    got = hashlib.sha256(stored["password"].encode()).hexdigest()[:12]
    pw = stored = None
    u = _json_tail(kc("get", f"users/{uid}", "-r", REALM, "--fields",
                      "id,username,email,emailVerified,enabled,requiredActions"))
    print(json.dumps({"keycloakUser": u, "subject": subject, "openbao": f"af/{BAO_PATH}",
                      "passwordSha256Prefix": {"set": want, "stored": got, "match": want == got}}, indent=1))
    if want != got:
        raise SystemExit(1)


def _json_tail(raw: bytes):
    t = raw.decode()
    return json.loads(t[t.find("{"):])


REVOKED = "0 memberships, 0 sessions"


def counts(subject: str) -> str:
    return psql(
        "SELECT (SELECT count(*) FROM memberships m JOIN users u ON u.id=m.user_id WHERE u.subject=:'subject')"
        "||' memberships, '||(SELECT count(*) FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.subject=:'subject')"
        "||' sessions'", subject=subject)


def revoke(subject: str) -> str:
    psql(
        "BEGIN; DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE subject=:'subject'); "
        "DELETE FROM memberships WHERE user_id IN (SELECT id FROM users WHERE subject=:'subject'); COMMIT;",
        subject=subject)
    return counts(subject)


def grant(subject: str) -> None:
    run(CTX + ["-n", "relay", "exec", "deploy/relay", "-c", "relay", "--", "node", "dist/scripts/member.js",
               "--subject", subject, "--name", NAME, "--role", "admin"])


def end_sso(uid: str) -> None:
    """End the account's Keycloak SSO sessions (admin logout of the user)."""
    kc("create", f"users/{uid}/logout", "-r", REALM)


def cleanup(subject: str, uid: str, rc: int) -> int:
    """Always runs after a grant was ATTEMPTED (member.js may commit and still report a transport error).
    Never raises: a failure is reported loudly and turned into a non-zero rc."""
    try:
        after = revoke(subject)
    except SystemExit as e:
        after = f"revoke failed: {e}"
    print(f"relay-operator: revoked ({after})", file=sys.stderr)
    if after != REVOKED:
        print("relay-operator: REVOKE NOT CONFIRMED - run `revoke` again NOW", file=sys.stderr)
        rc = rc or 3
    try:
        end_sso(uid)
        print("relay-operator: Keycloak SSO sessions ended", file=sys.stderr)
    except SystemExit as e:
        print(f"relay-operator: Keycloak logout failed ({e}); sessions expire at the realm idle timeout", file=sys.stderr)
        rc = rc or 5
    return rc


def call(op_args: list[str]) -> int:
    c = creds()
    subject, username, pw, uid = c["subject"], c["username"], c["password"], c["keycloak_user_id"]
    c = None
    tool = str(Path(__file__).resolve().parent)
    rc = 1
    try:  # the grant is INSIDE the cleanup scope: whatever fails after it starts, the finally revokes
        grant(subject)
        print(f"relay-operator: granted admin ({counts(subject)})", file=sys.stderr)
        p = subprocess.run(["docker", "run", "--rm", "-i", "--network", "bridge", "-v", f"{tool}:/tool:ro", IMAGE,
                            "python3", "-I", "/tool/relay-admin-call.py", "--username", username, *op_args],
                           input=(pw + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pw = None
        sys.stderr.write(p.stderr.decode("utf-8", "replace"))
        sys.stdout.buffer.write(p.stdout)
        sys.stdout.flush()
        rc = p.returncode
    finally:
        rc = cleanup(subject, uid, rc)
    return rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("provision"); pr.add_argument("--rotate", action="store_true")
    ca = sub.add_parser("call"); ca.add_argument("op", nargs=argparse.REMAINDER)
    sub.add_parser("revoke"); sub.add_parser("status")
    a = p.parse_args()
    if a.cmd == "provision":
        provision(a.rotate)
        return 0
    if a.cmd == "call":
        if not a.op:
            p.error("call needs an OP")
        return call(a.op)
    subject = creds()["subject"]
    if a.cmd == "revoke":
        rc = cleanup(subject, subject.rsplit("|", 1)[1], 0)
        print(counts(subject))
        return rc
    uid = subject.rsplit("|", 1)[1]
    u = _json_tail(kc("get", f"users/{uid}", "-r", REALM, "--fields", "id,username,emailVerified,enabled,requiredActions"))
    print(json.dumps({"relay": counts(subject), "keycloakUser": u}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
