#!/usr/bin/env python3
"""Offline tests for the S2S Phase 4 probes (scripts/s2s/phase4-probes.sh + phase4_probe.py).

No cluster and no network beyond 127.0.0.1.

Part 1 tests the in-pod verdict logic (phase4_probe.py) with fabricated JWTs and canned gatekeeper
answers. Part 2 runs the bash orchestrator end to end: stub `kubectl` and `curl` on PATH, and a
small gatekeeper emulator (one HTTP server per fake replica) that answers /auth/token and /metrics
the way gatekeeper does at platform main ac123f047, with switchable faults. The stub `kubectl`
runs the in-pod program locally (same bootstrap, same stdin) against that replica's emulator.

    python3 -m unittest scripts.s2s.test_phase4_probes

Needs bash (on Windows Git's usr/bin/bash.exe, NOT the bin/bash.exe launcher, which prepends
~/bin and so a real kubectl; PHASE4_TEST_BASH overrides). PyYAML is optional, as it is for the
in-pod program. This file is also the stub: `--fake-kubectl` and `--fake-curl` select that role.

FAIL-SAFE: a stub that does not win on PATH would mean real cluster calls. Before every run the
test asserts that `kubectl` and `curl` resolve to the stubs, and the environment points KUBECONFIG
at an empty file and every proxy variable at a closed port, so a real kubectl or curl reaches
nothing even then.
"""

import base64
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "phase4-probes.sh"
PROGRAM = HERE / "phase4_probe.py"

_spec = importlib.util.spec_from_file_location("phase4_probe", str(PROGRAM))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

NS = "strive-ailab"
AUDIENCE = "strive-gatekeeper"
GENERIC = {"error": "invalid_client", "error_description": "invalid client credentials"}
TENANT = "phase4-probe"
REFUSAL_REASONS = (
    "invalid_client_id",
    "invalid_auth_method",
    "secret_hash_present",
    "mtls_subject_present",
    "invalid_k8s_subject",
    "client_id_in_base",
    "client_id_duplicate",
    "k8s_subject_in_base",
    "k8s_subject_duplicate",
)

# The svc-harness extras entry as Phase 3 renders it (platform feat/s2s-charts-values,
# deploy/helm/values/providers/ailab-s2s-registry.yaml): D3 grant_types per audience.
HARNESS_AUDIENCES = {
    "svc-mcp": {"scopes": ["mcp:read", "mcp:write"], "grant_types": ["client_credentials", "token_exchange"]},
    "svc-integration": {"scopes": ["integration:read", "integration:write"], "grant_types": ["token_exchange"]},
    "svc-airlock": {"scopes": ["airlock:read", "airlock:write"], "grant_types": ["token_exchange"]},
    "svc-workflow": {"scopes": ["workflow:read", "workflow:write"], "grant_types": ["token_exchange"]},
    "svc-knowledge": {"scopes": ["knowledge:read", "knowledge:write"], "grant_types": ["token_exchange"]},
    "svc-profile": {"scopes": ["profile:read", "profile:write"], "grant_types": ["token_exchange"]},
    "svc-notification": {"scopes": ["notification:read", "notification:write"], "grant_types": ["token_exchange"]},
    "svc-digest": {"scopes": ["digest:read"], "grant_types": ["token_exchange"]},
}


def harness_entry(**overrides):
    entry = {
        "client_id": "svc-harness",
        "auth_method": "k8s",
        "k8s_subject": "system:serviceaccount:strive-ailab:harness",
        "audiences": json.loads(json.dumps(HARNESS_AUDIENCES)),
        "may_act_for_audiences": sorted(HARNESS_AUDIENCES),
    }
    entry.update(overrides)
    return entry


def extras_text(services=None):
    # JSON is YAML: the in-pod loader reads it with yaml.safe_load.
    return json.dumps({"services": services if services is not None else [harness_entry()]}, indent=2) + "\n"


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")


def fake_jwt(claims, sig="fakesig"):
    return "%s.%s.%s" % (b64url({"alg": "RS256", "kid": "test-kid"}), b64url(claims), sig)


def sa_token(sa, aud, ttl=600, bound_pod=None, no_exp=False):
    now = int(time.time())
    claims = {
        "aud": [aud],
        "sub": "system:serviceaccount:%s:%s" % (NS, sa),
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    if no_exp:
        del claims["exp"]
    if bound_pod:
        claims["kubernetes.io"] = {"namespace": NS, "pod": {"name": bound_pod}}
    return fake_jwt(claims, sig="fakesig" + uuid.uuid4().hex[:12])


def minted_jwt(client_id="svc-harness", audience="svc-mcp", tenant=TENANT, **overrides):
    now = int(time.time())
    scopes = " ".join(sorted(HARNESS_AUDIENCES.get(audience, {}).get("scopes", ["x:read"])))
    claims = {
        "iss": "http://gatekeeper.strive-ailab.svc.cluster.local:5000",
        "aud": "platform-services",
        "sub": client_id,
        "iat": now,
        "nbf": now - 5,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "auth_method": "cookie",
        "roles": [],
        "https://platform/tenant_id": tenant,
        "https://platform/email": client_id + "@platform",
        "scope": scopes,
        "azp": client_id,
        "platform_target_service": audience,
    }
    claims.update(overrides)
    for key in [k for k, v in claims.items() if v is None]:
        del claims[key]
    return fake_jwt(claims, sig="gksig" + uuid.uuid4().hex[:16]), scopes


def body(doc):
    # Starlette's JSONResponse rendering.
    return json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode("utf-8")


def resp(status, doc, headers=None):
    return probe.Response(status, headers or {}, body(doc) if not isinstance(doc, bytes) else doc)


def mint_response(**overrides):
    scope_override = overrides.pop("response_scope", None)
    expires_in = overrides.pop("expires_in", 300)
    token_type = overrides.pop("token_type", "Bearer")
    token, scopes = minted_jwt(**overrides)
    return token, resp(
        200,
        {
            "access_token": token,
            "token_type": token_type,
            "expires_in": expires_in,
            "scope": scope_override if scope_override is not None else scopes,
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        },
    )


# ── Part 1: the in-pod verdict logic ───────────────────────────────────────


class Claims(unittest.TestCase):
    def test_decodes_an_unverified_payload(self):
        token = fake_jwt({"sub": "x", "aud": ["a"]})
        self.assertEqual(probe.jwt_claims(token), {"sub": "x", "aud": ["a"]})

    def test_malformed_tokens_decode_to_none(self):
        for bad in (None, "", "a.b", "only", "x.%%%.y", "x." + b64url(["a list"]) + ".y", "x.bm90LWpzb24.y"):
            self.assertIsNone(probe.jwt_claims(bad), bad)

    def test_read_tokens(self):
        h, w = sa_token("harness", AUDIENCE), sa_token("harness", "nope")
        got = probe.read_tokens(io.StringIO("harness=%s\r\n\nwrong_aud=%s\n" % (h, w)))
        self.assertEqual(got, {"harness": h, "wrong_aud": w})

    def test_read_tokens_errors_never_carry_the_value(self):
        secretish = "not-a-jwt-but-secret-material"
        for text in ("harness=%s\n" % secretish, "bogus=%s\n" % secretish, secretish + "\n"):
            with self.assertRaises(ValueError) as caught:
                probe.read_tokens(io.StringIO(text))
            self.assertNotIn(secretish, str(caught.exception))

    def test_redact(self):
        token = sa_token("harness", AUDIENCE)
        self.assertEqual(probe.redact("x %s y" % token), "x <redacted-jwt> y")

    def test_loopback_only(self):
        self.assertTrue(probe.is_loopback_http("http://127.0.0.1:5000"))
        self.assertTrue(probe.is_loopback_http("http://localhost:5000"))
        for url in ("https://127.0.0.1:5000", "http://gatekeeper:5000", "http://10.0.0.1:5000"):
            self.assertFalse(probe.is_loopback_http(url), url)

    def test_request_shape_is_the_harness_one(self):
        seen = {}

        def http(method, url, data, headers):
            seen.update(method=method, url=url, data=data, headers=headers)
            return resp(401, GENERIC)

        bearer = sa_token("harness", AUDIENCE)
        probe.post_token("http://127.0.0.1:5000", probe.token_form("svc-harness", "svc-mcp", TENANT), bearer, http)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], "http://127.0.0.1:5000/auth/token")
        self.assertEqual(
            seen["data"].decode("ascii"),
            "grant_type=client_credentials&audience=svc-mcp&tenant_id=phase4-probe&client_id=svc-harness",
        )
        self.assertEqual(
            seen["headers"],
            {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "authorization": "Bearer " + bearer,
            },
        )
        self.assertNotIn(b"client_secret", seen["data"])


