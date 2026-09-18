#!/usr/bin/env python3
"""Identity and usage probe for ONE Claude seat (ansible/roles/pr_reviewer -> /usr/local/lib/reviewbot/).

reviewbot runs this AS the seat user (`sudo -n -u <user> HOME=<home> claude-usage.py`), so the
service user never holds a seat's token: the same isolation argument _run_llm makes for the
model run itself. It reads that HOME's credential, calls the two endpoints the CLI's own
`/usage` screen is built from, and prints ONE JSON document on stdout. It exits 0 always -
the failure, if any, is IN the document, so a caller parses one shape and never has to read
stderr.

Measured on reviewer-1, 2026-09-18:

    GET https://api.anthropic.com/api/oauth/profile   -> account.uuid / email, organization.rate_limit_tier
    GET https://api.anthropic.com/api/oauth/usage     -> limits[] of {kind, percent, resets_at, scope.model.display_name, is_active}

Neither call consumes quota. The document:

    {"ok": true, "error": "",
     "account": {"uuid": "...", "email": "...", "plan": "default_claude_max_20x"},
     "limits": [{"kind": "weekly_all", "model": "", "percent": 100.0, "resets_at": 1789847999, "active": true},
                {"kind": "weekly_scoped", "model": "Fable", "percent": 100.0, "resets_at": 1789847999, "active": false}]}

`ok` is "the usage call answered"; `account` is filled independently when the profile call
answered - each endpoint is guarded on its own, so a token with one scope but not the other
still yields whatever it can. `resets_at` is an epoch or null. Unknown `kind`s pass through
untouched - the caller decides.

    "credential": {"source": "login" | "token" | "none", "expires_at": 1789000000 | null}

names WHICH credential was used and, for a browser login, when its access token expires (the
CLI stores `expiresAt` in epoch milliseconds; epoch seconds here). It is filled whatever the
API answered: reviewbot's keepalive decides from it whether a 401 is an expired login - which
one CLI run as the seat renews - or something a re-login must fix. A setup-token has no
expiry and no refresh, so it is never "expired" here.

THE DOCUMENT NEVER CARRIES THE CREDENTIAL. A token is validated before it is used (whitespace
inside it would make http.client raise `ValueError: Invalid header value b'Bearer <token>'` -
an exception whose text IS the secret), and exception text reaches the document only for the
classes whose messages describe the network or the body, never a header. Stdlib only: the
reviewer VMs carry no pip packages.
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.anthropic.com/api/oauth"
HEADERS = {"anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
           "User-Agent": "reviewbot-usage/1"}
TIMEOUT_S = 15


class CredentialError(ValueError):
    """A credential that exists but cannot be used. Its message never contains the value."""


def _clean(tok):
    tok = (tok or "").strip()
    if not tok:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in tok):
        raise CredentialError("credential malformed: whitespace or control characters inside "
                              "the token")
    return tok


def read_credential(home):
    """The seat's token: the hand-seeded long-lived one first, else a browser login's access
    token. None when neither exists (a seat with no credential is a document, not a crash);
    CredentialError when one exists but is unusable - not UTF-8, not JSON, whitespace inside.
    An EMPTY token file falls through to the login beside it, the same rule reviewbot's
    seat_secrets() applies."""
    return credential(home)[0]


def credential(home):
    """(token, source, expires_at): the token read_credential() returns, which file it came
    from ("token" = oauth-token, "login" = .credentials.json, "none"), and the login's expiry
    as epoch seconds (None for a token, or a login whose expiresAt is missing or unreadable).
    """
    try:
        with open(os.path.join(home, ".claude", "oauth-token"), encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raw = None
    except ValueError:
        raise CredentialError("credential malformed: oauth-token is not UTF-8")
    if raw is not None:
        tok = _clean(raw)
        if tok:
            return tok, "token", None
    try:
        with open(os.path.join(home, ".claude", ".credentials.json"), encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return None, "none", None
    except ValueError:
        raise CredentialError("credential malformed: .credentials.json is not UTF-8 JSON")
    if not isinstance(d, dict):
        raise CredentialError("credential malformed: .credentials.json is not an object")
    o = d.get("claudeAiOauth") if isinstance(d.get("claudeAiOauth"), dict) else {}
    tok = o.get("accessToken")
    tok = _clean(tok) if isinstance(tok, str) else None
    if not tok:
        return None, "none", None
    return tok, "login", login_expiry(o.get("expiresAt"))


def login_expiry(v):
    """The CLI's expiresAt (epoch MILLISECONDS) as epoch seconds; seconds pass through; anything
    that is not a positive number is None - the caller then treats the login as never expired,
    which is the pre-keepalive behaviour."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return int(v / 1000.0) if v > 1e11 else int(v)


