#!/usr/bin/env python3
"""One allowlisted Relay admin call as the just-in-time automation identity `relay-operator`.

Runs inside a pinned python image (stdlib only), started by scripts/relay-operator.py, which grants
the identity its Relay admin membership just before this runs and deletes the membership and every
session of it right after. The identity's password alone grants nothing (no membership at rest).

    printf '%s' "$PASSWORD" | relay-admin-call.py --username EMAIL OP [ARGS]

OPs (the allowlist; nothing else is reachable):
    me                                  the signed-in actor (name, role)
    list-installations                  GET  /api/infrastructure/installations
    get-installation ID                 GET  /api/infrastructure/installations/ID
    enrollment-token ID [--ttl S]       POST /api/infrastructure/installations/ID/enrollment-token
                                        (new operationId each time; ONLY the raw token goes to stdout)

Flow: Relay /api/auth/oidc -> Keycloak login form (#kc-form-login) -> Relay /api/auth/callback ->
relay_session (memory only) -> /api/auth/me must say role admin -> the OP -> POST /api/auth/logout
(with Origin) -> /api/auth/me must be 401. Redirects are followed by hand so every hop's Location is
checked. A credential failure is never retried (the realm has brute-force protection).
Output: JSON on stdout, with `enrollmentToken`/`token` and `recoveryGeneration` redacted, except
enrollment-token, whose stdout is the raw token only and whose redacted receipt goes to stderr.
"""
from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

