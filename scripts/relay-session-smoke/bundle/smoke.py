"""The read-only live smoke for `relay-operator.py session` (plans/2026-11-27-relay-operator-session-plan.md, step 3).

Runs as the Runner (--network none) and talks only to the proxy's Unix socket. Prints status codes and whether the
PROXY refused a call, with the proxy's fixed refusal reason; never any other response body (inventories are tenant-wide).
Exit 0 only when every check passes.
"""
import http.client
import json
import os
import socket
import sys
import uuid

SOCK = os.environ["RELAY_SESSION_SOCKET"]
CONTROL_SOCK = os.environ["RELAY_SESSION_CONTROL_SOCKET"]
PREDECESSOR = "45724087-335a-4c55-ad0f-cb278afe608a"


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("proxy", timeout=40)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(40)
        self.sock.connect(self.path)


def call(method, path, body=None, sock=SOCK):
    """(status, the proxy's refusal reason or None). Only a proxy refusal's fixed `reason` word is ever read."""
    c = UnixHTTP(sock)
    data = None if body is None else json.dumps(body).encode()
    c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    raw = r.read()
    c.close()
    if b"refused by relay-session-proxy" not in raw:
        return r.status, None
    try:
        reason = json.loads(raw).get("reason")
    except ValueError:
        reason = "unparsed"
    return r.status, reason if isinstance(reason, str) else "unparsed"


def main():
    checks = {}
    st, why = call("GET", "/api/agent-profiles")
    checks["profiles 200"] = st == 200 and why is None
    st, why = call("GET", "/api/router-credentials")
    checks["router-credentials 200"] = st == 200 and why is None
    st, why = call("GET", "/api/auth/me")
    checks["auth/me refused locally"] = st == 403 and why is not None
    st, why = call("GET", f"/api/agent-sessions/{uuid.uuid4()}")
    checks["unlearned session refused locally"] = st == 403 and why is not None
    # The predecessor rule itself: a control-lease mutation on the CONTROL socket (the general socket would refuse it
    # earlier, for the wrong reason), and a general-socket predecessor mutation.
    path = f"/api/agent-sessions/{PREDECESSOR}/control"
    st, why = call("POST", path, {"generation": 1}, sock=CONTROL_SOCK)
    checks["predecessor control refused: predecessor-read-only"] = st == 403 and why == "predecessor-read-only"
    st, why = call("POST", path, {"generation": 1})
    checks["control route on the general socket refused"] = st == 403 and why == "use-the-control-socket"
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
