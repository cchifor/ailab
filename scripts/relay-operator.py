#!/usr/bin/env python3
"""The Relay operator automation identity: just-in-time admin, no standing Relay membership.

    python scripts/relay-operator.py provision            # once: Keycloak user + password in OpenBao
    python scripts/relay-operator.py provision --rotate   # new password (Keycloak + OpenBao)
    python scripts/relay-operator.py call OP [ARGS]       # grant admin -> one allowlisted call -> revoke
    python scripts/relay-operator.py revoke               # delete its membership + sessions (idempotent)
    python scripts/relay-operator.py session --manifest-ref COMMIT:PATH --bundle DIR --runner-minutes N
                                     --evidence-dir NEW_DIR [--predecessor-report FILE]
                                     # ONE bounded automated run (relay_operator_session.py); an observer manifest
                                     # (automated phase A) also needs the preserved predecessor report
    python scripts/relay-operator.py observer-probe       # READ-ONLY phase A observer checks + latencies (no authority)
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
import signal
import subprocess
import sys
import time
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


def run(args, stdin: bytes | None = None, check=True, timeout: float = 120) -> bytes:
    """Every subprocess is bounded; a timeout becomes the same SystemExit as a failure, which cleanup() handles."""
    try:
        p = subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"relay-operator: {' '.join(args[:6])} ... timed out after {timeout:.0f}s")
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
        rc = 3  # always wins: a possibly standing admin outranks any other failure
    try:
        end_sso(uid)
        print("relay-operator: Keycloak SSO sessions ended", file=sys.stderr)
    except SystemExit as e:
        print(f"relay-operator: Keycloak logout failed ({e}); sessions expire at the realm idle timeout", file=sys.stderr)
        rc = rc or 5
    return rc


REPO = Path(__file__).resolve().parents[1]


def _session_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import relay_operator_session as ros  # noqa: E402 - the orchestrator lives beside this script
    return ros


def ops():
    ros = _session_module()
    return ros, ros.Ops(
        creds=creds, grant=grant, revoke=revoke, end_sso=end_sso,
        kubectl=lambda args, stdin=None, timeout=60, check=True: run(CTX + list(args), stdin, check, timeout),
        docker=lambda args, stdin=None, timeout=60, check=True: run(["docker", *args], stdin, check, timeout),
        git_show=lambda commit, path: run(["git", "-C", str(REPO), "show", f"{commit}:{path}"], timeout=30),
        git_is_reviewed=lambda commit: subprocess.run(
            ["git", "-C", str(REPO), "merge-base", "--is-ancestor", commit, "gitea/main"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0)


CALL_DOCKER_TIMEOUT_S = 300
# Worst case of everything a call may spend while holding authority: creds, orphan revoke, grant and counts
# (4 x 120 s), the admin container (300 s) plus its stop/remove (75 s), then cleanup (revoke, readback, SSO:
# 3 x 120 s). The Lease must outlive all of it.
CALL_LEASE_S = 1800
CLEANUP_SECURE_S = 600  # the validity re-secured before destructive cleanup (cleanup's worst case is ~360 s)


class _Interrupted(BaseException):
    pass


class _SafeStream:
    """Wraps stderr/stdout during a call: a write or flush failure (a closed pipe) is swallowed, so reporting can
    never abort the cleanup that revokes the temporary admin."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, s):
        try:
            return self.inner.write(s)
        except (OSError, ValueError):
            return len(s)

    def flush(self):
        try:
            self.inner.flush()
        except (OSError, ValueError):
            pass

    @property
    def buffer(self):
        return _SafeStream(self.inner.buffer) if hasattr(self.inner, "buffer") else self

    def __getattr__(self, name):
        return getattr(self.inner, name)


