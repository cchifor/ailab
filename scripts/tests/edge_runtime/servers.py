"""Backends for the edge runtime fixture (scripts/tests/edge_runtime.py), run inside python:alpine.

    python servers.py echo      # :8080 - answers every request with what it received, as JSON
    python servers.py auth      # :9091 - a stand-in for Authelia's forward-auth endpoint

The echo body is {"method", "target", "headers": [[name, value], ...]} so the harness can assert
exactly which headers reached a backend (X-Test-Backend names the backend the proxy chose).
The auth stand-in allows a request carrying `X-Test-Auth: allow` and answers it with Remote-*
identity headers; anything else gets Authelia's anonymous answer, a 302 to the portal.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({"method": self.command, "target": self.path,
                           "headers": [[k, v] for k, v in self.headers.items()]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _answer

    def log_message(self, *args):
        pass


class Auth(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self):
        if self.headers.get("X-Test-Auth") == "allow":
            self.send_response(200)
            self.send_header("Remote-User", "alice")
            self.send_header("Remote-Groups", "admins")
            self.send_header("Remote-Email", "alice@example.test")
            self.send_header("Remote-Name", "Alice")
            # The proxy must not copy anything else from the auth answer.
            self.send_header("Remote-Extra", "must-not-be-copied")
            seen = {k.lower(): v for k, v in self.headers.items()}
            self.send_header("X-Auth-Saw-Proto", seen.get("x-forwarded-proto", ""))
            self.send_header("X-Auth-Saw-Host", seen.get("x-forwarded-host", ""))
            self.send_header("X-Auth-Saw-For", seen.get("x-forwarded-for", ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "https://sso.chifor.me/?rd=anonymous")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    kind = sys.argv[1]
    handler, port = (Echo, 8080) if kind == "echo" else (Auth, 9091)
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()
