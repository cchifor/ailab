"""Synthetic-only launcher: exact proxy main, replacing login and TLS transport only.

Run in a network-none container with a private /run/relay-session tmpfs. No
credentials are read: the cookie is a fixed fixture token, not a Relay session.
The original source snapshot is raw Git bytes from the named operator commit.
"""
import hashlib
import importlib.util
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SOURCE = Path(__file__).with_name("fixtures") / "relay-session-proxy-c8141ade.py"
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == (
    "012d1acc7639751989493fbf90d16c4e6361ea919656f683fb7ab970b369e114"
)
spec = importlib.util.spec_from_file_location("qualified_proxy", SOURCE)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
ACTOR = "11111111-1111-4111-8111-111111111111"
COOKIE = "synthetic-proxy-session-only"
upstream_socket = sys.argv[1]
assert Path(upstream_socket).is_absolute()


def synthetic_login(username, password):
    assert username == "synthetic-proxy-operator"
    assert password == "synthetic-no-credential"
    return (None, ACTOR, datetime.now(timezone.utc).replace(microsecond=0),
            {"relay_session": COOKIE})


def synthetic_open(self, end, holder, expired):
    # Only the network connection is synthetic. The actual call() retains its
    # deadline, request serialization, cookies, Host/Origin and response parsing.
    assert end > time.monotonic() and not expired.is_set()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    holder["sock"] = sock
    sock.settimeout(end - time.monotonic())
    sock.connect(upstream_socket)
    return sock


# No State, planner, learning, Server, Handler, Upstream.call or logout overrides.
proxy.login = synthetic_login
proxy.RESOLVER.start = lambda: None  # No DNS for the network-none Unix fixture.
proxy.Upstream._open = synthetic_open
sys.exit(proxy.main())
