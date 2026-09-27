#!/usr/bin/env python3
"""Mock-server tests for scripts/airlock-recycle-sandbox.sh.

    python scripts/tests/airlock-recycle-mock.py

Starts a stdlib HTTP server that plays airlock's app-lifecycle API under a chosen scenario, runs the
script against it (token via --token-fd, http allowed via AIRLOCK_RECYCLE_ALLOW_HTTP=1, kube verify
skipped) and asserts the exit code, the phases reached, which endpoints were written, and that the
token only ever arrived in an Authorization header (never in a path, query string or body).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "airlock-recycle-sandbox.sh"
# On Windows `bash` on PATH may be WSL's (cannot see C:/ paths); prefer Git Bash when present.
# (usr/bin/bash.exe, not the bin/bash.exe launcher wrapper, which does not pass extra fds through)
BASH = next((b for b in (r"C:/Program Files/Git/usr/bin/bash.exe", r"C:/Program Files/Git/bin/bash.exe")
             if os.path.exists(b)), "bash")
# long enough that, echoed after ~290 chars of padding, it straddles the script's 300-char cut
TOKEN = "tok-SECRET-" + "9f3e" * 40
TENANT = "dbe45925-7400-4157-ba89-2968bbe018b3"
API = "/api/airlock/v1"


class State:
    def __init__(self, scenario):
        self.scenario = scenario
        # post-teardown state for the resume scenarios: the sandbox is gone, the App row stays ACTIVE
        self.sandbox = None if scenario.startswith("resume-") else "RUNNING"
        self.ops = {}
        self.calls = []          # (method, path, has_auth)
        self.token_leak = False
        self.auth_calls = 0


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _record(self):
            auth = self.headers.get("Authorization", "")
            if TOKEN in self.path or TOKEN in self.requestline:
                state.token_leak = True
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if TOKEN.encode() in body:
                state.token_leak = True
            state.calls.append((self.command, self.path, auth == f"Bearer {TOKEN}"))
            return auth == f"Bearer {TOKEN}"

        def _app(self, app_id):
            tenant = "11111111-2222-3333-4444-555555555555" if state.scenario == "wrong-tenant" else TENANT
            sb = None if state.sandbox is None else {"status": state.sandbox, "url": "https://x"}
            return {"id": "u", "app_id": app_id, "tenant_id": tenant, "status": "ACTIVE",
                    "lifecycle_mode": "forever", "sandbox": sb}

        def do_GET(self):
            ok = self._record()
            if state.scenario == "auth-before" or not ok:
                # echo the header back: the script must never print it un-redacted
                pad = "x" * 290  # pushes the echoed token across the 300-char truncation boundary
                return self._send(401, {"detail": pad + " " + self.headers.get("Authorization", "")})
            if self.path.startswith(f"{API}/apps/"):
                return self._send(200, self._app(self.path.rsplit("/", 1)[1]))
            if self.path.startswith(f"{API}/app-operations/"):
                op = self.path.rsplit("/", 1)[1]
                o = state.ops.get(op)
                if not o:
                    return self._send(404, {"detail": "no such operation"})
                o["polls"] += 1
                if o["kind"] == "teardown":
                    st = "failed" if state.scenario == "teardown-fails" else ("running" if o["polls"] < 2 else "succeeded")
                    if st == "succeeded":
                        state.sandbox = None
                else:
                    st = "running" if (state.scenario == "deploy-timeout" or o["polls"] < 2) else "succeeded"
                    if st == "succeeded":
                        state.sandbox = "RUNNING"
                return self._send(200, {"operation_id": op, "status": st, "phase": "x", "kind": o["kind"],
                                        "error": {"message": "boom"} if st == "failed" else None})
            self._send(404, {})

        def do_POST(self):
            ok = self._record()
            if not ok:
                return self._send(401, {"detail": "not authenticated"})
            if self.path.endswith("/teardown"):
                if state.scenario == "sync-contract":
                    state.sandbox = None
                    return self._send(204)
                op = f"op-td-{len(state.ops)+1}"
                state.ops[op] = {"kind": "teardown", "polls": 0}
                return self._send(202, {"operation_id": op, "status": "queued", "phase": "q", "kind": "teardown"})
            if self.path.endswith("/deploy"):
                if state.scenario == "auth-after":
                    return self._send(401, {"detail": "token expired"})
                if state.scenario == "sync-contract":
                    state.sandbox = "RUNNING"
                    return self._send(200, self._app("x"))
                op = f"op-dp-{len(state.ops)+1}"
                state.ops[op] = {"kind": "deploy", "polls": 0}
                return self._send(202, {"operation_id": op, "status": "queued", "phase": "q", "kind": "deploy"})
            self._send(404, {})

    return H


def run_with_fd(scenario, extra=()):
    """Windows-portable variant: feed the token through fd 3 using a temp file redirection."""
    import tempfile
    state = State(scenario)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "AIRLOCK_RECYCLE_ALLOW_HTTP": "1"}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tok") as f:
        f.write(TOKEN + "\n")
        tokfile = f.name
    try:
        script = str(SCRIPT).replace("\\", "/")
        tokf = tokfile.replace("\\", "/")
        args = " ".join(["--app ryan", f"--tenant {TENANT}", f"--base-url http://127.0.0.1:{port}", "--token-fd 3",
                         "--skip-kube-verify", "--poll-timeout 6", "--interval 1", *extra])
        p = subprocess.run([BASH, "-c", f'exec 3<"{tokf}"; "{BASH}" "{script}" {args}'], capture_output=True, text=True, env=env)
    finally:
        os.unlink(tokfile)
    srv.shutdown()
    return p, state


CASES = [
    # scenario, extra args, expected exit, phases that must appear, writes that must NOT happen
    ("happy-202", (), 0, ["preflight", "teardown", "deploy"], []),
    ("sync-contract", (), 0, ["preflight", "teardown", "deploy"], []),
    ("wrong-tenant", (), 3, ["preflight"], ["/teardown", "/deploy"]),
    ("auth-before", (), 3, ["preflight"], ["/teardown", "/deploy"]),
    ("auth-after", (), 5, ["preflight", "teardown", "deploy"], []),
    ("teardown-fails", (), 4, ["preflight", "teardown"], ["/deploy"]),
    ("deploy-timeout", (), 5, ["preflight", "teardown", "deploy"], []),
    ("resume-after-teardown", ("--resume-deploy",), 0, ["preflight", "teardown-skipped", "deploy"], ["/teardown"]),
    # resume while the sandbox is still RUNNING must refuse before any write
    ("happy-202", ("--resume-deploy",), 3, ["preflight"], ["/teardown", "/deploy"]),
]


def main() -> int:
    failed = 0
    for scenario, extra, exit_code, phases, forbidden in CASES:
        p, st = run_with_fd(scenario, extra)
        out_phases = [l.split("=", 1)[1] for l in p.stdout.splitlines() if l.startswith("PHASE=")]
        written = [path for (m, path, _) in st.calls if m == "POST"]
        problems = []
        if p.returncode != exit_code:
            problems.append(f"exit {p.returncode} != {exit_code}")
        for ph in phases:
            if ph not in out_phases:
                problems.append(f"phase {ph} missing (got {out_phases})")
        for fb in forbidden:
            if any(w.endswith(fb) for w in written):
                problems.append(f"forbidden write {fb} happened")
        if st.token_leak:
            problems.append("token appeared outside the Authorization header")
        if any(not ok for (_, _, ok) in st.calls) and scenario not in ("auth-before", "auth-after"):
            problems.append("a request arrived without the bearer")
        if TOKEN in p.stdout or TOKEN in p.stderr or TOKEN[:24] in p.stdout or TOKEN[:24] in p.stderr:
            problems.append("token (or a prefix of it) printed by the script")
        label = f"{scenario} {' '.join(extra)}".strip()
        if problems:
            failed += 1
            print(f"FAIL  {label}: {'; '.join(problems)}\n      stderr: {p.stderr.strip()[-300:]}")
        else:
            print(f"PASS  {label} (exit {p.returncode}, phases {out_phases})")
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
