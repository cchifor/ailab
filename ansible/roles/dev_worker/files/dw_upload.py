#!/usr/bin/env python3
"""dw-upload: receives files pasted or dropped into the dev-worker web terminal.

The browser side (files/dw_paste.js) PUTs each file to /_dw/upload?name=<original name>; this stores
it in the pastes directory and answers {"path": ...}, which the page then pastes into the focused
tmux pane so the agent (Claude Code / Codex) can attach or read it.

Only reachable through Caddy's web gate (tasks/web_gate.yml: Access JWT or LAN login, Origin guard);
it listens on loopback and re-checks the Origin and a custom header itself, so a request that
somehow reached it directly still has to look like the worker's own page.

Framing is strict because this is the one endpoint that writes to disk: exactly one Content-Length
(no chunked bodies), at most MAX_BYTES, read under a per-request deadline; a short body leaves
nothing behind. Space is reserved under a lock BEFORE reading, counting in-flight uploads, so
concurrent uploads cannot overshoot the directory quota. Files are written 0600 to a temp name
and published with link(2), which never replaces an existing file.

Stdlib only. docs/runbooks/dev-workers.md § "Pasting images and files into agents".
"""

import errno
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("dw-upload")

MAX_BYTES = 64 * 1024 * 1024  # per file; Caddy's request_body max_size is the same 64MiB
QUOTA_BYTES = 2 * 1024 * 1024 * 1024  # whole pastes directory (tmpfiles ages files out after 14d)
MAX_FILES = 1000
MAX_CONCURRENT = 4
READ_DEADLINE = 300  # seconds for one whole body
SOCKET_TIMEOUT = 30  # seconds per socket read
CHUNK = 256 * 1024
TMP_PREFIX = ".upload-"

EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
}


class Rejected(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def safe_name(raw, content_type):
    """A basename that is safe on disk and in a shell-less paste: [A-Za-z0-9._-], no leading dot."""
    base = os.path.basename((raw or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)  # split BEFORE truncating, so a long name keeps its extension
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).lstrip("._-")[:64]
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)[:9].lower()
    ext = "." + ext if ext else EXT_BY_TYPE.get((content_type or "").split(";")[0].strip().lower(), ".bin")
    return (stem or "paste") + ext


class Store:
    def __init__(self, directory):
        self.dir = directory
        self._lock = threading.Lock()
        self._reserved_bytes = 0
        self._reserved_files = 0

    def cleanup_temps(self):
        for entry in os.scandir(self.dir):
            if entry.name.startswith(TMP_PREFIX):
                try:
                    os.unlink(entry.path)
                except FileNotFoundError:
                    pass

    def _usage(self):
        # Temp files are left out: every in-flight upload is already counted in full by its
        # reservation, and a crash's leftovers are removed at startup (cleanup_temps).
        size = count = 0
        for entry in os.scandir(self.dir):
            if entry.is_file(follow_symlinks=False) and not entry.name.startswith(TMP_PREFIX):
                size += entry.stat(follow_symlinks=False).st_size
                count += 1
        return size, count

    def reserve(self, nbytes):
        with self._lock:
            size, count = self._usage()
            if size + self._reserved_bytes + nbytes > QUOTA_BYTES:
                raise Rejected(507, "pastes directory quota exceeded")
            if count + self._reserved_files + 1 > MAX_FILES:
                raise Rejected(507, "pastes directory file-count limit reached")
            self._reserved_bytes += nbytes
            self._reserved_files += 1

    def release(self, nbytes):
        with self._lock:
            self._reserved_bytes -= nbytes
            self._reserved_files -= 1

    def store(self, name, nbytes, read):
        """Write nbytes from read(n) to a temp file, then publish it without replacing anything.

        read(n) returns at most n bytes (b"" = the client stopped) and raises Rejected itself for a
        stalled or overdue body. A failure of OUR side (disk full, permissions) is a 5xx, not the
        client's fault.
        """
        tmp = os.path.join(self.dir, TMP_PREFIX + secrets.token_hex(8))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise server_fault(exc) from exc
        try:
            remaining = nbytes
            with os.fdopen(fd, "wb") as f:
                while remaining:
                    chunk = read(min(CHUNK, remaining))
                    if not chunk:
                        raise Rejected(400, "body shorter than Content-Length")
                    try:
                        f.write(chunk)
                    except OSError as exc:
                        raise server_fault(exc) from exc
                    remaining -= len(chunk)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for _ in range(5):
                final = os.path.join(self.dir, f"{stamp}-{secrets.token_hex(2)}-{name}")
                try:
                    os.link(tmp, final)
                    return final
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise server_fault(exc) from exc
            raise Rejected(500, "could not pick a free file name")
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def server_fault(exc):
    full = exc.errno in (errno.ENOSPC, errno.EDQUOT)
    return Rejected(507 if full else 500, "the worker could not store the file: " + (exc.strerror or type(exc).__name__))