class Mint(unittest.TestCase):
    def check(self, response, scopes=("mcp:read", "mcp:write")):
        return probe.check_mint(response, "svc-harness", "svc-mcp", TENANT, list(scopes) if scopes else None)

    def test_a_correct_mint_passes(self):
        _, r = mint_response()
        self.assertEqual(self.check(r), [])

    def test_each_claim_is_asserted(self):
        cases = {
            "sub": {"sub": "svc-deepagent"},
            "azp": {"azp": "svc-other"},
            "platform_target_service": {"platform_target_service": "svc-profile"},
            "tenant claim": {"https://platform/tenant_id": "another"},
            "act claim": {"act": {"sub": "svc-harness"}},
            "exp - iat": {"exp": int(time.time()) + 3600},
            "scope claim differs": {"scope": "mcp:read"},
        }
        for needle, override in cases.items():
            token, r = mint_response(**override)
            problems = self.check(r)
            self.assertTrue(any(needle in p for p in problems), (needle, problems))
            self.assertFalse(any(token in p for p in problems), "a problem echoed the access token")

    def test_response_fields(self):
        _, r = mint_response(expires_in=3600)
        self.assertTrue(any("expires_in" in p for p in self.check(r)))
        _, r = mint_response(token_type="bearer")
        self.assertTrue(any("token_type" in p for p in self.check(r)))

    def test_scopes_are_the_registry_grant(self):
        _, r = mint_response()
        self.assertEqual(self.check(r, scopes=None), [])
        self.assertTrue(any("registry grant" in p for p in self.check(r, scopes=("mcp:read",))))

    def test_a_refusal_is_described_without_its_body(self):
        problems = self.check(resp(503, {"error": "temporarily_unavailable", "error_description": "x"}, {"retry-after": "5"}))
        self.assertEqual(problems, ["expected 200, got status 503 error=temporarily_unavailable description=x retry-after=5"])
        self.assertEqual(self.check(resp(200, b"not json")), ["200 without a JSON object body"])
        self.assertEqual(self.check(resp(200, {"access_token": "nope"})), ["200 without a decodable access_token"])


class Refusals(unittest.TestCase):
    def test_the_generic_401(self):
        self.assertEqual(probe.check_refusal(resp(401, GENERIC)), [])

    def test_anything_else_fails(self):
        for r in (
            resp(401, {"error": "invalid_client", "error_description": "client_secret required"}),
            resp(401, {"error": "invalid_client", "error_description": "invalid client credentials", "x": 1}),
            resp(403, {"error": "unauthorized_client", "error_description": "service account is not bound"}),
            resp(503, {"error": "temporarily_unavailable", "error_description": "x"}),
            mint_response()[1],
            probe.Response(0, {}, b"ConnectionRefusedError"),
        ):
            self.assertNotEqual(probe.check_refusal(r), [], r.status)

    def test_refused_mode_accepts_any_invalid_client(self):
        r = resp(401, {"error": "invalid_client", "error_description": "client_secret required"})
        self.assertEqual(probe.check_refusal(r, expected=None), [])
        self.assertNotEqual(probe.check_refusal(resp(403, {"error": "unauthorized_client"}), expected=None), [])

    def test_identical_bodies(self):
        self.assertEqual(probe.check_identical([body(GENERIC)] * 4), [])
        self.assertNotEqual(probe.check_identical([body(GENERIC), body({"error": "invalid_client"})]), [])

    def test_d3_refusal_is_told_apart_from_a_missing_audience(self):
        d3 = resp(403, probe.grant_refused_body("svc-harness", "svc-profile"))
        self.assertEqual(
            probe.grant_refused_body("svc-harness", "svc-profile")["error_description"],
            "client 'svc-harness' not allowed grant 'client_credentials' for audience 'svc-profile'",
        )
        self.assertEqual(probe.check_grant_refused(d3, "svc-harness", "svc-profile"), [])
        missing = resp(
            403,
            {"error": "unauthorized_client", "error_description": "client 'svc-harness' not allowed for audience 'svc-profile'"},
        )
        self.assertNotEqual(probe.check_grant_refused(missing, "svc-harness", "svc-profile"), [])
        self.assertNotEqual(probe.check_grant_refused(mint_response()[1], "svc-harness", "svc-profile"), [])


