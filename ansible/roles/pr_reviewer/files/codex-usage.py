#!/usr/bin/env python3
"""Identity and usage probe for ONE Codex seat (ansible/roles/pr_reviewer -> /usr/local/lib/reviewbot/).

reviewbot runs this AS the seat user (`sudo -n -u <user> HOME=<home> codex-usage.py`) and reads
ONE JSON document from stdout - the SAME document claude-usage.py prints, so the usage watchdog
is persona-blind. It exits 0 always; the failure, if any, is IN the document.

    {"ok": true, "error": "",
     "account": {"uuid": "<chatgpt account id>", "email": "...", "plan": "pro"},
     "limits": [{"kind": "weekly_all", "model": "", "percent": 97.0, "resets_at": 1790143563,
                 "active": false}],
     "credential": {"source": "chatgpt" | "apikey" | "none", "expires_at": 1789900000 | null}}

Measured on reviewer-2, 2026-09-19 (codex-cli 0.153.4). The usage comes from the CLI's own app
server: `codex app-server`, JSON-RPC 2.0 over stdio, one object per line, initialize ->
initialized -> account/rateLimits/read, which answers

    {"rateLimits": {"limitId": "codex",
                    "primary": {"usedPercent": 97, "windowDurationMins": 10080, "resetsAt": 1790143563},
                    "secondary": null, "planType": "pro", "rateLimitReachedType": null, ...},
     "accountId": "cfdea639-..."}

No model call, no quota consumed, and the app server refreshes the ChatGPT token itself (so
reviewbot's keepalive, which exists for claude's browser logins, has nothing to do here). The
backend URL behind it (chatgpt.com/backend-api/codex/usage) answers a script with a 403
challenge page, so the CLI is the only door. A window shorter than a day is `session` (the 5h
one), any other is `weekly_all` (10080 min); `active` is a spent window. Notifications the
server emits before its answer (configWarning, remoteControl/status/changed) are skipped.

Identity comes from the seat's own ~/.codex/auth.json: the access token is a JWT whose claims
carry the email, the plan and the account id - READ, NEVER VERIFIED, and never printed. The
document carries no token, and an exception's text reaches it only for the classes whose
messages describe the process or the body (RpcError, transport errors), never a header.
Stdlib only: the reviewer VMs carry no pip packages.

    REVIEWBOT_CODEX_BIN            the CLI (default "codex"); a JSON list for a command with args
    REVIEWBOT_CODEX_RPC_TIMEOUT_S  seconds to wait for the app server's answers (default 20)

DEADLINES. reviewbot runs this under its own subprocess timeout (probe_usage, 45 s); the RPC
deadline here is 20 s so that on a hung app server THIS process answers with the timeout
document - identity included - before the caller could ever kill it (both personas on
ailab#789). Should the caller kill it anyway, nothing is left behind: the app server exits
the instant its stdin closes (measured on reviewer-2: rc=0, 0.0 s after EOF).
"""
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time

WEEKLY_MIN_MINUTES = 1440          # a window this long or longer is the weekly one
RPC_TIMEOUT_S = 20.0               # well under reviewbot's 45 s probe timeout - see DEADLINES


class CredentialError(ValueError):
    """A credential that exists but cannot be used. Its message never contains the value."""


class RpcError(RuntimeError):
    """The app server did not answer, or answered with an error. Text is ours or the server's
    error message, never a token."""


def _bin():
    v = os.environ.get("REVIEWBOT_CODEX_BIN") or "codex"
    if v.startswith("["):
        try:
            cmd = json.loads(v)
            if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
                return cmd
        except ValueError:
            pass
    return [v]


def _timeout():
    try:
        return max(1.0, float(os.environ.get("REVIEWBOT_CODEX_RPC_TIMEOUT_S") or RPC_TIMEOUT_S))
    except (TypeError, ValueError):
        return RPC_TIMEOUT_S


def _describe(e):
    """Exception text that can never carry the credential: our own errors and transport/decode
    errors keep their message; anything else is reduced to its class name."""
    if isinstance(e, (RpcError, json.JSONDecodeError, TimeoutError, OSError)):
        return f"{type(e).__name__}: {e}"[:200] if not isinstance(e, RpcError) else str(e)[:200]
    return type(e).__name__


