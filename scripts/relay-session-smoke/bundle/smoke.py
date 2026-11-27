"""The read-only live smoke for `relay-operator.py session` (plans/2026-11-27-relay-operator-session-plan.md, step 3).

Runs as the Runner (--network none) and talks only to the proxy's Unix socket. Prints status codes and whether the
PROXY refused a call; never a response body (inventories are tenant-wide). Exit 0 only when every check passes.
"""
import http.client
import json
import os
import socket
import sys
import uuid

SOCK = os.environ["RELAY_SESSION_SOCKET"]
PREDECESSOR = "45724087-335a-4c55-ad0f-cb278afe608a"


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("proxy", timeout=40)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(40)
        self.sock.connect(SOCK)


def call(method, path, body=None):
    c = UnixHTTP()
    data = None if body is None else json.dumps(body).encode()
    c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, b"refused by relay-session-proxy" in raw


def main():
    checks = {}
    st, local = call("GET", "/api/agent-profiles")
    checks["profiles 200"] = st == 200 and not local
    st, local = call("GET", "/api/router-credentials")
    checks["router-credentials 200"] = st == 200 and not local
    st, local = call("GET", "/api/auth/me")
    checks["auth/me refused locally"] = st == 403 and local
    st, local = call("GET", f"/api/agent-sessions/{uuid.uuid4()}")
    checks["unlearned session refused locally"] = st == 403 and local
    st, local = call("POST", f"/api/agent-sessions/{PREDECESSOR}/control", {"generation": 1})
    checks["predecessor mutation refused locally"] = st == 403 and local
    try:
        socket.create_connection(("relay.chifor.me", 443), timeout=5).close()
        checks["no network"] = False
    except OSError:
        checks["no network"] = True
    ok = all(checks.values())
    print(json.dumps({"smoke": "pass" if ok else "fail", "checks": checks}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
