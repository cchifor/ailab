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
    REVIEWBOT_CODEX_RPC_TIMEOUT_S  seconds to wait for the app server's answers (default 30)
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
        return max(1.0, float(os.environ.get("REVIEWBOT_CODEX_RPC_TIMEOUT_S") or 30))
    except (TypeError, ValueError):
        return 30.0


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
    """(token, source, expires_at, claims). source: "chatgpt" (a ChatGPT login: tokens with an
    access token), "apikey" (OPENAI_API_KEY only - no ChatGPT account, no windows), "none".
    expires_at is the access token's `exp` claim, or None."""
    try:
        with open(os.path.join(home, ".codex", "auth.json"), encoding="utf-8") as f:
            d = json.load(f)
    except OSError:
        return None, "none", None, {}
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
        return access.strip(), "chatgpt", exp, claims
    if d.get("OPENAI_API_KEY"):
        return None, "apikey", None, {}
    return None, "none", None, {}


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


def window_kind(minutes):
    try:
        return "session" if float(minutes) < WEEKLY_MIN_MINUTES else "weekly_all"
    except (TypeError, ValueError):
        return "weekly_all"


def windows(result):
    """(limits, plan, account id) from an account/rateLimits/read result."""
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
        reset = w.get("resetsAt")
        reset = int(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > 0 else None
        limits.append({"kind": window_kind(w.get("windowDurationMins")), "model": "", "percent": pct,
                       "resets_at": reset, "active": pct >= 100.0})
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
            if not isinstance(m, dict) or m.get("id") != rid:
                continue                      # a notification, or someone else's answer
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
        token, source, expires_at, claims = credential(home)
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
