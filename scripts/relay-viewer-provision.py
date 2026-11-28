"""Provision the read-only Relay viewer login that dev-worker-4 uses for Playwright checks of production
(docs/runbooks/keycloak.md, "The viewer automation account").

  user              create Keycloak user relay-viewer@ailab.invalid in realm relay (refuses if it exists)
  member            bind its exact subject in Relay with role viewer (member.js)
  shim FILE         run BY `sops edit` as $EDITOR on devworker-seeds.sops.yaml: generate a new password, write
                    relay_viewer_user/relay_viewer_password into dev-worker-4.json (only that one line of the
                    decrypted file changes), THEN set it in Keycloak over stdin. If Keycloak refuses, the shim exits
                    non-zero and sops discards the buffer, so nothing changes anywhere. The one window left: sops
                    failing its own save after Keycloak accepted; `verify` detects it (the login fails), and the
                    fix is to run the rotation again.
  verify            the seed's field names and lengths (never values), the Keycloak user and its realm roles, the
                    Relay membership, and one password login against the realm's public admin-cli client (the
                    password only in a POST body). Exits 1 unless every invariant holds.

Secrets travel only through stdin pipes and the file sops manages. Patterned on scripts/relay-operator.py."""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CTX = ["kubectl", "--context", "admin@ai"]
ISSUER = "https://id.chifor.me/realms/relay"
REALM = "relay"
EMAIL = "relay-viewer@ailab.invalid"  # registrationEmailAsUsername: username == email; .invalid is unclaimable
NAME = "relay-viewer (Playwright, read-only)"
SEED = "kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml"
SCOPE = "dev-worker-4.json"
ALLOWED_REALM_ROLES = {"default-roles-relay"}
MIN_PASSWORD = 12  # the realm's passwordPolicy: length(12) and notUsername and notEmail
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KCADM = (
    'K=/opt/keycloak/bin/kcadm.sh; C=/tmp/kcadm-$$.cfg; trap "rm -f $C" EXIT; '
    '$K config credentials --config $C --server http://localhost:8080 --realm master '
    '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null && '
    '$K "$@" --config $C'
)
USAGE = "usage: relay-viewer-provision.py user | member | verify | shim FILE"