def jwt_claims(tok):
    """The payload of a JWT-shaped token, unverified; {} for anything else."""
    try:
        parts = tok.split(".")
        if len(parts) != 3:
            return {}
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        c = json.loads(base64.urlsafe_b64decode(pad).decode("utf-8"))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def credential(home):
    """(source, expires_at, claims) - NEVER the token: the app server reads the credential
    itself, so this process has no use for it in hand, and not returning it makes "never
    printed" structural (reviewer-claude on ailab#789). source: "chatgpt" (a ChatGPT login:
    tokens with an access token), "apikey" (OPENAI_API_KEY only - no ChatGPT account, no
    windows), "none". expires_at is the access token's `exp` claim, or None."""
    try:
        with open(os.path.join(home, ".codex", "auth.json"), encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return "none", None, {}
    except ValueError:
        raise CredentialError("credential malformed: auth.json is not UTF-8 JSON")
    if not isinstance(d, dict):
        raise CredentialError("credential malformed: auth.json is not an object")
    tokens = d.get("tokens") if isinstance(d.get("tokens"), dict) else {}
    access = tokens.get("access_token")
    if isinstance(access, str) and access.strip():
        claims = jwt_claims(access.strip())
        exp = claims.get("exp")
        exp = int(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > 0 else None
        return "chatgpt", exp, claims
    if d.get("OPENAI_API_KEY"):
        return "apikey", None, {}
    return "none", None, {}


def identity(claims):
    """Who the seat is, from the access token's claims; {} when they name nobody."""
    prof = claims.get("https://api.openai.com/profile")
    auth = claims.get("https://api.openai.com/auth")
    prof = prof if isinstance(prof, dict) else {}
    auth = auth if isinstance(auth, dict) else {}
    out = {"uuid": str(auth.get("chatgpt_account_id") or ""),
           "email": str(prof.get("email") or claims.get("email") or ""),
           "plan": str(auth.get("chatgpt_plan_type") or "")}
    return out if (out["uuid"] or out["email"]) else {}


def epoch_seconds(v):
    """An epoch stamp as seconds; milliseconds (> 1e11, the same rule as the claude probe's login
    expiry) are converted, anything else that is not a positive number is None. A millisecond
    stamp taken as seconds would read as a reset in the year 58000 (codex cross-review of
    ailab#789)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return int(v / 1000.0) if v > 1e11 else int(v)


def window_kind(minutes):
    try:
        return "session" if float(minutes) < WEEKLY_MIN_MINUTES else "weekly_all"
    except (TypeError, ValueError):
        return "weekly_all"


def windows(result):
    """(limits, plan, account id) from an account/rateLimits/read result.

    THE SERVER'S VERDICT BEATS ITS ROUNDED PERCENT: usedPercent is an integer, and a seat the
    API has limited can read 99 after rounding, while rateLimitReachedType says so in words.
    reviewbot parks on percent, so when the answer says reached, the fullest window is
    reported at 100 and active (reviewer-claude on ailab#789)."""
    rl = result.get("rateLimits") if isinstance(result, dict) else None
    rl = rl if isinstance(rl, dict) else {}
    limits = []
    for name in ("primary", "secondary"):
        w = rl.get(name)
        if not isinstance(w, dict):
            continue
        try:
            pct = float(w.get("usedPercent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        reset = epoch_seconds(w.get("resetsAt"))
        limits.append({"kind": window_kind(w.get("windowDurationMins")), "model": "", "percent": pct,
                       "resets_at": reset, "active": pct >= 100.0})
    if rl.get("rateLimitReachedType") and limits:
        fullest = max(limits, key=lambda l: l["percent"])
        fullest["percent"] = max(fullest["percent"], 100.0)
        fullest["active"] = True
    plan = rl.get("planType")
    acct = result.get("accountId") if isinstance(result, dict) else None
    return limits, (str(plan) if plan else ""), (str(acct) if acct else "")


def app_server_rate_limits(home):
    """initialize -> initialized -> account/rateLimits/read over the app server's stdio; the
    answer's result. Everything else the server says is skipped; no answer by the deadline,
    an exit, or an error answer is an RpcError."""
    timeout = _timeout()
    deadline = time.monotonic() + timeout
    env = {k: v for k, v in os.environ.items() if k not in ("GITEA_TOKEN",)}
    env["HOME"] = home
    p = subprocess.Popen(_bin() + ["app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=home, env=env)
    lines = queue.Queue()

    def pump():
        for line in iter(p.stdout.readline, ""):
            lines.put(line)
        lines.put(None)
    threading.Thread(target=pump, daemon=True).start()

    def send(m):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()

    def answer(rid):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RpcError(f"app-server: timeout after {timeout:.0f}s waiting for the answer to id {rid}")
            try:
                line = lines.get(timeout=left)
            except queue.Empty:
                raise RpcError(f"app-server: timeout after {timeout:.0f}s waiting for the answer to id {rid}")
            if line is None:
                raise RpcError(f"app-server: exited before answering id {rid}")
            try:
                m = json.loads(line)
            except ValueError:
                continue
            # Only a RESPONSE to our id: the app server also sends REQUESTS to its client, which
            # carry a `method` and an id from its own counter that can collide with ours
            # (reviewer-claude on ailab#789); notifications carry no id at all.
            if not isinstance(m, dict) or m.get("id") != rid or "method" in m:
                continue
            if "result" not in m and "error" not in m:
                continue
            if "error" in m:
                err = m["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                raise RpcError(f"app-server: {str(msg)[:160]}")
            r = m.get("result")
            return r if isinstance(r, dict) else {}

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "reviewbot-usage", "title": "reviewbot", "version": "1"},
                         "capabilities": {}}})
        answer(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        return answer(2)
    except BrokenPipeError:
        raise RpcError("app-server: exited before it could be asked")
    finally:
        try:
            p.kill()
        except Exception:
            pass


def probe(home, rpc=app_server_rate_limits):
    doc = {"ok": False, "error": "", "account": {}, "limits": [],
           "credential": {"source": "none", "expires_at": None}}
    try:
        source, expires_at, claims = credential(home)
    except CredentialError as e:
        doc["error"] = str(e)
        return doc
    except Exception as e:
        doc["error"] = "credential unreadable: " + _describe(e)
        return doc
    doc["credential"] = {"source": source, "expires_at": expires_at}
    # Identity BEFORE the CLI runs: a seat whose app server fails still names its account.
    doc["account"] = identity(claims) if claims else {}
    if source == "none":
        doc["error"] = f"no credential under {home}/.codex/auth.json"
        return doc
    if source != "chatgpt":
        doc["error"] = f"{source} login: no ChatGPT account, so no usage windows"
        return doc
    try:
        result = rpc(home)
    except Exception as e:
        doc["error"] = _describe(e) if isinstance(e, RpcError) else "app-server: " + _describe(e)
        return doc
    limits, plan, acct = windows(result)
    if acct or plan:
        doc["account"] = dict(doc["account"] or {"uuid": "", "email": "", "plan": ""})
        if acct:
            doc["account"]["uuid"] = acct
        if plan:
            doc["account"]["plan"] = plan
    doc["limits"] = limits
    doc["ok"] = True
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