def iso_epoch(s):
    """'2026-09-19T19:59:59.670651+00:00' -> 1789847999; null or garbage -> None. A naive
    timestamp is taken as UTC, which is what the API sends."""
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    try:
        return int(d.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def _describe(e):
    """Exception text that can never carry the credential. Transport and decode errors
    describe the network or the body and keep their message; anything else - http.client's
    header validation above all - is reduced to its class name."""
    if isinstance(e, (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError)):
        return f"{type(e).__name__}: {e}"[:200]
    return type(e).__name__


def fetch(url, token):
    """(status, body). An HTTP error is a status like any other; only transport failures raise,
    and probe() turns those into the document too."""
    req = urllib.request.Request(url, headers={**HEADERS, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def probe(home, http=fetch):
    doc = {"ok": False, "error": "", "account": {}, "limits": [],
           "credential": {"source": "none", "expires_at": None}}
    try:
        token, source, expires_at = credential(home)
    except CredentialError as e:
        doc["error"] = str(e)
        return doc
    except Exception as e:
        doc["error"] = "credential unreadable: " + _describe(e)
        return doc
    doc["credential"] = {"source": source, "expires_at": expires_at}
    if not token:
        doc["error"] = f"no credential under {home}/.claude (oauth-token or .credentials.json)"
        return doc
    errors = []
    # Each endpoint guarded on its own, so a profile that fails - transport, an HTML body, a
    # missing scope - can never skip the usage call the parks depend on.
    try:
        st, body = http(API + "/profile", token)
        if st == 200:
            p = json.loads(body)
            a, o = p.get("account") or {}, p.get("organization") or {}
            doc["account"] = {"uuid": str(a.get("uuid") or ""), "email": str(a.get("email") or ""),
                              "plan": str(o.get("rate_limit_tier") or "")}
        else:
            errors.append(f"profile: HTTP {st}")
    except Exception as e:
        errors.append("profile: " + _describe(e))
        doc["account"] = {}
    try:
        st, body = http(API + "/usage", token)
        if st == 200:
            limits = []
            for lim in (json.loads(body).get("limits") or []):
                if not isinstance(lim, dict):
                    continue
                model = (((lim.get("scope") or {}).get("model") or {}).get("display_name")) or ""
                try:
                    pct = float(lim.get("percent") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                limits.append({"kind": str(lim.get("kind") or ""), "model": str(model),
                               "percent": pct, "resets_at": iso_epoch(lim.get("resets_at")),
                               "active": bool(lim.get("is_active"))})
            doc["limits"] = limits
            doc["ok"] = True
        else:
            errors.append(f"usage: HTTP {st}")
    except Exception as e:
        errors.append("usage: " + _describe(e))
        doc["ok"], doc["limits"] = False, []
    doc["error"] = "; ".join(errors)
    return doc


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    try:
        doc = probe(home)
    except Exception as e:  # the contract is one document and exit 0, whatever happened
        doc = {"ok": False, "error": "probe crashed: " + _describe(e), "account": {}, "limits": [],
               "credential": {"source": "none", "expires_at": None}}
    print(json.dumps(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
