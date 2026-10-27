#!/usr/bin/env python3
"""Smoke-test every registry host through the edge VIP, as a client on the LAN would reach it.

    python3 scripts/edge/smoke.py                      # from a trusted LAN host
    python3 scripts/edge/smoke.py --as untrusted       # from a source outside the trusted ranges
    python3 scripts/edge/smoke.py --host router --host id

Each host gets an anonymous `GET /` over TLS to the VIP with SNI and Host set to its name, like
`curl --resolve <name>:443:<vip>`. The certificate must verify against the system trust store and
cover the name. The answer must match the registry's `smoke` outcome (status, optional
Location fragment). `--as untrusted` instead expects 403 for every host that is not direct, and
the normal outcome for direct ones: run it from a pod or another non-trusted source.
Stdlib only, plus PyYAML through render.load.
"""
from __future__ import annotations

import argparse
import http.client
import pathlib
import socket
import ssl
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import render  # noqa: E402


class PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed address with SNI and certificate checks for `server_name`."""

    def __init__(self, address, server_name, port=443, timeout=15, context=None):
        super().__init__(server_name, port, timeout=timeout, context=context)
        self._address = address
        self._server_name = server_name

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._server_name)
        # read now: getresponse() drops the socket when the server closes the connection
        self.peer_cert = self.sock.getpeercert()


def check(vip, fqdn, expect, insecure=False, path="/"):
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    conn = PinnedHTTPS(vip, fqdn, context=ctx)
    t0 = time.monotonic()
    try:
        # A browser's Accept: Authelia answers a client that sends none with 401 instead of the
        # 302 to its login page that the registry's smoke outcomes describe.
        conn.request("GET", path, headers={"User-Agent": "edge-smoke/1",
                                           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        resp = conn.getresponse()
        resp.read(4096)
        cert = getattr(conn, "peer_cert", None) if not insecure else None
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    ms = int((time.monotonic() - t0) * 1000)
    problems = []
    if "status" in expect and resp.status != expect["status"]:
        problems.append(f"status {resp.status} != {expect['status']}")
    if "status_any" in expect and resp.status not in expect["status_any"]:
        problems.append(f"status {resp.status} not in {expect['status_any']}")
    loc = resp.getheader("Location") or ""
    if expect.get("location_contains") and expect["location_contains"] not in loc:
        problems.append(f"Location {loc!r} lacks {expect['location_contains']!r}")
    if cert:
        days = (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400
        if days < 14:
            problems.append(f"certificate expires in {days:.0f} days")
    detail = f"{resp.status} {ms}ms" + (f" -> {loc[:60]}" if loc else "")
    return not problems, detail + ("" if not problems else "  [" + "; ".join(problems) + "]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--as", dest="role", choices=["trusted", "untrusted"], default="trusted")
    ap.add_argument("--vip", help="address to connect to (default: the registry's vip)")
    ap.add_argument("--host", action="append", help="only these registry names (repeatable)")
    ap.add_argument("--insecure", action="store_true", help="skip certificate verification")
    args = ap.parse_args(argv)
    reg = render.load()
    vip = args.vip or reg["vip"]
    names = sorted(args.host or reg["hosts"])
    unknown = [n for n in names if n not in reg["hosts"]]
    if unknown:
        print(f"not in the registry: {unknown}", file=sys.stderr)
        return 2
    failed = 0
    for name in names:
        h = reg["hosts"][name]
        fqdn = f"{name}.{reg['zone']}"
        mode = render.effective_mode(reg, name)
        expect = h["smoke"]
        if args.role == "untrusted" and mode != "direct":
            expect = {"status": 403}
        ok, detail = check(vip, fqdn, expect, insecure=args.insecure)
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {fqdn:28s} {mode:8s} {detail}")
    # An unknown name must get nothing.
    ok, detail = check(vip, f"edge-smoke-unknown.{reg['zone']}", {"status": 404}, insecure=args.insecure)
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'(unknown host)':28s} {'-':8s} {detail}")
    print(f"edge smoke via {vip} as {args.role}: {'all passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