def run(args, stdin: bytes | None = None, timeout: float = 120) -> bytes:
    p = subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode != 0:
        raise SystemExit(f"relay-viewer: {' '.join(args[:6])} ... failed ({p.returncode}): "
                         f"{p.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return p.stdout


def kc(*args, stdin: bytes | None = None) -> bytes:
    return run(CTX + ["-n", "keycloak", "exec", "-i", "deploy/keycloak", "-c", "keycloak", "--",
                      "sh", "-c", KCADM, "kc", *args], stdin)


def kc_user_id() -> str | None:
    out = kc("get", "users", "-r", REALM, "-q", f"username={EMAIL}", "-q", "exact=true", "--fields", "id")
    ids = re.findall(UUID, out.decode())
    if len(ids) > 1:
        raise SystemExit("relay-viewer: more than one Keycloak user matches")
    return ids[0] if ids else None


def need_uid() -> str:
    uid = kc_user_id()
    if not uid:
        raise SystemExit("relay-viewer: no Keycloak user yet; run `user` first")
    return uid


def user() -> None:
    if kc_user_id():
        raise SystemExit("relay-viewer: the Keycloak user already exists")
    u = {"username": EMAIL, "email": EMAIL, "emailVerified": True, "enabled": True,
         "firstName": "Relay", "lastName": "Viewer automation", "requiredActions": []}
    uid = re.findall(UUID, kc("create", "users", "-r", REALM, "-f", "-", "-i", stdin=json.dumps(u).encode()).decode())[0]
    print(json.dumps({"keycloakUserId": uid, "subject": f"{ISSUER}|{uid}"}))


def member() -> None:
    uid = need_uid()
    out = run(CTX + ["-n", "relay", "exec", "deploy/relay", "-c", "relay", "--", "node", "dist/scripts/member.js",
                     "--subject", f"{ISSUER}|{uid}", "--name", NAME, "--role", "viewer"])
    print(out.decode("utf-8", "replace").strip()[-400:])


def seed_line(text: str, scope: str) -> tuple[int, dict, str]:
    """The decrypted seeds file's one `    <scope>: <inline JSON>` line: (its index, the parsed JSON, the quote)."""
    lines = text.split("\n")
    idx = [i for i, line in enumerate(lines) if line.startswith("    " + scope + ": ")]
    if len(idx) != 1:
        raise SystemExit(f"relay-viewer: expected one {scope} line, found {len(idx)}")
    raw = lines[idx[0]][len("    " + scope + ": "):]
    if len(raw) > 1 and raw.startswith("'") and raw.endswith("'"):
        return idx[0], json.loads(raw[1:-1].replace("''", "'")), "'"
    if len(raw) > 1 and raw.startswith('"') and raw.endswith('"'):
        return idx[0], json.loads(json.loads(raw)), '"'
    raise SystemExit("relay-viewer: unexpected scalar style for the seed line")


def edit_seed(text: str, scope: str, fields: dict) -> str:
    """Set `fields` in one scope's JSON; every other line, the other scopes included, stays byte-identical."""
    i, seed, quote = seed_line(text, scope)
    seed.update(fields)
    out = json.dumps(seed)
    lines = text.split("\n")
    lines[i] = "    " + scope + ": " + ("'" + out.replace("'", "''") + "'" if quote == "'" else json.dumps(out))
    return "\n".join(lines)


def shim(path: str) -> None:
    """Write the seed FIRST, then set Keycloak: a Keycloak refusal raises, the editor exits non-zero, and sops
    discards the buffer (`Could not run editor`, exit 201: verified with sops 3.9.4), so nothing changes."""
    uid = need_uid()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    pw = secrets.token_urlsafe(32)
    text = edit_seed(text, SCOPE, {"relay_viewer_user": EMAIL, "relay_viewer_password": pw})
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    text = None
    kc("update", f"users/{uid}/reset-password", "-r", REALM, "-f", "-",
       stdin=json.dumps({"type": "password", "value": pw, "temporary": False}).encode())
    pw = None
    print("relay-viewer: seed written, then the password set in Keycloak (not printed); sops saves on exit",
          file=sys.stderr)


def check(facts: dict) -> list[str]:
    """Every invariant the account must hold; each failure is one line. An empty list means it is healthy."""
    bad = []
    if facts.get("seedUser") != EMAIL:
        bad.append(f"seed relay_viewer_user is {facts.get('seedUser')!r}, not {EMAIL}")
    if facts.get("passwordLength", 0) < MIN_PASSWORD:
        bad.append(f"seed relay_viewer_password is missing or shorter than {MIN_PASSWORD}")
    u = facts.get("keycloakUser") or {}
    if u.get("username") != EMAIL or u.get("enabled") is not True or u.get("emailVerified") is not True:
        bad.append("the Keycloak user is not enabled and verified under the expected username")
    if u.get("requiredActions"):
        bad.append(f"the Keycloak user has required actions {u.get('requiredActions')}")
    roles = facts.get("realmRoles")
    if roles is None or set(roles) != ALLOWED_REALM_ROLES:
        bad.append(f"realm roles are {roles}, expected exactly {sorted(ALLOWED_REALM_ROLES)}")
    if facts.get("membership") != "viewer":
        bad.append(f"the Relay membership is {facts.get('membership')!r}, not 'viewer'")
    if facts.get("login") != 200:
        bad.append(f"the password login answered {facts.get('login')!r}, not 200")
    return bad


def login(pw: str) -> object:
    body = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli", "username": EMAIL,
                                   "password": pw, "scope": "openid"}).encode()
    req = urllib.request.Request(ISSUER + "/protocol/openid-connect/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ailab-operator"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read())  # the token is parsed to prove the answer, then discarded
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        return f"unreachable ({e.reason})"


def verify() -> int:
    uid = need_uid()
    facts: dict = {}
    _, seed, _ = seed_line(run(["sops", "-d", SEED]).decode("utf-8"), SCOPE)
    pw = seed.get("relay_viewer_password", "")
    facts.update(seedFields=sorted(seed), seedUser=seed.get("relay_viewer_user"), passwordLength=len(pw))
    facts["keycloakUser"] = json.loads(kc("get", f"users/{uid}", "-r", REALM, "--fields",
                                          "id,username,emailVerified,enabled,requiredActions").decode())
    facts["realmRoles"] = sorted(r["name"] for r in json.loads(
        kc("get", f"users/{uid}/role-mappings/realm", "-r", REALM).decode() or "[]"))
    sql = f"SELECT m.role FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.subject = '{ISSUER}|{uid}'"
    try:
        rows = run(CTX + ["-n", "relay", "exec", "relay-postgres-0", "-c", "postgres", "--", "psql", "-X", "-U",
                          "postgres", "-d", "relay", "-At", "-c", sql]).decode().split()
        facts["membership"] = rows[0] if len(rows) == 1 else (rows or None)
    except SystemExit as e:
        facts["membership"] = f"query failed: {str(e)[-120:]}"
    facts["login"] = login(pw) if pw else None
    pw = None
    bad = check(facts)
    print(json.dumps({**facts, "ok": not bad, "failures": bad}, indent=1))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit(USAGE)
    cmd = argv[1]
    if cmd == "shim":
        if len(argv) < 3:
            raise SystemExit(USAGE)
        shim(argv[-1])  # sops appends the decrypted file's path as the last argument
        return 0
    if cmd == "user":
        user()
        return 0
    if cmd == "member":
        member()
        return 0
    if cmd == "verify":
        return verify()
    raise SystemExit(USAGE)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