UA = "relay-admin-call/1 (ailab operator automation)"
ISSUER = "https://id.chifor.me/realms/relay"
EXPECTED_NAME = "relay-operator (automation)"
REDACT = {"enrollmentToken", "token", "recoveryGeneration"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Fail(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # hand the 3xx back to the caller


class Client:
    def __init__(self, relay: str):
        self.relay = relay.rstrip("/")
        self.origin = urllib.parse.urlsplit(self.relay)._replace(path="", query="", fragment="").geturl()
        self.jar = http.cookiejar.CookieJar()  # memory only, never saved
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def request(self, method: str, url: str, body: bytes | None = None, headers: dict | None = None):
        h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
        if method not in ("GET", "HEAD"):
            h["Origin"] = self.origin
        h.update(headers or {})
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:  # 3xx (no redirect handler) and 4xx/5xx
            return e.code, e.headers, e.read()

    def has_session(self) -> bool:
        return any(c.name == "relay_session" for c in self.jar)

    def api(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode()
        hdr = {"Content-Type": "application/json"} if body is not None else {}
        status, _, raw = self.request(method, self.relay + path, body, hdr)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = {"nonJson": raw[:200].decode("utf-8", "replace")}
        return status, data


class _LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields: dict[str, str] = {}
        self.feedback = ""
        self._in_form = False
        self._in_feedback = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("id") == "kc-form-login":
            self._in_form, self.action = True, html.unescape(a.get("action") or "")
        elif tag == "input" and self._in_form and a.get("name") and a.get("type") == "hidden":
            self.fields[a["name"]] = a.get("value") or ""
        if "kc-feedback" in (a.get("class") or "") or a.get("id") in ("input-error", "kc-feedback"):
            self._in_feedback += 1

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False

    def handle_data(self, data):
        if self._in_feedback and data.strip():
            self.feedback = (self.feedback + " " + data.strip())[:200]


def _loc(headers) -> str:
    return headers.get("Location") or ""


def login(c: Client, username: str, password: str) -> None:
    st, h, _ = c.request("GET", c.relay + "/api/auth/oidc")
    auth = _loc(h)
    if st not in (302, 303) or not auth.startswith(ISSUER + "/protocol/openid-connect/auth?"):
        raise Fail(f"oidc start: {st} -> {auth[:80]!r}")
    st, h, page = c.request("GET", auth)
    if st != 200:
        raise Fail(f"keycloak login page: {st}")
    form = _LoginForm()
    form.feed(page.decode("utf-8", "replace"))
    if not form.action or not form.action.startswith(ISSUER + "/login-actions/authenticate?"):
        raise Fail("keycloak login form not found (#kc-form-login)")
    fields = dict(form.fields, username=username, password=password)
    st, h, page = c.request(
        "POST", form.action, urllib.parse.urlencode(fields).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    cb = _loc(h)
    if st not in (302, 303) or not cb.startswith(c.relay + "/api/auth/callback?"):
        # 200 = the form again (bad credentials, lockout) or a required-action page. Never retry.
        fb = _LoginForm()
        fb.feed(page.decode("utf-8", "replace"))
        what = "required action" if b"login-actions/required-action" in page else "credentials rejected"
        raise Fail(f"keycloak login: {st}, {what}: {fb.feedback or '-'}")
    st, h, _ = c.request("GET", cb)
    done = _loc(h)
    if st not in (302, 303) or "auth_error" in done or not c.has_session():
        m = re.search(r"auth_error=([a-z_]+)", done)
        raise Fail(f"relay callback: {st}, auth_error={m.group(1) if m else '-'}, session={c.has_session()}")
    st, me = c.api("GET", "/api/auth/me")
    if st != 200 or not isinstance(me, dict) or me.get("role") != "admin" or me.get("name") != EXPECTED_NAME:
        raise Fail(f"/api/auth/me: {st} name={me.get('name') if isinstance(me, dict) else '-'!r} "
                   f"role={me.get('role') if isinstance(me, dict) else '-'!r} (want {EXPECTED_NAME!r}, admin)")


def logout(c: Client) -> tuple[str, bool]:
    """(message, closed). Closed = no session was opened, or logout 200 and /api/auth/me 401 afterwards."""
    if not c.has_session():
        return "no session", True
    st, _ = c.api("POST", "/api/auth/logout", {})
    st2, _ = c.api("GET", "/api/auth/me")
    closed = st == 200 and st2 == 401
    return f"logout {st}, /api/auth/me after {st2}" + ("" if closed else " (NOT CLOSED)"), closed


def redact(o):
    if isinstance(o, dict):
        return {k: (f"<redacted{'' if not v else ' present'}>" if k in REDACT else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    return o


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--relay", default="https://relay.chifor.me")
    p.add_argument("--username", required=True)
    sub = p.add_subparsers(dest="op", required=True)
    sub.add_parser("me")
    sub.add_parser("list-installations")
    g = sub.add_parser("get-installation"); g.add_argument("id")
    t = sub.add_parser("enrollment-token"); t.add_argument("id"); t.add_argument("--ttl", type=int, default=900)
    a = p.parse_args()
    if getattr(a, "id", None) is not None and not UUID_RE.match(a.id):
        p.error("ID must be a lowercase UUID")
    if a.op == "enrollment-token" and not 60 <= a.ttl <= 3600:
        p.error("--ttl must be 60..3600")
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        p.error("password expected on stdin")

    c = Client(a.relay)
    rc = 0
    try:
        login(c, a.username, password)
        password = ""
        if a.op == "enrollment-token":
            op_id = str(uuid.uuid4())
            st, out = c.api("POST", f"/api/infrastructure/installations/{a.id}/enrollment-token",
                            {"operationId": op_id, "enrollmentTtlSeconds": a.ttl})
            print(json.dumps({"status": st, "operationId": op_id, "receipt": redact(out)}), file=sys.stderr)
            # The reissue route answers 200 (approve answers 201). A replay returns the stored receipt,
            # whose token is null: only a fresh (replayed=false) receipt carries the token, once.
            tok = out.get("enrollmentToken") if isinstance(out, dict) else None
            if st not in (200, 201) or not tok or out.get("replayed") is not False:
                raise Fail(f"enrollment-token: HTTP {st}, no fresh token (a retry must use a NEW operationId)")
            sys.stdout.write(tok)
            sys.stdout.flush()
        else:
            path = {"me": "/api/auth/me", "list-installations": "/api/infrastructure/installations",
                    "get-installation": f"/api/infrastructure/installations/{getattr(a, 'id', '')}"}[a.op]
            st, out = c.api("GET", path)
            print(json.dumps({"status": st, "body": redact(out)}, indent=1))
            rc = 0 if st == 200 else 1
    except Fail as e:
        print(f"relay-admin-call: {e}", file=sys.stderr)
        rc = 2
    finally:
        msg, closed = logout(c)
        print(f"relay-admin-call: {msg}", file=sys.stderr)
        if not closed:
            rc = rc or 4  # the operator's DB revoke still deletes the session; the caller must know
    return rc


if __name__ == "__main__":
    sys.exit(main())
