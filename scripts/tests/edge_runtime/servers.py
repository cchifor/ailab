"""Backends for the edge runtime fixture (scripts/tests/edge_runtime.py), run inside python:alpine.

    python servers.py echo        # :8080 HTTP  - answers every request with what it received, as JSON
    python servers.py echo-tls    # :8443 HTTPS - the same, plus the SNI the proxy sent (/certs/echo.*)
    python servers.py auth        # :9091 HTTP  - a stand-in for Authelia's forward-auth endpoint

The echo body is {"method", "target", "sni", "headers": [[name, value], ...]} so the harness can
assert exactly which headers (and which TLS server name) reached a backend; X-Test-Backend names the
upstream the proxy chose.

The auth stand-in answers by the request's `X-Test-Auth` header:
  allow -> 200 with Remote-* identity headers. Remote-Name carries what the auth service itself
           received (CF-Connecting-IP, X-Forwarded-For/Proto/Host), so the harness can assert the
           proxy's headers TOWARD Authelia through the backend's echo.
  deny  -> 403, as Authelia answers an authenticated user outside the rule's group.
  else  -> 302 to the portal, as Authelia answers an anonymous browser.
"""
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SNI = {}


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        sni = SNI.get(id(self.connection))
        body = json.dumps({"method": self.command, "target": self.path, "sni": sni,
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
        verdict = self.headers.get("X-Test-Auth")
        if verdict == "allow":
            seen = {k.lower(): v for k, v in self.headers.items()}
            self.send_response(200)
            self.send_header("Remote-User", "alice")
            self.send_header("Remote-Groups", "admins")
            self.send_header("Remote-Email", "alice@example.test")
            self.send_header("Remote-Name", "cf={};xff={};proto={};host={}".format(
                seen.get("cf-connecting-ip", ""), seen.get("x-forwarded-for", ""),
                seen.get("x-forwarded-proto", ""), seen.get("x-forwarded-host", "")))
            # The proxy must not copy anything else from the auth answer.
            self.send_header("Remote-Extra", "must-not-be-copied")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if verdict == "deny":
            body = b"forbidden by the auth stand-in"
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", "https://sso.chifor.me/?rd=anonymous")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _answer

    def log_message(self, *args):
        pass


class TLSServer(ThreadingHTTPServer):
    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain("/certs/echo.crt", "/certs/echo.key")

        def remember(sslsock, name, _ctx):
            SNI[id(sslsock)] = name
        ctx.sni_callback = remember
        self.socket = ctx.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def get_request(self):
        sock, addr = self.socket.accept()
        sock.do_handshake()
        # the handler sees the same SSLSocket object the callback recorded
        return sock, addr


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "echo":
        ThreadingHTTPServer(("0.0.0.0", 8080), Echo).serve_forever()
    elif kind == "echo-tls":
        TLSServer(("0.0.0.0", 8443), Echo).serve_forever()
    else:
        ThreadingHTTPServer(("0.0.0.0", 9091), Auth).serve_forever()