def exposition(extras_sha="e" * 64, base_sha="b" * 64, rejected=0, refused=None, authenticated=0, miss=0):
    lines = [
        "# HELP gatekeeper_service_registry_info x",
        "# TYPE gatekeeper_service_registry_info gauge",
        'gatekeeper_service_registry_info{base_sha="%s",extras_sha="%s"} 1.0' % (base_sha, extras_sha),
        "gatekeeper_service_registry_extras_rejected %s" % float(rejected),
    ]
    for reason in REFUSAL_REASONS:
        lines.append('gatekeeper_service_registry_extras_refused_total{reason="%s"} %s' % (reason, float((refused or {}).get(reason, 0))))
        lines.append('gatekeeper_service_registry_extras_refused_created{reason="%s"} 1.7e+09' % reason)
    lines.append('gatekeeper_tokenreview_total{outcome="authenticated"} %s' % float(authenticated))
    lines.append('gatekeeper_tokenreview_cache_total{cache="positive",result="miss"} %s' % float(miss))
    return "\n".join(lines) + "\n"


class Registry(unittest.TestCase):
    def test_parse(self):
        samples = probe.parse_metrics(exposition(authenticated=3))
        self.assertEqual(probe.metric(samples, "gatekeeper_tokenreview_total", outcome="authenticated"), 3.0)
        self.assertEqual(probe.metric(samples, "gatekeeper_service_registry_extras_rejected"), 0.0)

    def test_a_good_load(self):
        samples = probe.parse_metrics(exposition())
        self.assertEqual(probe.check_registry(samples, "e" * 64, expect_extras=True), [])

    def test_bad_loads(self):
        cases = [
            (exposition(rejected=1), "e" * 64, "extras_rejected"),
            (exposition(refused={"k8s_subject_in_base": 1}), "e" * 64, "k8s_subject_in_base=1"),
            (exposition(extras_sha="f" * 64), "e" * 64, "roll gatekeeper"),
            (exposition(extras_sha=""), "e" * 64, "no extras loaded"),
            (exposition(), None, "unreadable"),
        ]
        for text, file_sha, needle in cases:
            problems = probe.check_registry(probe.parse_metrics(text), file_sha, expect_extras=True)
            self.assertTrue(any(needle in p for p in problems), (needle, problems))
        self.assertEqual(probe.check_registry(None, "e" * 64, expect_extras=True), ["GET /metrics failed"])

    def test_refused_mode_wants_no_extras(self):
        self.assertEqual(probe.check_registry(probe.parse_metrics(exposition(extras_sha="")), None, expect_extras=False), [])
        self.assertNotEqual(probe.check_registry(probe.parse_metrics(exposition()), None, expect_extras=False), [])

    def test_a_fresh_review_must_be_counted(self):
        before = probe.parse_metrics(exposition(authenticated=4, miss=9))
        self.assertEqual(probe.check_review_happened(before, probe.parse_metrics(exposition(authenticated=5, miss=10))), [])
        problems = probe.check_review_happened(before, probe.parse_metrics(exposition(authenticated=4, miss=10)))
        self.assertEqual(problems, ["no authenticated TokenReview counted for this run's fresh token"])
        self.assertEqual(probe.check_review_happened(before, None), ["GET /metrics failed around the mint"])


class Policy(unittest.TestCase):
    def test_phase3_values_meet_d3(self):
        policy = probe.harness_policy({"services": [harness_entry()]})
        self.assertEqual(policy["problems"], [])
        self.assertEqual(sorted(policy["tx_only"]), sorted(probe.D3_TX_ONLY_AUDIENCES))
        self.assertEqual(policy["scopes"], ["mcp:read", "mcp:write"])
        self.assertEqual(policy["others"], [])

    def test_d3_violations(self):
        open_profile = harness_entry()
        open_profile["audiences"]["svc-profile"]["grant_types"] = ["client_credentials", "token_exchange"]
        no_grant_types = harness_entry()
        del no_grant_types["audiences"]["svc-digest"]["grant_types"]
        for entry, needle in (
            (open_profile, "svc-profile"),
            (no_grant_types, "svc-digest"),
            (harness_entry(k8s_subject="system:serviceaccount:strive-ailab:default"), "k8s_subject"),
            (harness_entry(auth_method="preshared"), "auth_method"),
        ):
            problems = probe.harness_policy({"services": [entry]})["problems"]
            self.assertTrue(any(needle in p for p in problems), (needle, problems))
        self.assertTrue(probe.harness_policy({"services": []})["problems"])
        self.assertTrue(probe.harness_policy({"nope": 1})["problems"])

    def test_a_second_k8s_entry_is_found(self):
        other = {"client_id": "svc-x", "auth_method": "k8s", "k8s_subject": "system:serviceaccount:strive-ailab:x"}
        policy = probe.harness_policy({"services": [harness_entry(), other]})
        self.assertEqual(policy["others"], ["svc-x"])


# ── The gatekeeper emulator (Part 2, and in-process main() tests) ──────────


