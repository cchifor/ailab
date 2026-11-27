#!/usr/bin/env python3
"""relay-observer-gate.py: the credential-less bridge between the phase A Runner's observer client and the host
observer (plans/2026-11-27-relay-phase-a-observer-plan.md, section 2).

Runs as UID 10001 in a `--network none --read-only --log-driver none` container that shares the session's tmpfs socket
volume. It serves /run/relay-session/observer.sock:
  - one connection carries one request: one JSON line of at most 64 KiB (duplicate keys and non-finite numbers are
    refused), read within 5 s; a second concurrent connection is answered `busy`;
  - the gate assigns `seq` and its receipt time `gateNow` (epoch ms, the Docker VM clock the Runner shares), writes
    {"seq", "gateNow", "request"} as one line to STDOUT, and relays the host's reply with that seq (read from STDIN,
    at most 512 KiB) to the client;
  - a client disconnect or the request deadline emits {"seq", "cancel": true}; cancellation is TERMINAL: every later
    connection is answered `cancelled` (a one-shot run needs no resynchronization), and a late reply is discarded;
  - host EOF on STDIN is terminal too (`observer-down`).
STDOUT carries only protocol frames; diagnostics go to STDERR as fixed words. It holds no credentials.
"""
import json
import os
import socket
import sys
import threading
import time

SOCKET = os.environ.get("RELAY_OBSERVER_SOCKET", "/run/relay-session/observer.sock")
MAX_REQUEST = 64 * 1024
MAX_REPLY = 512 * 1024
READ_S = 5.0
MAX_WAIT_S = 130.0  # no checkpoint is longer than 120 s; a request deadline beyond this is clamped
CALLS = ("checkpoint", "deployment", "predecessor")


class Bad(Exception):
    pass


def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise Bad("duplicate-key")
        out[k] = v
    return out


def _constant(name):
    raise Bad("non-finite")


MAX_DEPTH = 32


def _float(s):
    v = float(s)
    if v != v or v in (float("inf"), float("-inf")):  # 1e999 overflows to infinity: refused like NaN
        raise Bad("non-finite")
    return v


def _depth_ok(raw: bytes) -> bool:
    """Bound nesting BEFORE parsing (a deep document would raise RecursionError inside json)."""
    depth, in_str, esc = 0, False, False
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_DEPTH:
                return False
        elif b in (0x5D, 0x7D):
            depth -= 1
    return True


def strict_json(raw: bytes):
    if not _depth_ok(raw):
        raise Bad("too-deep")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant,
                          parse_float=_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise Bad("malformed") from e


def diag(word: str) -> None:
    try:
        sys.stderr.write(json.dumps({"gate": word}) + "\n")
        sys.stderr.flush()
    except OSError:
        pass


