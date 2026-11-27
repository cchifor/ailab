"""Synthetic-only launcher: exact proxy main, replacing login and TLS transport only.

Run in a network-none container with a private /run/relay-session tmpfs. No
credentials are read: the cookie is a fixed fixture token, not a Relay session.
The original source snapshot is raw Git bytes from the named operator commit.
"""
import hashlib
import http.client
import importlib.util
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path


SOURCE = Path(__file__).with_name("fixtures") / "relay-session-proxy-2c2deb.py"
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == (
    "611ab65a3590d836feb68dfaf15ae3e063fe47986543283f739d49e983cf0b24"
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


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, host, port, timeout=None, context=None):
        assert host == proxy.HOST and port == 443
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(upstream_socket)


# No State, planner, learning, Server, Handler, Upstream.call or logout overrides.
proxy.login = synthetic_login
proxy.http.client.HTTPSConnection = UnixConnection
sys.exit(proxy.main())