def call(op_args: list[str]) -> int:
    ros, o = ops()
    holder = "call-" + secrets.token_hex(6)
    container = "relay-admin-" + holder
    state = {"cleanup": False, "deferred": 0}

    def on_signal(*_):  # the same lifecycle as `session`: interrupt the work, never the cleanup
        if state["cleanup"]:
            state["deferred"] += 1
            return
        raise _Interrupted()

    prev = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    for s in prev:
        signal.signal(s, on_signal)
    saved_streams = (sys.stdout, sys.stderr)
    sys.stdout, sys.stderr = _SafeStream(sys.stdout), _SafeStream(sys.stderr)
    rc = 1
    try:
        try:  # the same fenced Lease as `session`: a call never overlaps a run, and the reaper waits for it
            ros.lease_acquire(o, holder, time.time() + CALL_LEASE_S)
        except (ros.LeaseLost, SystemExit) as e:
            print(f"relay-operator: {e}; refusing (a session or reaper holds the identity)", file=sys.stderr)
            return 7
        subject = uid = None
        try:  # credentials, the grant and the call are all INSIDE the cleanup + Lease-release scope
            c = creds()
            subject, username, pw, uid = c["subject"], c["username"], c["password"], c["keycloak_user_id"]
            c = None
            tool = str(Path(__file__).resolve().parent)
            orphan = revoke(subject)
            if orphan != REVOKED:
                raise SystemExit(f"orphan authority not cleared ({orphan})")
            grant(subject)
            print(f"relay-operator: granted admin ({counts(subject)})", file=sys.stderr)
            try:
                p = subprocess.run(["docker", "run", "--rm", "-i", "--name", container, "--network", "bridge",
                                    "-v", f"{tool}:/tool:ro", IMAGE, "python3", "-I", "/tool/relay-admin-call.py",
                                    "--username", username, *op_args],
                                   input=(pw + "\n").encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=CALL_DOCKER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"the admin call exceeded {CALL_DOCKER_TIMEOUT_S}s")
            pw = None
            sys.stderr.write(p.stderr.decode("utf-8", "replace"))
            sys.stdout.buffer.write(p.stdout)
            sys.stdout.flush()
            rc = p.returncode
        except BaseException as e:  # noqa: BLE001 - grant/count/docker/signal: report it; cleanup's verdict decides
            print(f"relay-operator: {type(e).__name__}: {e}", file=sys.stderr)
            rc = 1
        finally:
            state["cleanup"] = True
            # 1. the admin container: gone before anything else (a timed-out docker client leaves it running)
            try:
                run(["docker", "rm", "-f", container], check=False, timeout=75)
            except BaseException as e:  # noqa: BLE001
                print(f"relay-operator: could not remove {container} ({type(e).__name__})", file=sys.stderr)
                rc = rc or 1
            # 2. destructive cleanup only while this call still owns the Lease, with enough validity for it
            if subject is not None:
                owned = ros.lease_secure(o, holder, CLEANUP_SECURE_S)
                if owned is True:
                    try:
                        rc = cleanup(subject, uid, rc)
                    except BaseException as e:  # noqa: BLE001 - cleanup() never raises by contract; be sure
                        print(f"relay-operator: cleanup raised {type(e).__name__}; REVOKE NOT CONFIRMED",
                              file=sys.stderr)
                        rc = 3
                else:
                    print("relay-operator: Lease ownership NOT confirmed (" + ("a successor holds it"
                          if owned is False else "unknown") + "); not revoking: run `relay-operator.py revoke` "
                          "once the Lease is free (the reaper also clears it)", file=sys.stderr)
                    rc = rc if rc == 3 else 7
            # 3. the Lease
            try:
                ros.lease_release(o, holder)
            except BaseException as e:  # noqa: BLE001 - the Lease expires on its own; report it
                print(f"relay-operator: Lease release failed ({type(e).__name__}); it expires on its own",
                      file=sys.stderr)
                rc = rc or 7
            if state["deferred"]:
                print(f"relay-operator: {state['deferred']} signal(s) deferred until cleanup finished", file=sys.stderr)
    finally:
        for s, h in prev.items():
            signal.signal(s, h)
        sys.stdout, sys.stderr = saved_streams
    return rc


def session(a) -> int:
    ros, o = ops()
    subprocess.run(["git", "-C", str(REPO), "fetch", "-q", "gitea", "main"], timeout=60, check=False)
    return ros.run_session(o, a.manifest_ref, a.bundle, a.runner_minutes, a.evidence_dir, a.predecessor_report)


def observer_probe() -> int:
    """Read-only: the phase A observer's sources against the live estate, with per-call latency. No Relay session, no
    Lease, no holder start (plans/2026-11-27-relay-phase-a-observer-plan.md, Verification 3)."""
    _session_module()
    import relay_phase_a_host as H  # noqa: E402
    report = H.probe(REPO)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("provision"); pr.add_argument("--rotate", action="store_true")
    ca = sub.add_parser("call"); ca.add_argument("op", nargs=argparse.REMAINDER)
    sub.add_parser("revoke"); sub.add_parser("status")
    se = sub.add_parser("session")
    se.add_argument("--manifest-ref", required=True, help="FULL_COMMIT:path of the reviewed bundle manifest")
    se.add_argument("--bundle", required=True)
    se.add_argument("--runner-minutes", type=int, required=True)
    se.add_argument("--evidence-dir", required=True)
    se.add_argument("--predecessor-report", help="the preserved original phase A report (observer manifests only)")
    sub.add_parser("observer-probe")
    a = p.parse_args()
    if a.cmd == "provision":
        provision(a.rotate)
        return 0
    if a.cmd == "session":
        return session(a)
    if a.cmd == "observer-probe":
        return observer_probe()
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