class Gate:
    def __init__(self, sock_path: str, out, inp):
        self.sock_path, self.out, self.inp = sock_path, out, inp
        self.lock = threading.Lock()  # guards seq/busy/terminal/replies
        self.out_lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.seq = 0
        self.busy = False
        self.terminal = None  # None, "cancelled" or "observer-down"
        self.replies = {}  # seq -> raw reply line (bytes)
        self.waiting = None  # the seq currently awaited

    # ------------------------------------------------------------------ host side
    def emit(self, obj) -> None:
        """STDOUT to the host. A failed write is terminal (observer-down) and aborts the current request."""
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self.out_lock:
            try:
                self.out.write(line)
                self.out.flush()
            except (OSError, ValueError):
                with self.cond:
                    self.terminal = "observer-down"
                    self.cond.notify_all()
                raise

    def read_host(self) -> None:
        """STDIN: one reply per line, {"seq": n, ...}. EOF or a malformed/oversized line is terminal."""
        try:
            while True:
                line = self.inp.readline(MAX_REPLY + 2)
                if not line:
                    break
                if len(line) > MAX_REPLY or not line.endswith(b"\n"):
                    diag("reply-oversize")
                    break
                try:
                    obj = strict_json(line)
                    if isinstance(obj, dict) and set(obj) == {"clockProbe"} and isinstance(obj["clockProbe"], str) \
                            and len(obj["clockProbe"]) <= 64:
                        self.emit({"clockProbe": obj["clockProbe"], "gateNow": int(time.time() * 1000)})
                        continue
                    seq = obj["seq"] if isinstance(obj, dict) else None
                except (Bad, KeyError):
                    diag("reply-malformed")
                    break
                with self.cond:
                    if isinstance(seq, int) and not isinstance(seq, bool) and seq == self.waiting:
                        self.replies[seq] = line
                        self.cond.notify_all()
                    else:
                        diag("reply-discarded")  # late (after a cancel) or unexpected; never relayed
        except (OSError, ValueError):
            diag("stdin-error")
        with self.cond:
            if self.terminal is None:
                self.terminal = "observer-down"
            self.cond.notify_all()
        diag("host-eof")

    # ------------------------------------------------------------------ client side
    @staticmethod
    def answer(conn, obj) -> None:
        try:
            conn.settimeout(READ_S)
            conn.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except OSError:
            pass

    def read_request(self, conn):
        conn.settimeout(READ_S)
        end = time.monotonic() + READ_S
        buf = b""
        while b"\n" not in buf:
            if len(buf) > MAX_REQUEST or time.monotonic() > end:
                raise Bad("request-oversize-or-slow")
            chunk = conn.recv(65536)
            if not chunk:
                raise Bad("request-truncated")
            buf += chunk
        line, _, rest = buf.partition(b"\n")
        if rest or len(line) > MAX_REQUEST:
            raise Bad("request-extra-bytes")
        req = strict_json(line)
        if (not isinstance(req, dict) or req.get("v") != 1 or req.get("call") not in CALLS
                or not isinstance(req.get("deadline"), int) or isinstance(req.get("deadline"), bool)):
            raise Bad("request-shape")
        return req

    def handle(self, conn) -> None:
        try:
            try:
                req = self.read_request(conn)
            except (Bad, OSError) as e:
                diag(e.args[0] if isinstance(e, Bad) else "request-io")
                self.answer(conn, {"v": 1, "ok": False, "code": "refused"})
                return
            with self.cond:
                if self.terminal:
                    code = self.terminal
                else:
                    code = None
                    self.seq += 1
                    seq = self.seq
                    self.waiting = seq
            if code:
                self.answer(conn, {"v": 1, "ok": False, "code": code})
                return
            now_ms = int(time.time() * 1000)
            try:
                self.emit({"seq": seq, "gateNow": now_ms, "request": req})
            except (OSError, ValueError):
                diag("stdout-failed")
                self.answer(conn, {"v": 1, "ok": False, "code": "observer-down"})
                return
            wait_until = time.monotonic() + max(0.0, min(MAX_WAIT_S, (req["deadline"] - now_ms) / 1000.0))
            reply = self.await_reply(conn, seq, wait_until)
            if reply is None:
                with self.cond:
                    if self.terminal is None:
                        self.terminal = "cancelled"
                    self.waiting = None
                try:
                    self.emit({"seq": seq, "cancel": True})
                except (OSError, ValueError):
                    diag("stdout-failed")
                diag("cancelled")
                self.answer(conn, {"v": 1, "ok": False, "code": "cancelled"})
                return
            with self.cond:  # the request is complete: admit the next connection before sending (no busy race)
                self.busy = False
            try:
                conn.settimeout(READ_S)
                conn.sendall(reply)
            except OSError:
                with self.cond:
                    if self.terminal is None:
                        self.terminal = "cancelled"
                try:
                    self.emit({"seq": seq, "cancel": True})
                except (OSError, ValueError):
                    pass
                diag("reply-undeliverable")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self.cond:
                self.busy = False

    def await_reply(self, conn, seq, wait_until):
        """The host's reply for seq, or None on client disconnect, deadline or a terminal state."""
        conn.setblocking(False)
        while True:
            with self.cond:
                if seq in self.replies:
                    self.waiting = None
                    return self.replies.pop(seq)
                if self.terminal:
                    return None
                remaining = wait_until - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(min(0.1, remaining))
            try:  # a readable socket with no data = the client went away (it sends nothing after its request)
                if conn.recv(1, socket.MSG_PEEK) == b"":
                    return None
            except BlockingIOError:
                pass
            except OSError:
                return None

    def serve(self) -> None:
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(self.sock_path)
        finally:
            os.umask(old)
        srv.listen(8)
        threading.Thread(target=self.guarded, args=(self.read_host,), daemon=True).start()
        self.emit({"ready": True})
        while True:
            conn, _ = srv.accept()
            with self.cond:
                busy = self.busy
                if not busy:
                    self.busy = True
            if busy:
                self.answer(conn, {"v": 1, "ok": False, "code": "busy"})
                conn.close()
                continue
            threading.Thread(target=self.guarded, args=(self.handle, conn), daemon=True).start()

    def guarded(self, fn, *args) -> None:
        """Any unexpected error in a gate thread is terminal (observer-down), never a silently dead thread."""
        try:
            fn(*args)
        except BaseException:  # noqa: BLE001
            diag("thread-error")
            with self.cond:
                self.terminal = "observer-down"
                self.busy = False
                self.cond.notify_all()


def main() -> int:
    Gate(SOCKET, sys.stdout.buffer, sys.stdin.buffer).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
