#!/usr/bin/env python3
"""dw-access-verify: Caddy forward_auth target for the dev-worker web terminal's tunnel path.

Cloudflare Access puts a signed JWT in the Cf-Access-Jwt-Assertion header of every request it
proxies to the origin. Caddy sends any request that carries that header here first; a 2xx lets the
request through to ttyd, anything else is returned to the client. So a LAN host or a cluster pod
that reaches Caddy directly cannot get past by inventing the header: it would need a token signed by
Cloudflare for THIS worker's Access application.

Responses: 200 = valid token; 403 = missing, malformed, forged, expired or for another application;
503 = no usable signing keys (the JWKS has never been fetched, or not for longer than MAX_STALE).
GET /_dw/healthz answers 200 once signing keys are cached, 503 otherwise.

Stdlib + python3-jwt (2.7) + python3-cryptography, all Ubuntu 24.04 packages. The token is never
logged. docs/runbooks/dev-workers.md § "Remote access (web terminals)".
"""

import json
import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

LOG = logging.getLogger("dw-access-verify")

REFRESH_AFTER = 300  # refetch the key set when it is older than this (seconds)
MIN_REFETCH_INTERVAL = 30  # never hit the JWKS endpoint more often than this
MAX_STALE = 86400  # stop trusting a key set that could not be refreshed for this long
FETCH_TIMEOUT = 5
LEEWAY = 30  # clock skew tolerated on exp/nbf/iat
MAX_TOKEN_BYTES = 8192


class KeysUnavailable(Exception):
    """No signing key set is usable: the answer is 503, not 403."""


class KeyCache:
    """Signing keys by kid, refreshed lazily; the network fetch never runs under the state lock.

    PyJWKClient drops its cached set when a fetch fails and refetches on every unknown kid; this
    keeps the last good set through a JWKS outage (bounded by MAX_STALE) and rate-limits fetches,
    so a stream of tokens with made-up kids cannot turn into a stream of outbound requests.

    Two locks: `_lock` guards the key map and timestamps (held only for reads and the swap);
    `_refresh_lock` admits one fetcher at a time. A routine refresh of a still-usable set is
    attempted non-blocking, so while one request fetches (up to FETCH_TIMEOUT) every other request
    keeps verifying against the cached keys instead of queueing behind it.
    """

    def __init__(self, fetch, now=time.monotonic):
        self._fetch = fetch
        self._now = now
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._keys = {}
        self._fetched_at = None
        self._last_attempt = None

    def _refresh(self, blocking):
        if not self._refresh_lock.acquire(blocking=blocking):
            return  # someone else is fetching; the caller uses the cached set
        try:
            now = self._now()
            with self._lock:
                if self._last_attempt is not None and now - self._last_attempt < MIN_REFETCH_INTERVAL:
                    return
                self._last_attempt = now
            try:
                keyset = jwt.PyJWKSet.from_dict(self._fetch())
                keys = {k.key_id: k for k in keyset.keys if k.key_id}
                if not keys:
                    raise ValueError("key set has no keys with a kid")
            except Exception as exc:  # noqa: BLE001 - any failure keeps the previous set
                LOG.warning("JWKS refresh failed, keeping the previous key set: %s", exc)
                return
            with self._lock:
                self._keys, self._fetched_at = keys, now
        finally:
            self._refresh_lock.release()

    def _usable(self):
        return self._fetched_at is not None and self._now() - self._fetched_at <= MAX_STALE

    def get(self, kid):
        """Return the key for kid, or None when the (fresh enough) set does not have it."""
        with self._lock:
            fetched, known = self._fetched_at, kid in self._keys
        due = fetched is None or self._now() - fetched > REFRESH_AFTER
        if due or not known:
            # Block only when the answer depends on the fetch: nothing cached, or this kid unknown.
            self._refresh(blocking=fetched is None or not known)
        with self._lock:
            if not self._usable():
                raise KeysUnavailable()
            return self._keys.get(kid)

    def healthy(self):
        with self._lock:
            fetched = self._fetched_at
        if fetched is None or self._now() - fetched > REFRESH_AFTER:
            self._refresh(blocking=fetched is None)
        with self._lock:
            return self._usable()


class Verifier:
    def __init__(self, audiences, issuer, keys):
        self.audiences = list(audiences)
        self.issuer = issuer
        self.keys = keys

    def verify(self, token):
        """Return the token's claims, or None when it is not valid for this worker.

        Raises KeysUnavailable when no key set can be trusted at all.
        """
        if not token or len(token) > MAX_TOKEN_BYTES:
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None
        # Checked before the key lookup, so alg=none or an HMAC token never reaches a key.
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            return None
        key = self.keys.get(header["kid"])
        if key is None:
            return None
        try:
            return jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            return None


def http_fetch(url):
    def fetch():
        req = urllib.request.Request(url, headers={"User-Agent": "dw-access-verify"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 - fixed https URL
            return json.load(resp)

    return fetch


def make_handler(verifier):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        server_version = "dw-access-verify"
        sys_version = ""

        def log_message(self, fmt, *args):  # the default logs every request line to stderr
            pass

        def _reply(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _handle(self):
            path = self.path.split("?", 1)[0]
            if path == "/_dw/healthz":
                self._reply(200 if verifier.keys.healthy() else 503)
                return
            if path != "/_dw/verify":
                self._reply(404)
                return
            uri = self.headers.get("X-Forwarded-Uri", "?")
            try:
                claims = verifier.verify(self.headers.get("Cf-Access-Jwt-Assertion"))
            except KeysUnavailable:
                LOG.error("deny 503 (no usable signing keys) uri=%s", uri)
                self._reply(503)
                return
            if claims is None:
                LOG.warning("deny 403 uri=%s", uri)
                self._reply(403)
                return
            LOG.info("allow email=%s uri=%s", claims.get("email", "?"), uri)
            self._reply(200)

        do_GET = do_HEAD = do_POST = do_PUT = _handle

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    team = os.environ["DW_ACCESS_TEAM_DOMAIN"]
    audiences = [a for a in os.environ["DW_ACCESS_AUD"].split(",") if a]
    host, port = os.environ.get("DW_ACCESS_LISTEN", "127.0.0.1:7682").rsplit(":", 1)
    if not audiences:
        raise SystemExit("DW_ACCESS_AUD is empty")
    keys = KeyCache(http_fetch(f"https://{team}/cdn-cgi/access/certs"))
    verifier = Verifier(audiences, f"https://{team}", keys)
    keys.healthy()  # warm the cache; a failure here is logged and retried on demand
    server = ThreadingHTTPServer((host, int(port)), make_handler(verifier))
    server.daemon_threads = True
    LOG.info("listening on %s:%s for aud=%s", host, port, ",".join(a[:8] + "…" for a in audiences))
    server.serve_forever()


if __name__ == "__main__":
    main()
