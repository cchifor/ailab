"""Upstream stand-in for scripts/tests/relay_internal_tls_runtime.py, run inside python:alpine.

    python echo.py <backend-name>   # :8080 HTTP, answers every request with what it received

The body is {"backend", "method", "target", "headers": [[name, value], ...]}, so the harness can assert
which upstream a terminator chose and exactly which headers reached it.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = sys.argv[1]


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({"backend": BACKEND, "method": self.command, "target": self.path,
                           "headers": [[k, v] for k, v in self.headers.items()]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _answer

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Echo).serve_forever()