class Emulator(object):
    """One fake gatekeeper replica: /auth/token and /metrics, per gatekeeper at ac123f047.

    mode: composite (Phase 3 live) | cold (composite, extras absent) | preshared (rolled back).
    faults: fallback (a preshared entry accepts the Bearer), wrong_sub, d3_open, body_variant,
    extras_rejected, stale_extras, slow_revocation (a removed pod's token is honoured 6 s longer).
    """

    def __init__(self, state_dir, mode="composite", faults=(), extras=None, second_k8s=None):
        self.state_dir = state_dir
        self.mode = mode
        self.faults = set(faults)
        self.extras = extras if extras is not None else extras_text()
        self.second_k8s = second_k8s
        self.positive = set()
        self.counters = {"authenticated": 0, "miss": 0}
        self.after_gone = 0
        self.minted = []
        self.lock = threading.Lock()
        emulator = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, headers=None, content_type="application/json"):
                data = payload if isinstance(payload, bytes) else body(payload)
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path != "/metrics":
                    return self._send(404, {"detail": "Not Found"})
                return self._send(200, emulator.metrics().encode("utf-8"), content_type="text/plain")

            def do_POST(self):
                if self.path != "/auth/token":
                    return self._send(404, {"detail": "Not Found"})
                length = int(self.headers.get("content-length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode("ascii")))
                auth = self.headers.get("authorization") or ""
                bearer = auth[7:] if auth.lower().startswith("bearer ") else None
                with emulator.lock:
                    status, payload, headers = emulator.token(form, bearer)
                return self._send(status, payload, headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def registry(self):
        reg = {"svc-deepagent": {"auth": "preshared", "audiences": {"svc-mcp": {"scopes": ["mcp:read"]}}}}
        if self.mode == "composite":
            reg["svc-harness"] = {
                "auth": "k8s",
                "subject": "system:serviceaccount:strive-ailab:harness",
                "audiences": HARNESS_AUDIENCES,
            }
            if self.second_k8s:
                reg[self.second_k8s] = {
                    "auth": "k8s",
                    "subject": "system:serviceaccount:strive-ailab:other",
                    "audiences": {"svc-mcp": {"scopes": ["mcp:read"]}},
                }
        return reg

    def authentic(self, claims, sig):
        if not sig.startswith("fakesig"):
            return False
        pod = ((claims.get("kubernetes.io") or {}).get("pod") or {}).get("name")
        if pod:
            gone = os.path.join(self.state_dir, "gone-" + pod)
            if os.path.exists(gone):
                if "reaccept" in self.faults:
                    # Refuses the removed pod's bearer twice, then honours it again.
                    self.after_gone += 1
                    return self.after_gone > 2
                delay = 6.0 if "slow_revocation" in self.faults else 0.0
                if time.time() - os.path.getmtime(gone) >= delay:
                    return False
        return True

    def token(self, form, bearer):
        client_id = form.get("client_id")
        refusal = (401, GENERIC, None)
        if self.mode == "preshared":
            if not form.get("client_secret"):
                return 401, {"error": "invalid_client", "error_description": "client_secret required"}, None
            return refusal
        entry = self.registry().get(client_id)
        if entry is None:
            return refusal
        if entry["auth"] == "k8s":
            if bearer is None:
                if "body_variant" in self.faults:
                    return 401, {"error": "invalid_client", "error_description": "Bearer required"}, None
                return refusal
            claims = probe.jwt_claims(bearer)
            aud = (claims or {}).get("aud")
            auds = [aud] if isinstance(aud, str) else aud or []
            if not claims or AUDIENCE not in auds or claims.get("sub") != entry["subject"] or claims.get("exp", 0) <= time.time():
                return refusal
            digest = hashlib.sha256(bearer.encode()).hexdigest()
            if digest not in self.positive:
                self.counters["miss"] += 1
                if not self.authentic(claims, bearer.rsplit(".", 1)[1]):
                    # A removed pod's token: TokenReview says why in status.error -> 503 (GC4).
                    return 503, {"error": "temporarily_unavailable", "error_description": "client authentication temporarily unavailable"}, {"Retry-After": "5"}
                self.counters["authenticated"] += 1
                self.positive.add(digest)
            elif not self.authentic(claims, bearer.rsplit(".", 1)[1]):
                # The positive cache (<= 60 s in gatekeeper) is modelled as expiring at once.
                self.positive.discard(digest)
                return 503, {"error": "temporarily_unavailable", "error_description": "client authentication temporarily unavailable"}, {"Retry-After": "5"}
        elif not (bearer and "fallback" in self.faults):
            return refusal
        audience = form.get("audience")
        if audience not in entry["audiences"]:
            return 403, {"error": "unauthorized_client", "error_description": "client %r not allowed for audience %r" % (client_id, audience)}, None
        cfg = entry["audiences"][audience]
        if form.get("grant_type") != "client_credentials":
            return 400, {"error": "unsupported_grant_type", "error_description": "x"}, None
        if "client_credentials" not in cfg.get("grant_types", ["client_credentials"]) and "d3_open" not in self.faults:
            return 403, probe.grant_refused_body(client_id, audience), None
        sub = "svc-imposter" if "wrong_sub" in self.faults else client_id
        token, scopes = minted_jwt(client_id=client_id, audience=audience, tenant=form.get("tenant_id"), sub=sub)
        self.minted.append(token)
        return 200, {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": " ".join(sorted(cfg["scopes"])),
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }, None

    def metrics(self):
        extras_sha = ""
        if self.mode == "composite":
            extras_sha = hashlib.sha256(self.extras.encode("utf-8")).hexdigest()
            if "stale_extras" in self.faults:
                extras_sha = "0" * 64
        if self.mode == "preshared":
            return 'gatekeeper_requests_total{path="/auth/token"} 1.0\n'
        return exposition(
            extras_sha=extras_sha,
            rejected=1 if "extras_rejected" in self.faults else 0,
            authenticated=self.counters["authenticated"],
            miss=self.counters["miss"],
        )


class InProcess(unittest.TestCase):
    """main() of the in-pod program against one emulator, without kubectl or bash."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phase4-inproc-")
        self.extras = os.path.join(self.dir, "registry.yaml")
        with open(self.extras, "w", encoding="utf-8", newline="\n") as f:
            f.write(extras_text())
        self.own = os.path.join(self.dir, "own-token")
        with open(self.own, "w", encoding="utf-8") as f:
            f.write(sa_token("gatekeeper", "https://kubernetes.default.svc"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_main(self, emulator, mode="probe", tokens=None):
        tokens = tokens or {
            "harness": sa_token("harness", AUDIENCE),
            "wrong_aud": sa_token("harness", "not-strive-gatekeeper"),
            "alt_sa": sa_token("default", AUDIENCE),
        }
        out = io.StringIO()
        argv = ["--mode", mode, "--base-url", emulator.url, "--extras-file", self.extras, "--own-token-file", self.own]
        stdin = io.StringIO("".join("%s=%s\n" % kv for kv in tokens.items()))
        rc = probe.main(argv, stdin=stdin, out=out)
        text = out.getvalue()
        for secret in list(tokens.values()) + emulator.minted:
            self.assertNotIn(secret, text)
        self.assertNotIn("<redacted-jwt>", text)
        return rc, text

    def test_all_pass(self):
        emulator = Emulator(self.dir)
        self.addCleanup(emulator.close)
        rc, text = self.run_main(emulator)
        self.assertEqual(rc, 0, text)
        self.assertIn("PASS k8s-mint[svc-mcp]", text)
        for audience in probe.D3_TX_ONLY_AUDIENCES:
            self.assertIn("PASS d3-refused[%s]" % audience, text)
        for check in ("refuse-preshared[svc-deepagent]", "refuse-unregistered", "refuse-other-sa", "refuse-wrong-audience", "refuse-no-token", "refusals-identical", "registry"):
            self.assertIn("PASS " + check, text)
        self.assertIn("OWNTOKEN fp=", text)
        if HAVE_YAML:
            self.assertIn("PASS d3-policy", text)
        self.assertTrue(text.rstrip().endswith("RESULT pass"))

    def test_each_fault_fails(self):
        for fault, needle in (
            ("fallback", "FAIL refuse-preshared[svc-deepagent]"),
            ("wrong_sub", "FAIL k8s-mint[svc-mcp]"),
            ("d3_open", "FAIL d3-refused[svc-profile]"),
            ("body_variant", "FAIL refuse-no-token"),
            ("extras_rejected", "FAIL registry"),
            ("stale_extras", "FAIL registry"),
        ):
            emulator = Emulator(self.dir, faults=[fault])
            self.addCleanup(emulator.close)
            rc, text = self.run_main(emulator)
            self.assertEqual(rc, 1, fault)
            self.assertIn(needle, text, fault)
            self.assertTrue(text.rstrip().endswith("RESULT fail"), fault)

    def test_a_second_k8s_entry_is_probed(self):
        other = {"client_id": "svc-other", "auth_method": "k8s", "k8s_subject": "system:serviceaccount:strive-ailab:other"}
        with open(self.extras, "w", encoding="utf-8", newline="\n") as f:
            f.write(extras_text([harness_entry(), other]))
        emulator = Emulator(self.dir, second_k8s="svc-other", extras=extras_text([harness_entry(), other]))
        self.addCleanup(emulator.close)
        rc, text = self.run_main(emulator)
        if HAVE_YAML:
            self.assertIn("PASS refuse-second-k8s[svc-other]", text)
        self.assertEqual(rc, 0, text)

    def test_refused_mode(self):
        cold = Emulator(self.dir, mode="cold")
        self.addCleanup(cold.close)
        rc, text = self.run_main(cold, mode="refused")
        self.assertEqual(rc, 0, text)
        self.assertIn("PASS harness-refused[svc-mcp]", text)
        live = Emulator(self.dir)
        self.addCleanup(live.close)
        rc, text = self.run_main(live, mode="refused")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL harness-refused[svc-mcp]", text)
        self.assertIn("FAIL registry-no-extras", text)

    def test_single_mode(self):
        emulator = Emulator(self.dir)
        self.addCleanup(emulator.close)
        rc, text = self.run_main(emulator, mode="single", tokens={"held": sa_token("harness", AUDIENCE)})
        self.assertEqual(rc, 0)
        self.assertIn("MINT 200 -", text)
        rc, text = self.run_main(emulator, mode="single", tokens={"held": sa_token("harness", "wrong")})
        self.assertIn("MINT 401 invalid_client", text)

    def test_bad_invocations(self):
        out = io.StringIO()
        self.assertEqual(probe.main(["--base-url", "http://10.1.2.3:5000"], stdin=io.StringIO(""), out=out), 2)
        out = io.StringIO()
        self.assertEqual(probe.main([], stdin=io.StringIO("harness=nope\n"), out=out), 2)
        self.assertNotIn("nope", out.getvalue())


# ── Part 2: the bash orchestrator end to end ───────────────────────────────


def find_bash():
    override = os.environ.get("PHASE4_TEST_BASH")
    if override:
        return override
    if os.name == "nt":
        # usr/bin/bash.exe keeps the caller's PATH order; bin/bash.exe (a launcher) puts
        # /mingw64/bin:/usr/bin:~/bin first, and ~/bin may hold the real kubectl.
        candidate = r"C:\Program Files\Git\usr\bin\bash.exe"
        if os.path.exists(candidate):
            return candidate
    return shutil.which("bash")


def fake_kubectl(argv):
    """The stub `kubectl`: answers the read-only calls the script makes, runs `exec` locally."""
    state = os.environ["PHASE4_FAKE_DIR"]
    with open(os.path.join(state, "scenario.json"), encoding="utf-8") as f:
        sc = json.load(f)
    with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(["kubectl"] + argv) + "\n")
    args, i = [], 0
    while i < len(argv):
        if argv[i] in ("--context", "-n"):
            i += 2
            continue
        args.append(argv[i])
        i += 1

    def out(text):
        # Raw bytes, as Go's kubectl writes them (text-mode stdout would add CRs on Windows).
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0

    def not_found(what):
        sys.stderr.write('Error from server (NotFound): %s not found\n' % what)
        return 1

    verb = args[:2]
    if verb == ["get", "pods"]:
        label, jsonpath = args[args.index("-l") + 1], args[args.index("-o") + 1]
        if label.endswith("=gatekeeper"):
            return out("".join("%s|%s|%s|%s\n" % (p["name"], p.get("phase", "Running"), p.get("deleting", ""), str(p.get("ready", True)).lower()) for p in sc["gatekeeper"]))
        h = sc.get("harness")
        if not h:
            return out("")
        if "metadata.uid" in jsonpath:
            return out("%s|%s|%s|\n" % (h["name"], h["uid"], h.get("ready", "True")))
        return out("%s|Running||%s|%s|%s|%s|%s\n" % (h["name"], h.get("ready", "True"), h.get("sa", "harness"), h.get("migrate", ["Completed", "0"])[0], h.get("migrate", ["Completed", "0"])[1], h.get("env", "HARNESS_CLIENT_TOKEN_FILE HARNESS_DATABASE_URL")))
    if verb == ["get", "pod"]:
        name = args[2]
        counter = os.path.join(state, "podpolls-" + name)
        count = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
        with open(counter, "w") as f:
            f.write(str(count))
        uid = (sc.get("harness") or {}).get("uid", "")
        if count >= sc.get("pod_removal_after", 3):
            gone = os.path.join(state, "gone-" + name)
            if not os.path.exists(gone):
                open(gone, "w").close()
            return out("")
        return out("%s|%s" % (uid, "2026-10-06T00:00:00Z" if count >= 2 else ""))
    if verb == ["get", "configmap"]:
        return out(sc["cm"]) if sc.get("cm") is not None else not_found("configmaps " + args[2])
    if verb == ["get", "helmrelease"]:
        return out(sc.get("hr", "True"))
    if verb == ["get", "serviceaccount"]:
        if args[2] == "harness" and not sc.get("sa_harness", True):
            return not_found("serviceaccounts harness")
        return out("serviceaccount/%s\n" % args[2])
    if verb == ["get", "ingressroute"]:
        return out("ingressroute.traefik.io/%s\n" % args[2]) if sc.get("ingressroute", True) else not_found("ingressroutes")
    if verb == ["create", "token"]:
        sa = args[2]
        if sa == "harness" and not sc.get("sa_harness", True):
            return not_found("serviceaccounts harness")
        aud = args[args.index("--audience") + 1]
        pod = args[args.index("--bound-object-name") + 1] if "--bound-object-name" in args else None
        if pod:  # the revocation drill's held token: its lifetime is the scenario's
            token = sa_token(sa, aud, ttl=sc.get("held_ttl", 600), bound_pod=pod, no_exp=sc.get("held_no_exp", False))
        else:
            token = sa_token(sa, aud)
        with open(os.path.join(state, "tokens.log"), "a", encoding="utf-8") as f:
            f.write(token + "\n")
        return out(token + "\n")
    if args[:2] == ["exec", "-i"]:
        pod = args[2]
        replica = [p for p in sc["gatekeeper"] if p["name"] == pod][0]
        cmd = args[args.index("--") + 1 :]
        if cmd[:2] != ["python", "-c"]:
            return not_found("exec command")
        url = replica["url"]
        harness_gone = os.path.exists(os.path.join(state, "gone-" + (sc.get("harness") or {}).get("name", "")))
        if harness_gone and pod in sc.get("unreachable_after_removal", ()):
            url = "http://127.0.0.1:9"  # nothing listens: the in-pod program gets no HTTP answer
        if harness_gone and pod in sc.get("flaky_after_removal", ()):
            counter = os.path.join(state, "flaky-" + pod)
            count = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
            with open(counter, "w") as f:
                f.write(str(count))
            if count % 2 == 0:
                url = "http://127.0.0.1:9"  # every other attempt gets no HTTP answer
        local = [sys.executable, "-c"] + cmd[2:] + ["--base-url", url, "--extras-file", sc["extras_file"], "--own-token-file", sc["own_token_file"]]
        return subprocess.call(local, stdin=sys.stdin)
    with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(["UNEXPECTED"] + argv) + "\n")
    sys.stderr.write("fake kubectl: unexpected call %r\n" % (argv,))
    return 99


def fake_curl(argv):
    state = os.environ["PHASE4_FAKE_DIR"]
    with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(["curl"] + argv) + "\n")
    sys.stdout.write(os.environ.get("PHASE4_FAKE_EDGE_CODE", "401"))
    return 0


BASH = find_bash()
WRITE_VERBS = {"apply", "patch", "scale", "delete", "edit", "annotate", "label", "replace", "rollout", "set", "cordon", "drain"}


@unittest.skipIf(BASH is None, "bash is required (set PHASE4_TEST_BASH)")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phase4-e2e-")
        self.stubs = os.path.join(self.dir, "bin")
        os.mkdir(self.stubs)
        py = sys.executable.replace("\\", "/")
        me = str(pathlib.Path(__file__).resolve()).replace("\\", "/")
        for name, role in (("kubectl", "--fake-kubectl"), ("curl", "--fake-curl")):
            path = os.path.join(self.stubs, name)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write('#!/usr/bin/env bash\nexec "%s" "%s" %s "$@"\n' % (py, me, role))
            os.chmod(path, 0o755)
        self.extras = os.path.join(self.dir, "registry.yaml")
        with open(self.extras, "w", encoding="utf-8", newline="\n") as f:
            f.write(extras_text())
        self.own = os.path.join(self.dir, "own-token")
        with open(self.own, "w", encoding="utf-8") as f:
            f.write(sa_token("gatekeeper", "https://kubernetes.default.svc"))
        self.emulators = {}

    def tearDown(self):
        for emulator in self.emulators.values():
            emulator.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def scenario(self, replicas=("gatekeeper-a", "gatekeeper-b"), faults=None, mode="composite", **overrides):
        faults = faults or {}
        pods = []
        for name in replicas:
            self.emulators[name] = Emulator(self.dir, mode=mode, faults=faults.get(name, ()))
            pods.append({"name": name, "url": self.emulators[name].url})
        sc = {
            "gatekeeper": pods,
            "harness": {"name": "harness-7d9c-x", "uid": "uid-1"},
            "cm": extras_text() if mode == "composite" else None,
            "hr": "True",
            "extras_file": self.extras if mode == "composite" else os.path.join(self.dir, "absent.yaml"),
            "own_token_file": self.own,
        }
        sc.update(overrides)
        with open(os.path.join(self.dir, "scenario.json"), "w", encoding="utf-8") as f:
            json.dump(sc, f)
        return sc

    def run_script(self, *args, edge="401", extra_env=None):
        env = dict(os.environ)
        env.update(
            PATH=self.stubs + os.pathsep + env.get("PATH", ""),
            PHASE4_FAKE_DIR=self.dir,
            PHASE4_FAKE_EDGE_CODE=edge,
            PHASE4_POLL_SECONDS="0.2",
            PHASE4_POD_POLL_SECONDS="0.2",
        )
        # Dead ends for a real binary that would slip through: no kubeconfig, no route out.
        kubeconfig = os.path.join(self.dir, "empty-kubeconfig")
        open(kubeconfig, "w").close()
        env["KUBECONFIG"] = kubeconfig
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env[var] = "http://127.0.0.1:9"
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
        env.update(extra_env or {})
        self.assert_stubbed(env)
        done = subprocess.run([BASH, str(SCRIPT)] + list(args), env=env, capture_output=True, timeout=300)
        text = done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")
        secrets = []
        tokens_log = os.path.join(self.dir, "tokens.log")
        if os.path.exists(tokens_log):
            secrets += open(tokens_log, encoding="utf-8").read().split()
        for emulator in self.emulators.values():
            secrets += emulator.minted
        secrets.append(open(self.own, encoding="utf-8").read().strip())
        for secret in secrets:
            self.assertNotIn(secret, text, "a token reached the output")
        self.assertNotIn("<redacted-jwt>", text, "something JWT-shaped reached the output")
        return done.returncode, text

    def assert_stubbed(self, env):
        """kubectl and curl must be THIS test's stubs, or the run would reach a real cluster."""
        found = subprocess.run([BASH, "-c", "command -v kubectl; command -v curl"], env=env, capture_output=True, text=True)
        paths = found.stdout.split()
        marker = "/%s/bin/" % os.path.basename(self.dir)
        self.assertEqual(len(paths), 2, found.stdout + found.stderr)
        for path in paths:
            self.assertIn(marker, path.replace(os.sep, "/"), "not the stub: %s (refusing to run)" % path)

    def calls(self):
        path = os.path.join(self.dir, "calls.log")
        if not os.path.exists(path):
            return []
        return [json.loads(line) for line in open(path, encoding="utf-8")]

    def kubectl_verbs(self):
        verbs = []
        for call in self.calls():
            self.assertNotEqual(call[0], "UNEXPECTED", call)
            if call[0] == "kubectl":
                args = [a for i, a in enumerate(call[1:]) if a not in ("--context", "-n") and call[1:][i - 1] not in ("--context", "-n")]
                verbs.append(args[0])
        self.assertFalse(WRITE_VERBS & set(verbs), "the script made a cluster write")
        return verbs

    def exec_pods(self):
        return [c[c.index("-i") + 1] for c in self.calls() if c[0] == "kubectl" and "exec" in c]

    def test_activation_passes(self):
        self.scenario()
        rc, text = self.run_script()
        self.assertEqual(rc, 0, text)
        self.assertIn("ALL CHECKS PASSED", text)
        self.assertNotIn("DARKEN", text)
        for pod in ("gatekeeper-a", "gatekeeper-b"):
            self.assertIn("== Replica %s (probe)" % pod, text)
        self.assertIn("PASS k8s-mint[svc-mcp]", text)
        self.assertIn("PASS registry: base_sha", text)
        self.assertIn("PASS init container migrate Completed (exit 0)", text)
        self.assertIn("-> 401", text)
        self.assertIn("report-ailab-pin-drift", text)
        self.assertEqual(sorted(self.exec_pods()), ["gatekeeper-a", "gatekeeper-b"])
        self.assertEqual(self.kubectl_verbs().count("create"), 3)
        self.assertIn(["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", "https://strive.place/api/harness/admin/v1/chat"], self.calls())

    def test_a_fallback_on_one_replica_darkens(self):
        self.scenario(faults={"gatekeeper-b": ["fallback"]})
        rc, text = self.run_script()
        self.assertEqual(rc, 1)
        self.assertIn("gatekeeper-b: refuse-preshared[svc-deepagent]", text)
        self.assertNotIn("gatekeeper-a: refuse-preshared", text)
        self.assertIn("DARKEN THE HARNESS", text)
        self.assertIn("kubectl --context admin@ai -n strive-ailab scale deployment/harness --replicas=0", text)
        self.assertIn("""patch helmrelease strive --type=merge -p '{"spec":{"suspend":true}}'""", text)

    def test_registry_drift_fails(self):
        self.scenario(cm=extras_text([harness_entry(), {"client_id": "svc-new", "auth_method": "k8s", "k8s_subject": "system:serviceaccount:strive-ailab:new"}]))
        rc, text = self.run_script("--gatekeeper-only")
        self.assertEqual(rc, 1)
        self.assertIn("!= ConfigMap", text)

    def test_edge_redirect_darkens(self):
        self.scenario()
        rc, text = self.run_script(edge="302")
        self.assertEqual(rc, 1)
        self.assertIn("a login redirect", text)
        self.assertIn("DARKEN THE HARNESS", text)

    def test_harness_secret_or_migrate_failure_darkens(self):
        self.scenario(harness={"name": "harness-x", "uid": "u", "env": "HARNESS_CLIENT_TOKEN_FILE HARNESS_CLIENT_SECRET", "migrate": ["Error", "1"]})
        rc, text = self.run_script()
        self.assertEqual(rc, 1)
        self.assertIn("harness env carries HARNESS_CLIENT_SECRET", text)
        self.assertIn("init container migrate: reason=Error exit=1", text)

    def test_dry_run_makes_no_call(self):
        self.scenario()
        rc, text = self.run_script("--dry-run")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.calls(), [])
        self.assertIn("Plan (dry run", text)
        self.assertIn("scale deployment/harness --replicas=0", text)

    def test_replica_flag(self):
        self.scenario()
        rc, text = self.run_script("--replica", "gatekeeper-b", "--gatekeeper-only")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.exec_pods(), ["gatekeeper-b"])
        self.assertNotIn("curl", [c[0] for c in self.calls()])
        rc, text = self.run_script("--replica", "gatekeeper-z")
        self.assertEqual(rc, 1)
        self.assertIn("gatekeeper-z is not a Running, Ready gatekeeper pod", text)

    def test_a_roll_in_flight_mints_nothing(self):
        sc = self.scenario()
        sc["gatekeeper"][1]["ready"] = False
        with open(os.path.join(self.dir, "scenario.json"), "w", encoding="utf-8") as f:
            json.dump(sc, f)
        rc, text = self.run_script()
        self.assertEqual(rc, 1)
        self.assertIn("1 of 2 gatekeeper pods are Running and Ready", text)
        self.assertNotIn("create", self.kubectl_verbs())

    def test_bad_arguments(self):
        rc, _ = self.run_script("--replica")
        self.assertEqual(rc, 2)
        rc, _ = self.run_script("--expect-refused", "--revocation-drill")
        self.assertEqual(rc, 2)
        rc, _ = self.run_script("--tenant", "a b")
        self.assertEqual(rc, 2)

    def test_expect_refused_cold_start(self):
        self.scenario(mode="cold")
        rc, text = self.run_script("--expect-refused")
        self.assertEqual(rc, 0, text)
        self.assertIn("PASS harness-refused[svc-mcp]", text)
        self.assertNotIn("curl", [c[0] for c in self.calls()])

    def test_expect_refused_fails_while_svc_harness_is_served(self):
        self.scenario()
        rc, text = self.run_script("--expect-refused")
        self.assertEqual(rc, 1)
        self.assertIn("harness-refused[svc-mcp]", text)
        self.assertIn("DARKEN THE HARNESS", text)

    def test_expect_refused_after_a_rollback(self):
        self.scenario(mode="preshared", sa_harness=False)
        rc, text = self.run_script("--expect-refused")
        self.assertEqual(rc, 0, text)
        self.assertIn("serviceaccount harness is absent", text)
        self.assertIn("harness-refused[svc-mcp] skipped", text)
        self.assertIn("client_secret required", text)

    # The revocation drill observes until the held token's own exp; these scenarios hold a short
    # token (HELD_TTL) so a run takes seconds, not ten minutes.
    HELD_TTL = 45
    DRILL_ENV = {"PHASE4_REMOVAL_MARGIN_SECONDS": "5"}

    def drill(self, **env):
        extra = dict(self.DRILL_ENV)
        extra.update(env)
        started = time.time()
        rc, text = self.run_script("--revocation-drill", extra_env=extra)
        return rc, text, time.time() - started

    def single_mints(self, pod):
        return sum(1 for c in self.calls() if c[0] == "kubectl" and "exec" in c and pod in c and "single" in c)

    def test_revocation_drill(self):
        self.scenario(pod_removal_after=3, held_ttl=self.HELD_TTL)
        rc, text, took = self.drill()
        self.assertEqual(rc, 0, text)
        self.assertIn("accepts the held bearer before the revocation", text)
        for pod in ("gatekeeper-a", "gatekeeper-b"):
            self.assertIn("PASS %s rejected the revoked bearer" % pod, text)
            self.assertIn("PASS %s never accepted the revoked bearer again before its expiry" % pod, text)
        self.assertIn("observed until the held token's expiry", text)
        # It kept polling well past two refused rounds, up to the token's exp.
        self.assertGreater(self.single_mints("gatekeeper-a"), 4)
        self.assertGreaterEqual(took, self.HELD_TTL - 15)
        created = [c for c in self.calls() if c[0] == "kubectl" and "create" in c]
        self.assertEqual(len(created), 1)
        self.assertIn("--bound-object-kind", created[0])
        self.assertIn("uid-1", created[0])

    def test_revocation_over_the_bound_fails(self):
        self.scenario(pod_removal_after=2, held_ttl=self.HELD_TTL, faults={"gatekeeper-a": ["slow_revocation"]})
        rc, text, _ = self.drill(PHASE4_REVOCATION_BOUND="1")
        self.assertEqual(rc, 1, text)
        self.assertIn("gatekeeper-a", text)
        self.assertIn("the 1s bound", text)
        self.assertIn("DARKEN THE HARNESS", text)

    def test_revocation_a_connection_failure_is_never_a_refusal(self):
        # gatekeeper-b answered the baseline mint, then gives no HTTP answer at all: that is not
        # a refusal of the bearer, and the drill must not pass on it.
        self.scenario(pod_removal_after=2, held_ttl=self.HELD_TTL, unreachable_after_removal=["gatekeeper-b"])
        rc, text, _ = self.drill()
        self.assertEqual(rc, 1, text)
        self.assertIn("could not observe gatekeeper-b", text)
        self.assertNotIn("PASS gatekeeper-b rejected the revoked bearer", text)
        self.assertNotIn("gatekeeper-b: rejected at", text)
        self.assertIn("PASS gatekeeper-a accepts the held bearer before the revocation", text)

    def test_revocation_intermittent_no_answers_count_for_nothing(self):
        # gatekeeper-b alternates a refusal and no answer: never two refused rounds in a row, so
        # the refused-round streak keeps resetting and the end state is never observed as stable.
        self.scenario(pod_removal_after=2, held_ttl=self.HELD_TTL, flaky_after_removal=["gatekeeper-b"])
        rc, text, _ = self.drill(PHASE4_MAX_NO_ANSWER="99")
        self.assertEqual(rc, 1, text)
        self.assertIn("no answer", text)
        self.assertIn("consecutive refused rounds", text)

    def test_revocation_a_later_acceptance_fails(self):
        # gatekeeper-a refuses the removed pod's bearer twice, then accepts it again: two refused
        # rounds prove nothing, the drill watches until the token's exp.
        self.scenario(pod_removal_after=2, held_ttl=self.HELD_TTL, faults={"gatekeeper-a": ["reaccept"]})
        rc, text, _ = self.drill()
        self.assertEqual(rc, 1, text)
        self.assertIn("gatekeeper-a: accepted the held bearer AGAIN", text)
        self.assertIn("DARKEN THE HARNESS", text)

    def test_revocation_needs_the_held_tokens_exp(self):
        self.scenario(pod_removal_after=2, held_no_exp=True)
        rc, text, _ = self.drill()
        self.assertEqual(rc, 1, text)
        self.assertIn("cannot read the held token's exp", text)
        self.assertEqual(self.single_mints("gatekeeper-a"), 0)


RUNBOOK = HERE.parents[1] / "docs" / "runbooks" / "s2s-identity.md"
CM_YAML = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: gatekeeper-registry-extras\n  namespace: strive-ailab\ndata:\n  registry.yaml: |\n    services: []\n"


def drill1_backup_lines():
    """Drill 1's lines from `OUT=` through the chained ConfigMap delete, as the runbook has them."""
    text = RUNBOOK.read_text(encoding="utf-8")
    block = [b for b in text.split("```sh\n")[1:] if "delete configmap gatekeeper-registry-extras" in b.split("```")[0]]
    if len(block) != 1:
        raise AssertionError("expected one sh block deleting the extras ConfigMap, found %d" % len(block))
    lines = block[0].split("```")[0].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("OUT="))
    end = next(i for i, line in enumerate(lines) if "delete configmap gatekeeper-registry-extras" in line)
    while lines[end].rstrip().endswith("\\"):
        end += 1
    deletes = [i for i, line in enumerate(lines) if "delete configmap" in line]
    if deletes != [end]:
        raise AssertionError("a ConfigMap delete outside the guarded backup chain: lines %r" % deletes)
    return "\n".join(lines[start : end + 1]) + "\n"


