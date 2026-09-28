"""Unit + socket-level tests for files/dw_upload.py (the web terminal's paste endpoint).

Run through tests/test-web-gate.sh. Stdlib only.
"""

import errno
import http.client
import importlib.util
import json
import os
import pathlib
import socket
import stat
import tempfile
import threading
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("dw_upload", HERE.parent / "files" / "dw_upload.py")
up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(up)

ORIGIN = "https://dw1.chifor.me"


class NameTests(unittest.TestCase):
    def test_safe_names(self):
        cases = {
            ("shot.png", ""): "shot.png",
            ("../../etc/passwd", ""): "passwd.bin",
            ("..\\..\\x.PDF", ""): "x.pdf",
            ("my report (final).docx", ""): "my_report_final_.docx",
            ("", "image/png"): "paste.png",
            (".bashrc", ""): "bashrc.bin",
            ("image", "image/jpeg; charset=x"): "image.jpg",
            ("a" * 300 + ".png", ""): "a" * 64 + ".png",
            ("$(rm -rf ~);.sh", ""): "rm_-rf_.sh",
        }
        for (raw, ctype), want in cases.items():
            self.assertEqual(up.safe_name(raw, ctype), want, raw)

    def test_names_never_escape_or_hide(self):
        for raw in ("../x", "/abs/x", "..", ".", "-rf", "~/x", "a/../../b"):
            name = up.safe_name(raw, "")
            self.assertNotIn("/", name)
            self.assertFalse(name.startswith((".", "-")), name)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.store = up.Store(self.dir)
        self.server = up.ThreadingHTTPServer(("127.0.0.1", 0), up.make_handler(self.store, {ORIGIN}))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def put(self, body=b"hello", name="a.txt", headers=None, drop=()):
        h = {"X-DW-Upload": "1", "Origin": ORIGIN, "Content-Type": "text/plain", "Content-Length": str(len(body))}
        h.update(headers or {})
        for k in drop:
            h.pop(k, None)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("PUT", f"/_dw/upload?name={name}", skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def files(self):
        return sorted(os.listdir(self.dir))


class UploadTests(ServerTestCase):
    def test_happy_path(self):
        status, data = self.put(b"\x89PNG fake", name="shot.png", headers={"Content-Type": "image/png"})
        self.assertEqual(status, 201)
        path = data["path"]
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertTrue(path.endswith("-shot.png"))
        self.assertEqual(pathlib.Path(path).read_bytes(), b"\x89PNG fake")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.files(), [os.path.basename(path)], "no temp file left behind")

    def test_same_name_twice_never_replaces(self):
        a = self.put(b"one", name="x.txt")[1]["path"]
        b = self.put(b"two", name="x.txt")[1]["path"]
        self.assertNotEqual(a, b)
        self.assertEqual(pathlib.Path(a).read_bytes(), b"one")

    def test_rejections(self):
        cases = [
            ({"headers": {"X-DW-Upload": "0"}}, 403),
            ({"drop": ("X-DW-Upload",)}, 403),
            ({"headers": {"Origin": "https://evil.example"}}, 403),
            ({"drop": ("Origin",)}, 403),
            ({"headers": {"Origin": "null"}}, 403),
            ({"body": b""}, 400),
            ({"headers": {"Content-Length": "abc"}}, 411),
        ]
        for kwargs, want in cases:
            status, _ = self.put(**kwargs)
            self.assertEqual(status, want, kwargs)
        self.assertEqual(self.files(), [])

    def test_chunked_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("PUT", "/_dw/upload?name=a.txt", body=iter([b"abc"]), headers={"X-DW-Upload": "1", "Origin": ORIGIN}, encode_chunked=True)
        self.assertEqual(conn.getresponse().status, 411)
        self.assertEqual(self.files(), [])

    def test_duplicate_content_length_is_refused(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(
            b"PUT /_dw/upload?name=a.txt HTTP/1.1\r\nHost: x\r\nX-DW-Upload: 1\r\nOrigin: " + ORIGIN.encode()
            + b"\r\nContent-Length: 3\r\nContent-Length: 30\r\n\r\nabc"
        )
        self.assertIn(b" 411 ", s.recv(4096))
        s.close()
        self.assertEqual(self.files(), [])

    def test_too_large_is_refused_before_reading(self):
        status, _ = self.put(b"x", headers={"Content-Length": str(up.MAX_BYTES + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(self.files(), [])

    def test_truncated_body_leaves_nothing(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(
            b"PUT /_dw/upload?name=a.txt HTTP/1.1\r\nHost: x\r\nX-DW-Upload: 1\r\nOrigin: " + ORIGIN.encode()
            + b"\r\nContent-Length: 1000\r\n\r\nonly-a-little"
        )
        s.shutdown(socket.SHUT_WR)  # the client gives up mid-body
        self.assertIn(b" 400 ", s.recv(4096))
        s.close()
        time.sleep(0.2)
        self.assertEqual(self.files(), [])

    def test_other_methods_and_paths(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/_dw/upload")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()
        status, _ = self.put(name="a.txt", headers={}, drop=())
        self.assertEqual(status, 201)

    def test_startup_cleans_stale_temps(self):
        stale = os.path.join(self.dir, up.TMP_PREFIX + "deadbeef")
        pathlib.Path(stale).write_bytes(b"half")
        keep = os.path.join(self.dir, "20260101-000000-abcd-keep.txt")
        pathlib.Path(keep).write_bytes(b"k")
        self.store.cleanup_temps()
        self.assertEqual(self.files(), ["20260101-000000-abcd-keep.txt"])


class DeadlineTests(ServerTestCase):
    """A trickling body must not outlive READ_DEADLINE (Codex review of #936)."""

    def setUp(self):
        self._saved = (up.READ_DEADLINE, up.SOCKET_TIMEOUT)
        up.READ_DEADLINE, up.SOCKET_TIMEOUT = 1.0, 5
        super().setUp()

    def tearDown(self):
        super().tearDown()
        up.READ_DEADLINE, up.SOCKET_TIMEOUT = self._saved

    def test_trickle_is_cut_at_the_deadline(self):
        # One byte every 0.3 s: always inside the 5 s inactivity timeout, never done by the 1 s deadline.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(
            b"PUT /_dw/upload?name=a.txt HTTP/1.1\r\nHost: x\r\nX-DW-Upload: 1\r\nOrigin: " + ORIGIN.encode()
            + b"\r\nContent-Length: 100\r\n\r\n"
        )
        started = time.monotonic()
        s.settimeout(0.3)
        reply = b""
        while time.monotonic() - started < 8 and not reply:
            try:
                s.sendall(b"x")
            except OSError:
                pass
            try:
                reply = s.recv(4096)
            except socket.timeout:
                continue
        elapsed = time.monotonic() - started
        s.close()
        self.assertIn(b" 408 ", reply)
        self.assertLess(elapsed, 3.0, f"the trickle was served for {elapsed:.1f}s past a 1s deadline")
        time.sleep(0.2)
        self.assertEqual(self.files(), [])


class ServerFaultTests(ServerTestCase):
    """Our disk failing is a 5xx, not the client's fault (Claude review of #936)."""

    def test_disk_full_is_507_and_leaves_nothing(self):
        real_link = up.os.link

        def full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        up.os.link = full
        try:
            status, data = self.put(b"hello")
        finally:
            up.os.link = real_link
        self.assertEqual(status, 507)
        self.assertIn("could not store", data.get("error", ""))
        self.assertEqual(self.files(), [])

    def test_quota_does_not_double_count_in_flight_temps(self):
        # A temp file on disk is already covered by its upload's reservation: _usage() must skip it.
        pathlib.Path(self.dir, up.TMP_PREFIX + "inflight").write_bytes(b"x" * 1000)
        self.assertEqual(self.store._usage(), (0, 0))


class QuotaTests(ServerTestCase):
    def setUp(self):
        self._saved = (up.QUOTA_BYTES, up.MAX_FILES)
        up.QUOTA_BYTES, up.MAX_FILES = 100, 3
        super().setUp()

    def tearDown(self):
        super().tearDown()
        up.QUOTA_BYTES, up.MAX_FILES = self._saved

    def test_byte_quota(self):
        self.assertEqual(self.put(b"x" * 60)[0], 201)
        self.assertEqual(self.put(b"x" * 60)[0], 507)
        self.assertEqual(self.put(b"x" * 40)[0], 201)

    def test_file_count_cap(self):
        for _ in range(3):
            self.assertEqual(self.put(b"x")[0], 201)
        self.assertEqual(self.put(b"x")[0], 507)

    def test_in_flight_uploads_are_reserved(self):
        # One upload stalls mid-body holding a 60-byte reservation; a second 60-byte upload must be
        # refused even though the directory is still empty on disk.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(
            b"PUT /_dw/upload?name=a.txt HTTP/1.1\r\nHost: x\r\nX-DW-Upload: 1\r\nOrigin: " + ORIGIN.encode()
            + b"\r\nContent-Length: 60\r\n\r\nxx"
        )
        time.sleep(0.3)
        self.assertEqual(self.put(b"y" * 60)[0], 507)
        s.sendall(b"x" * 58)
        self.assertIn(b" 201 ", s.recv(4096))
        s.close()


if __name__ == "__main__":
    unittest.main()
