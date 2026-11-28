"""Provision the read-only Relay viewer login that dev-worker-4 uses for Playwright checks of production
(docs/runbooks/keycloak.md, "The viewer automation account").

  user              create Keycloak user relay-viewer@ailab.invalid in realm relay (refuses if it exists)
  member            bind its exact subject in Relay with role viewer (member.js)
  shim FILE         run BY `sops edit` as $EDITOR on devworker-seeds.sops.yaml: generate a (new) password, set it in
                    Keycloak over stdin, and write relay_viewer_user/relay_viewer_password into dev-worker-4.json
                    (only that one line of the decrypted file changes). Fails, so sops saves nothing, if Keycloak
                    refuses the password.
  verify            the seed's field names and lengths (never values), the Keycloak user, the Relay membership, and
                    one password login against the realm's public admin-cli client (password only in a POST body)

Secrets travel only through stdin pipes and the file sops manages. Patterned on scripts/relay-operator.py."""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request

CTX = ["kubectl", "--context", "admin@ai"]
ISSUER = "https://id.chifor.me/realms/relay"
REALM = "relay"
EMAIL = "relay-viewer@ailab.invalid"  # registrationEmailAsUsername: username == email; .invalid is unclaimable
NAME = "relay-viewer (Playwright, read-only)"
SEED = "kubernetes/apps/infrastructure/security/openbao/devworker-seeds.sops.yaml"
SCOPE = "dev-worker-4.json"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KCADM = (
    'K=/opt/keycloak/bin/kcadm.sh; C=/tmp/kcadm-$$.cfg; trap "rm -f $C" EXIT; '
    '$K config credentials --config $C --server http://localhost:8080 --realm master '
    '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null && '
    '$K "$@" --config $C'
)


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
    uid = need_uid()
    text = open(path, encoding="utf-8").read()
    seed_line(text, SCOPE)  # refuse before touching Keycloak if the file is not as expected
    pw = secrets.token_urlsafe(32)
    kc("update", f"users/{uid}/reset-password", "-r", REALM, "-f", "-",
       stdin=json.dumps({"type": "password", "value": pw, "temporary": False}).encode())
    text = edit_seed(text, SCOPE, {"relay_viewer_user": EMAIL, "relay_viewer_password": pw})
    open(path, "w", encoding="utf-8", newline="\n").write(text)
    pw = text = None
    print("relay-viewer: password set in Keycloak and written to the seed (not printed)", file=sys.stderr)


def verify() -> None:
    uid = need_uid()
    _, seed, _ = seed_line(run(["sops", "-d", SEED]).decode("utf-8"), SCOPE)
    pw = seed.get("relay_viewer_password", "")
    print(json.dumps({"seedFields": sorted(seed), "relay_viewer_user": seed.get("relay_viewer_user"),
                      "passwordLength": len(pw)}))
    u = kc("get", f"users/{uid}", "-r", REALM, "--fields", "id,username,emailVerified,enabled,requiredActions")
    print(u.decode().strip().replace("\n", " "))
    roles = kc("get", f"users/{uid}/role-mappings/realm", "-r", REALM, "--fields", "name").decode()
    print("realm roles:", sorted(set(re.findall(r'"name" : "([^"]+)"', roles))))
    sql = f"SELECT role FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.subject = '{ISSUER}|{uid}'"
    try:
        print("relay membership:", run(CTX + ["-n", "relay", "exec", "relay-postgres-0", "-c", "postgres", "--", "psql",
                                             "-X", "-U", "postgres", "-d", "relay", "-At", "-c", sql]).decode().strip())
    except SystemExit as e:
        print("relay membership: query failed (", str(e)[-160:], ")")
    body = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli", "username": EMAIL,
                                   "password": pw, "scope": "openid"}).encode()
    pw = None
    req = urllib.request.Request(ISSUER + "/protocol/openid-connect/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "ailab-operator"})
    body = None
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read())
            print("password login:", r.status, "token_type", tok.get("token_type"), "(token discarded)")
    except urllib.error.HTTPError as e:
        print("password login FAILED:", e.code, e.read()[:200].decode("utf-8", "replace"))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "shim":
        shim(sys.argv[-1])  # sops appends the decrypted file's path as the last argument
    elif cmd in ("user", "member", "verify"):
        {"user": user, "member": member, "verify": verify}[cmd]()
    else:
        raise SystemExit("usage: relay-viewer-provision.py user|member|verify|shim FILE")