@unittest.skipIf(BASH is None, "bash is required (set PHASE4_TEST_BASH)")
class RunbookDrillBackup(unittest.TestCase):
    """Drill 1 deletes the extras ConfigMap only after a verified copy is saved under a gitignored _out/."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phase4-runbook-")
        self.log = os.path.join(self.dir, "k.log")
        self.stub = os.path.join(self.dir, "k.sh")
        with open(self.stub, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                'printf "%s\\n" "$*" >> "$K_LOG"\n'
                'if [ "$1" = get ]; then [ -n "$K_GET_FAIL" ] && exit 1; printf "%s" "$K_CM"; fi\n'
            )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_lines(self, ignored=True, get_fail=False):
        repo = os.path.join(self.dir, "repo")
        os.mkdir(repo)
        git = ["git", "-C", repo]
        subprocess.run(git + ["init", "-q"], check=True)
        if ignored:
            with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8", newline="\n") as f:
                f.write("kubernetes/infra/_out/\n")
        env = dict(os.environ, K_LOG=self.log, K_CM=CM_YAML, K_GET_FAIL="1" if get_fail else "")
        script = 'cd "$1" || exit 9\nK="bash %s"\n%s' % (self.stub.replace("\\", "/"), drill1_backup_lines())
        done = subprocess.run([BASH, "-c", script, "drill", repo.replace("\\", "/")], env=env, capture_output=True, text=True)
        calls = open(self.log, encoding="utf-8").read().splitlines() if os.path.exists(self.log) else []
        saved = os.path.join(repo, "kubernetes", "infra", "_out", "gatekeeper-registry-extras-drill.yaml")
        return done, calls, saved

    def test_a_verified_copy_then_the_delete(self):
        done, calls, saved = self.run_lines()
        self.assertTrue(any(c.startswith("delete configmap gatekeeper-registry-extras") for c in calls), done.stderr)
        with open(saved, encoding="utf-8") as f:
            self.assertEqual(f.read(), CM_YAML)

    def test_no_copy_no_delete(self):
        done, calls, _ = self.run_lines(get_fail=True)
        self.assertFalse(any(c.startswith("delete") for c in calls))
        self.assertIn("STOP", done.stderr)

    def test_not_a_gitignored_out_no_delete(self):
        done, calls, _ = self.run_lines(ignored=False)
        self.assertFalse(any(c.startswith("delete") for c in calls))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-kubectl":
        sys.exit(fake_kubectl(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-curl":
        sys.exit(fake_curl(sys.argv[2:]))
    unittest.main()
