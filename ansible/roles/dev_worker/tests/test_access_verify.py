"""Unit tests for files/dw_access_verify.py (the web terminal's Access-JWT gate).

Run through tests/test-web-gate.sh (what CI invokes), which also decides what a missing python3-jwt means.
"""

import base64
import hashlib
import hmac
import http.client
import importlib.util
import json
import pathlib
import threading
import time
import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("dw_access_verify", HERE.parent / "files" / "dw_access_verify.py")
dav = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dav)

TEAM = "team.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "a" * 64
OTHER_AUD = "b" * 64


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(private_key, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    d.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return d


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Fetcher:
    """Stands in for the JWKS endpoint; `fail` makes it raise like a network error."""

    def __init__(self, *jwks):
        self.jwks = list(jwks)
        self.calls = 0
        self.fail = False

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise OSError("network is unreachable")
        return {"keys": self.jwks}


KEY1 = new_key()
KEY2 = new_key()


def token(key=KEY1, kid="k1", aud=AUD, iss=ISSUER, alg="RS256", **overrides):
    now = int(time.time())
    claims = {"aud": [aud], "iss": iss, "iat": now, "exp": now + 3600, "email": "op@example.com"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fetch = Fetcher(jwk(KEY1, "k1"))
        self.cache = dav.KeyCache(self.fetch, now=self.clock)
        self.v = dav.Verifier([AUD], ISSUER, self.cache)

    def test_valid_token(self):
        claims = self.v.verify(token())
        self.assertIsNotNone(claims)
        self.assertEqual(claims["email"], "op@example.com")

    def test_rejects_missing_empty_and_garbage(self):
        for bad in (None, "", "forged", "a.b.c", "x" * (dav.MAX_TOKEN_BYTES + 1)):
            self.assertIsNone(self.v.verify(bad), bad and bad[:20])

    def test_rejects_other_application(self):
        self.assertIsNone(self.v.verify(token(aud=OTHER_AUD)))

    def test_rejects_other_issuer(self):
        self.assertIsNone(self.v.verify(token(iss="https://evil.cloudflareaccess.com")))

    def test_rejects_expired_beyond_leeway(self):
        now = int(time.time())
        self.assertIsNone(self.v.verify(token(iat=now - 7200, exp=now - dav.LEEWAY - 5)))

    def test_accepts_expiry_within_leeway(self):
        now = int(time.time())
        self.assertIsNotNone(self.v.verify(token(iat=now - 7200, exp=now - 5)))

    def test_requires_exp(self):
        self.assertIsNone(self.v.verify(token(exp=None)))

    def test_rejects_wrong_signing_key_with_known_kid(self):
        self.assertIsNone(self.v.verify(token(key=KEY2, kid="k1")))

    def test_rejects_alg_none(self):
        now = int(time.time())
        unsigned = jwt.encode(
            {"aud": [AUD], "iss": ISSUER, "iat": now, "exp": now + 60}, None, algorithm="none", headers={"kid": "k1"}
        )
        self.assertIsNone(self.v.verify(unsigned))

    def test_rejects_hmac_key_confusion(self):
        # The classic RS256 -> HS256 swap: sign with the PUBLIC key bytes as an HMAC secret.
        pem = KEY1.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        now = int(time.time())
        # PyJWT refuses to sign with a PEM as an HMAC secret, so the forgery is assembled by hand.
        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode()) + b"." + b64(
            json.dumps({"aud": [AUD], "iss": ISSUER, "iat": now, "exp": now + 60}).encode()
        )
        forged = (signing_input + b"." + b64(hmac.new(pem, signing_input, hashlib.sha256).digest())).decode()
        self.assertIsNone(self.v.verify(forged))

    def test_unknown_kid_triggers_one_refresh(self):
        self.v.verify(token())
        self.fetch.jwks.append(jwk(KEY2, "k2"))  # Cloudflare rotated its signing key
        self.clock.t += dav.MIN_REFETCH_INTERVAL + 1
        self.assertIsNotNone(self.v.verify(token(key=KEY2, kid="k2")))
        self.assertEqual(self.fetch.calls, 2)

    def test_unknown_kid_refetch_is_rate_limited(self):
        self.v.verify(token())
        self.clock.t += dav.MIN_REFETCH_INTERVAL + 1
        for _ in range(20):
            self.assertIsNone(self.v.verify(token(kid="made-up")))
        self.assertEqual(self.fetch.calls, 2, "a flood of unknown kids must not become a flood of fetches")

    def test_outage_with_warm_cache_keeps_working(self):
        self.v.verify(token())
        self.fetch.fail = True
        self.clock.t += dav.REFRESH_AFTER + 1  # due for a refresh, and the refresh fails
        self.assertIsNotNone(self.v.verify(token()))

    def test_outage_with_cold_cache_is_unavailable_not_forbidden(self):
        self.fetch.fail = True
        with self.assertRaises(dav.KeysUnavailable):
            self.v.verify(token())

    def test_key_set_too_stale_is_unavailable(self):
        self.v.verify(token())
        self.fetch.fail = True
        self.clock.t += dav.MAX_STALE + 1
        with self.assertRaises(dav.KeysUnavailable):
            self.v.verify(token())

    def test_revoked_key_disappears_on_refresh(self):
        self.v.verify(token())
        self.fetch.jwks = [jwk(KEY2, "k2")]
        self.clock.t += dav.REFRESH_AFTER + 1
        self.assertIsNone(self.v.verify(token()))

    def test_slow_refresh_does_not_stall_other_requests(self):
        # A routine refresh of a still-usable set must not make every other request wait for it.
        self.v.verify(token())
        self.clock.t += dav.REFRESH_AFTER + 1
        started, release = threading.Event(), threading.Event()
        real = self.fetch

        def slow_fetch():
            started.set()
            release.wait(5)
            return real()

        self.cache._fetch = slow_fetch
        refresher = threading.Thread(target=self.v.verify, args=(token(),))
        refresher.start()
        self.assertTrue(started.wait(2), "the refresh never started")
        t0 = time.monotonic()
        self.assertIsNotNone(self.v.verify(token()))  # served from the cached set meanwhile
        self.assertLess(time.monotonic() - t0, 1.0)
        release.set()
        refresher.join(5)

    def test_healthz_tracks_key_availability(self):
        self.fetch.fail = True
        self.assertFalse(self.cache.healthy())
        self.fetch.fail = False
        self.clock.t += dav.MIN_REFETCH_INTERVAL + 1
        self.assertTrue(self.cache.healthy())


class HttpTests(unittest.TestCase):
    """The forward_auth contract Caddy relies on: status codes only."""

    @classmethod
    def setUpClass(cls):
        cls.fetch = Fetcher(jwk(KEY1, "k1"))
        verifier = dav.Verifier([AUD], ISSUER, dav.KeyCache(cls.fetch))
        cls.server = dav.ThreadingHTTPServer(("127.0.0.1", 0), dav.make_handler(verifier))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def status(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        code = conn.getresponse().status
        conn.close()
        return code

    def test_valid_token_is_200(self):
        self.assertEqual(self.status("/_dw/verify", {"Cf-Access-Jwt-Assertion": token()}), 200)

    def test_forged_token_is_403(self):
        self.assertEqual(self.status("/_dw/verify", {"Cf-Access-Jwt-Assertion": "forged"}), 403)

    def test_missing_header_is_403(self):
        self.assertEqual(self.status("/_dw/verify"), 403)

    def test_other_paths_are_404(self):
        self.assertEqual(self.status("/"), 404)
        self.assertEqual(self.status("/_dw/verify/../x", {"Cf-Access-Jwt-Assertion": token()}), 404)

    def test_healthz(self):
        self.assertEqual(self.status("/_dw/healthz"), 200)


if __name__ == "__main__":
    unittest.main()