def make_handler(store, origins):
    slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT
        server_version = "dw-upload"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")  # never reuse a connection after an unread body
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _reject(self, status, reason):
            LOG.warning("reject %s: %s", status, reason)
            self._reply(status, {"error": reason})

        def do_PUT(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/_dw/upload":
                return self._reject(404, "not found")
            if self.headers.get("X-DW-Upload") != "1":
                return self._reject(403, "missing X-DW-Upload header")
            if self.headers.get("Origin") not in origins:
                return self._reject(403, "foreign or missing Origin")
            if self.headers.get("Transfer-Encoding"):
                return self._reject(411, "chunked bodies are not accepted; send Content-Length")
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or not lengths[0].strip().isdigit():
                return self._reject(411, "exactly one numeric Content-Length is required")
            nbytes = int(lengths[0])
            if nbytes == 0:
                return self._reject(400, "empty file")
            if nbytes > MAX_BYTES:
                return self._reject(413, f"file larger than {MAX_BYTES // (1024 * 1024)} MiB")
            name = safe_name(urllib.parse.parse_qs(url.query).get("name", [""])[0], self.headers.get("Content-Type"))
            deadline = time.monotonic() + READ_DEADLINE

            def read_some(n):
                # read1 returns after ONE receive, and the socket timeout never exceeds what is left of
                # the deadline — so a client trickling a byte now and then cannot hold a slot and a
                # reservation past READ_DEADLINE (a plain read(n) blocks until n bytes arrive).
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Rejected(408, "body not received in time")
                self.connection.settimeout(min(SOCKET_TIMEOUT, left))
                try:
                    return self.rfile.read1(n)
                except (socket.timeout, TimeoutError) as exc:
                    raise Rejected(408, "body stalled or not received in time") from exc
                except OSError as exc:
                    raise Rejected(400, "connection failed mid-body") from exc

            if not slots.acquire(blocking=False):
                return self._reject(503, "too many uploads at once")
            try:
                store.reserve(nbytes)
                try:
                    path = store.store(name, nbytes, read_some)
                finally:
                    store.release(nbytes)
            except Rejected as exc:
                return self._reject(exc.status, exc.reason)
            except OSError as exc:  # anything else on our side: quota scan, directory gone
                return self._reject(500, f"upload failed: {exc.__class__.__name__}")
            finally:
                slots.release()
            LOG.info("stored %s bytes=%d", os.path.basename(path), nbytes)
            self._reply(201, {"path": path, "bytes": nbytes})

        def _method_not_allowed(self):
            self._reject(405, "PUT only")

        do_GET = do_POST = do_DELETE = do_HEAD = _method_not_allowed

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    directory = os.environ["DW_UPLOAD_DIR"]
    origins = {o for o in os.environ["DW_UPLOAD_ORIGINS"].split(",") if o}
    host, port = os.environ.get("DW_UPLOAD_LISTEN", "127.0.0.1:7683").rsplit(":", 1)
    if not os.path.isdir(directory):
        raise SystemExit(f"{directory} does not exist")
    store = Store(directory)
    store.cleanup_temps()  # a crash mid-upload leaves a temp behind; nothing else would remove it
    server = ThreadingHTTPServer((host, int(port)), make_handler(store, origins))
    server.daemon_threads = True
    LOG.info("listening on %s:%s, storing into %s", host, port, directory)
    server.serve_forever()


if __name__ == "__main__":
    main()
