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
answered, so a token with one scope but not the other still yields whatever it can.
`resets_at` is an epoch or null. Unknown `kind`s pass through untouched - the caller decides.
Stdlib only: the reviewer VMs carry no pip packages.
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


def read_credential(home):
    """The seat's token: the hand-seeded long-lived one first, else a browser login's access
    token. None when neither is readable - a seat with no credential is a document, not a
    crash."""
    try:
        with open(os.path.join(home, ".claude", "oauth-token"), encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    try:
        with open(os.path.join(home, ".claude", ".credentials.json"), encoding="utf-8") as f:
            d = json.load(f)
        return ((d.get("claudeAiOauth") or {}).get("accessToken") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


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
    doc = {"ok": False, "error": "", "account": {}, "limits": []}
    token = read_credential(home)
    if not token:
        doc["error"] = f"no credential under {home}/.claude (oauth-token or .credentials.json)"
        return doc
    errors = []
    try:
        st, body = http(API + "/profile", token)
        if st == 200:
            p = json.loads(body)
            a, o = p.get("account") or {}, p.get("organization") or {}
            doc["account"] = {"uuid": str(a.get("uuid") or ""), "email": str(a.get("email") or ""),
                              "plan": str(o.get("rate_limit_tier") or "")}
        else:
            errors.append(f"profile: HTTP {st}")
        st, body = http(API + "/usage", token)
        if st == 200:
            for lim in (json.loads(body).get("limits") or []):
                if not isinstance(lim, dict):
                    continue
                model = (((lim.get("scope") or {}).get("model") or {}).get("display_name")) or ""
                try:
                    pct = float(lim.get("percent") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                doc["limits"].append({"kind": str(lim.get("kind") or ""), "model": str(model),
                                      "percent": pct, "resets_at": iso_epoch(lim.get("resets_at")),
                                      "active": bool(lim.get("is_active"))})
            doc["ok"] = True
        else:
            errors.append(f"usage: HTTP {st}")
    except Exception as e:  # transport, JSON, anything: the document says so, the exit code does not
        errors.append(f"{type(e).__name__}: {e}"[:300])
        doc["ok"] = False
    doc["error"] = "; ".join(errors)
    return doc


def main():
    home = os.environ.get("HOME") or os.path.expanduser("~")
    print(json.dumps(probe(home)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
