#!/usr/bin/env python3
"""Offline tests for the in-cluster S2S Phase 4 probe Job.

scripts/s2s/phase4_job.py (Endpoints -> replicas, TokenRequests, per-replica phase4_probe.main
against pod IPs, base_sha agreement, the summary and the exit code), the pod-IP opening in
phase4_probe.main, the stdlib extras reader, the vendored copies under
kubernetes/apps/infrastructure/s2s-phase4-probe/ and that tree's manifests.

    python3 -m unittest scripts.s2s.test_phase4_job
    python3 -m pytest scripts/s2s/

No cluster and no network beyond 127.0.0.1: the Kubernetes API is a fake (or a local HTTP server
for the client itself), and the "pod IPs" are routed to one gatekeeper emulator per replica (the
emulator of test_phase4_probes.py). PyYAML is NOT needed: the manifests are read with the same
stdlib reader the Job uses, so this runs on the CI runner as it is.
"""

import importlib.util
import io
import json
import os
import pathlib
import shutil
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
TREE = REPO / "kubernetes" / "apps" / "infrastructure" / "s2s-phase4-probe"
FLUX_KS = REPO / "kubernetes" / "apps" / "clusters" / "ai" / "s2s-phase4-probe.yaml"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("phase4_probe", HERE / "phase4_probe.py")
job = _load("phase4_job", HERE / "phase4_job.py")
t4 = _load("phase4_probe_tests", HERE / "test_phase4_probes.py")

# The live gatekeeper Endpoints (2026-10-06), as `get endpoints gatekeeper -o json` returns them.
LIVE_ENDPOINTS = {
    "kind": "Endpoints",
    "apiVersion": "v1",
    "metadata": {"name": "gatekeeper", "namespace": "strive-ailab"},
    "subsets": [
        {
            "addresses": [
                {"ip": "10.244.0.29", "nodeName": "talos-cp2", "targetRef": {"kind": "Pod", "name": "gatekeeper-65b8f6946d-v6f6v", "namespace": "strive-ailab"}},
                {"ip": "10.244.1.73", "nodeName": "talos-cp3", "targetRef": {"kind": "Pod", "name": "gatekeeper-65b8f6946d-8ttzl", "namespace": "strive-ailab"}},
            ],
            "ports": [{"name": "http", "port": 5000, "protocol": "TCP"}],
        }
    ],
}
POD_A, IP_A = "gatekeeper-65b8f6946d-v6f6v", "10.244.0.29"
POD_B, IP_B = "gatekeeper-65b8f6946d-8ttzl", "10.244.1.73"

# The live gatekeeper-registry-extras data.registry.yaml (2026-10-06), as Helm's toYaml renders it.
LIVE_EXTRAS = """services:
- audiences:
    svc-airlock:
      grant_types:
      - token_exchange
      scopes:
      - airlock:read
      - airlock:write
    svc-digest:
      grant_types:
      - token_exchange
      scopes:
      - digest:read
    svc-integration:
      grant_types:
      - token_exchange
      scopes:
      - integration:read
      - integration:write
    svc-knowledge:
      grant_types:
      - token_exchange
      scopes:
      - knowledge:read
      - knowledge:write
    svc-mcp:
      grant_types:
      - client_credentials
      - token_exchange
      scopes:
      - mcp:read
      - mcp:write
    svc-notification:
      grant_types:
      - token_exchange
      scopes:
      - notification:read
      - notification:write
    svc-profile:
      grant_types:
      - token_exchange
      scopes:
      - profile:read
      - profile:write
    svc-workflow:
      grant_types:
      - token_exchange
      scopes:
      - workflow:read
      - workflow:write
  auth_method: k8s
  client_id: svc-harness
  k8s_subject: system:serviceaccount:strive-ailab:harness
  may_act_for_audiences:
  - svc-mcp
  - svc-integration
  - svc-airlock
  - svc-workflow
  - svc-knowledge
  - svc-profile
"""


class FakeKube(object):
    """The three calls the Job makes; records them; mints fabricated SA tokens.

    extras: the ConfigMap's data.registry.yaml (None = the ConfigMap is absent, a 404).
    """

    def __init__(self, endpoints=None, token_error=None, endpoints_error=None, extras="default", configmap_error=None):
        self.endpoints_doc = endpoints if endpoints is not None else json.loads(json.dumps(LIVE_ENDPOINTS))
        self.token_error = token_error
        self.endpoints_error = endpoints_error
        self.extras = t4.extras_text() if extras == "default" else extras
        self.configmap_error = configmap_error
        self.calls = []
        self.issued = []

    def configmap(self, namespace, name):
        self.calls.append(("configmap", namespace, name))
        if self.configmap_error:
            raise job.KubeError(self.configmap_error[1], self.configmap_error[0])
        if self.extras is None:
            raise job.KubeError("GET /x: HTTP 404 reason=NotFound", 404)
        return {"kind": "ConfigMap", "data": {"registry.yaml": self.extras}}

    def endpoints(self, namespace, name):
        self.calls.append(("endpoints", namespace, name))
        if self.endpoints_error:
            raise job.KubeError(self.endpoints_error)
        return self.endpoints_doc

    def token(self, namespace, sa, audience, seconds=job.TOKEN_SECONDS):
        self.calls.append(("token", namespace, sa, audience, seconds))
        if self.token_error:
            raise job.KubeError(self.token_error)
        token = t4.sa_token(sa, audience, ttl=seconds)
        self.issued.append(token)
        return token


class Router(object):
    """probe.http_request in front of the emulators: a pod IP URL goes to that replica's emulator."""

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def __call__(self, method, url, data=None, headers=None, timeout=15.0):
        self.seen.append(url)
        for prefix, target in self.routes.items():
            if url.startswith(prefix + "/"):
                return probe.http_request(method, target + url[len(prefix) :], data, headers, timeout)
        return probe.Response(0, {}, b"URLError")


class OtherBase(t4.Emulator):
    def metrics(self):
        return super(OtherBase, self).metrics().replace('base_sha="%s"' % ("b" * 64), 'base_sha="%s"' % ("c" * 64))


class JobRun(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phase4-job-")
        self.work = os.path.join(self.dir, "work")
        os.mkdir(self.work)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def emulators(self, a=None, b=None):
        ea = a or t4.Emulator(self.dir)
        eb = b or t4.Emulator(self.dir)
        self.addCleanup(ea.close)
        self.addCleanup(eb.close)
        self.emus = (ea, eb)
        return Router({"http://%s:5000" % IP_A: ea.url, "http://%s:5000" % IP_B: eb.url})

    def run_job(self, kube, router, extra_args=(), gatekeeper_namespace="strive-ailab"):
        out = io.StringIO()
        argv = ["--gatekeeper-namespace", gatekeeper_namespace, "--work-dir", self.work, "--revision", "rev-7"] + list(extra_args)
        rc = job.main(argv, kube=kube, out=out, http=router)
        text = out.getvalue()
        for secret in kube.issued + [m for e in getattr(self, "emus", ()) for m in e.minted]:
            self.assertNotIn(secret, text)
        # No token, and not even a token's signature segment alone.
        for token in kube.issued:
            self.assertNotIn(token.rsplit(".", 1)[1], text)
        return rc, text

    def summary_row(self, text, pod):
        rows = [l for l in text.splitlines() if l.strip().startswith(pod + " ")]
        self.assertEqual(len(rows), 1, text)
        return rows[0].split()

    def test_every_replica_passes(self):
        kube, router = FakeKube(), self.emulators()
        rc, text = self.run_job(kube, router)
        self.assertEqual(rc, 0, text)
        self.assertTrue(text.rstrip().endswith("RESULT pass"), text)
        self.assertIn("revision=rev-7", text.splitlines()[0])
        for pod in (POD_A, POD_B):
            self.assertEqual(self.summary_row(text, pod)[2], "PASS")
            self.assertIn("  [%s] PASS k8s-mint[svc-mcp]" % pod, text)
            self.assertIn("  [%s] PASS registry" % pod, text)
            self.assertIn("  [%s] RESULT pass" % pod, text)
        self.assertIn("PASS registry-agreement", text)
        self.assertNotIn("OWNTOKEN", text)  # the Job's token file is its own SA, not gatekeeper's
        self.assertNotIn("gatekeeper's own token", text)  # skipped outright, not attempted
        # The Endpoints and the extras ConfigMap, then exactly the workstation script's three
        # TokenRequests.
        self.assertEqual(kube.calls[0], ("endpoints", "strive-ailab", "gatekeeper"))
        self.assertEqual(kube.calls[1], ("configmap", "strive-ailab", "gatekeeper-registry-extras"))
        with open(os.path.join(self.work, "registry.yaml"), "rb") as f:
            self.assertEqual(f.read(), t4.extras_text().encode("utf-8"))  # byte for byte
        self.assertEqual(
            sorted(kube.calls[2:]),
            sorted([
                ("token", "strive-ailab", "harness", "strive-gatekeeper", 600),
                ("token", "strive-ailab", "harness", "not-strive-gatekeeper", 600),
                ("token", "strive-ailab", "default", "strive-gatekeeper", 600),
            ]),
        )
        # Each replica was driven at its own pod IP, and only there.
        self.assertTrue(any(u.startswith("http://%s:5000/auth/token" % IP_A) for u in router.seen))
        self.assertTrue(any(u.startswith("http://%s:5000/auth/token" % IP_B) for u in router.seen))
        self.assertTrue(all(u.startswith(("http://%s:5000/" % IP_A, "http://%s:5000/" % IP_B)) for u in router.seen))
        # Both replicas reviewed the token themselves (the fresh-TokenReview proof).
        for emulator in self.emus:
            self.assertGreaterEqual(emulator.counters["authenticated"], 1)

    def test_one_bad_replica_fails_the_job_and_names_it(self):
        for fault, check in (("fallback", "refuse-preshared[svc-deepagent]"), ("wrong_sub", "k8s-mint[svc-mcp]"), ("d3_open", "d3-refused[svc-mcp]".replace("svc-mcp", "svc-profile")), ("stale_extras", "registry")):
            kube = FakeKube()
            router = self.emulators(b=t4.Emulator(self.dir, faults=[fault]))
            rc, text = self.run_job(kube, router)
            self.assertEqual(rc, 1, fault)
            self.assertTrue(text.rstrip().endswith("RESULT fail"), fault)
            self.assertEqual(self.summary_row(text, POD_A)[2], "PASS", fault)
            row = self.summary_row(text, POD_B)
            self.assertEqual(row[2], "FAIL", fault)
            self.assertIn(check, " ".join(row[3:]), fault)

    def test_the_configmap_now_is_the_reference(self):
        # The ConfigMap changed after both replicas booted: each loaded sha differs from what the
        # Job reads now -> registry FAIL on both ("roll gatekeeper").
        changed = t4.extras_text([t4.harness_entry(k8s_subject="system:serviceaccount:strive-ailab:other")])
        rc, text = self.run_job(FakeKube(extras=changed), self.emulators())
        self.assertEqual(rc, 1)
        self.assertIn("roll gatekeeper", text)
        self.assertIn("FAIL d3-policy", text)

    def test_an_absent_configmap_fails_registry(self):
        # A stale copy from an earlier run must not stand in for the absent ConfigMap.
        with open(os.path.join(self.work, "registry.yaml"), "w") as f:
            f.write(t4.extras_text())
        rc, text = self.run_job(FakeKube(extras=None), self.emulators())
        self.assertEqual(rc, 1, text)
        self.assertIn("configmap strive-ailab/gatekeeper-registry-extras is absent", text)
        self.assertIn("  [%s] FAIL registry" % POD_A, text)
        self.assertFalse(os.path.exists(os.path.join(self.work, "registry.yaml")))

    def test_a_denied_configmap_read_mints_nothing(self):
        kube = FakeKube(configmap_error=(403, "GET /x: HTTP 403 reason=Forbidden"))
        router = self.emulators()
        rc, text = self.run_job(kube, router)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL extras: GET /x: HTTP 403 reason=Forbidden", text)
        self.assertEqual([c for c in kube.calls if c[0] == "token"], [])
        self.assertEqual(router.seen, [])

    def test_gatekeeper_in_its_own_namespace(self):
        # After the A.2 move: Endpoints and ConfigMap come from strive-gatekeeper, the tokens still
        # from strive-ailab (where the harness and default ServiceAccounts live).
        kube = FakeKube()
        rc, text = self.run_job(kube, self.emulators(), gatekeeper_namespace="strive-gatekeeper")
        self.assertEqual(rc, 0, text)
        self.assertIn("namespace=strive-ailab gatekeeper_namespace=strive-gatekeeper", text.splitlines()[0])
        self.assertEqual(kube.calls[0], ("endpoints", "strive-gatekeeper", "gatekeeper"))
        self.assertEqual(kube.calls[1], ("configmap", "strive-gatekeeper", "gatekeeper-registry-extras"))
        self.assertEqual(set(c[1] for c in kube.calls if c[0] == "token"), {"strive-ailab"})

    def test_the_gatekeeper_namespace_is_required(self):
        saved = os.environ.pop("GATEKEEPER_NAMESPACE", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "GATEKEEPER_NAMESPACE", saved)
        out = io.StringIO()
        kube = FakeKube()
        self.assertEqual(job.main(["--work-dir", self.work], kube=kube, out=out), 2)
        self.assertIn("GATEKEEPER_NAMESPACE", out.getvalue())
        self.assertEqual(kube.calls, [])
        # The env var is how the Job passes it.
        os.environ["GATEKEEPER_NAMESPACE"] = "strive-gatekeeper"
        self.addCleanup(os.environ.pop, "GATEKEEPER_NAMESPACE", None)
        self.assertEqual(job.parse_args([]).gatekeeper_namespace, "strive-gatekeeper")

    def test_base_sha_disagreement_fails(self):
        rc, text = self.run_job(FakeKube(), self.emulators(b=OtherBase(self.dir)))
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL registry-agreement", text)

    def test_a_roll_in_flight_mints_nothing(self):
        endpoints = json.loads(json.dumps(LIVE_ENDPOINTS))
        endpoints["subsets"][0]["notReadyAddresses"] = [{"ip": "10.244.2.9", "targetRef": {"kind": "Pod", "name": "gatekeeper-new-x"}}]
        kube = FakeKube(endpoints=endpoints)
        router = self.emulators()
        rc, text = self.run_job(kube, router)
        self.assertEqual(rc, 1)
        self.assertIn("gatekeeper-new-x is not ready", text)
        self.assertEqual([c for c in kube.calls if c[0] == "token"], [])
        self.assertEqual(router.seen, [])
        self.assertIn("no replica probed", text)

    def test_too_few_replicas_mints_nothing(self):
        endpoints = json.loads(json.dumps(LIVE_ENDPOINTS))
        del endpoints["subsets"][0]["addresses"][1]
        kube = FakeKube(endpoints=endpoints)
        rc, text = self.run_job(kube, self.emulators())
        self.assertEqual(rc, 1)
        self.assertIn("1 ready gatekeeper replica(s), expected 2", text)
        self.assertEqual([c for c in kube.calls if c[0] == "token"], [])
        # --expect-replicas 1 accepts it (a deliberately scaled-down fleet).
        rc, text = self.run_job(FakeKube(endpoints=endpoints), self.emulators(), ["--expect-replicas", "1"])
        self.assertEqual(rc, 0, text)

    def test_no_endpoints_at_all(self):
        kube = FakeKube(endpoints={"kind": "Endpoints"})
        rc, text = self.run_job(kube, self.emulators())
        self.assertEqual(rc, 1)
        self.assertIn("0 ready gatekeeper replica(s)", text)
        rc, text = self.run_job(FakeKube(endpoints_error="GET /x: HTTP 403 reason=Forbidden"), self.emulators())
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 403 reason=Forbidden", text)

    def test_an_api_error_is_redacted(self):
        leak = t4.sa_token("harness", "strive-gatekeeper")
        rc, text = self.run_job(FakeKube(endpoints_error="GET /x: HTTP 500 " + leak), self.emulators())
        self.assertEqual(rc, 1)
        self.assertNotIn(leak, text)
        self.assertIn("<redacted-jwt>", text)

    def test_nothing_probed_is_never_a_pass(self):
        rc, text = self.run_job(FakeKube(endpoints={"kind": "Endpoints"}), self.emulators(), ["--expect-replicas", "0"])
        self.assertEqual(rc, 1, text)
        self.assertTrue(text.rstrip().endswith("RESULT fail"), text)

    def test_a_probe_that_does_not_finish_fails_its_replica(self):
        # No FAIL line, no RESULT line, a non-zero exit (e.g. a bad invocation): still a FAIL.
        real = job.probe.main

        def broken(argv, stdin=None, out=None, http=None, pod_ip_ok=False):
            out.write("ERROR something\n")
            return 2

        job.probe.main = broken
        self.addCleanup(setattr, job.probe, "main", real)
        rc, text = self.run_job(FakeKube(), self.emulators())
        self.assertEqual(rc, 1, text)
        self.assertEqual(self.summary_row(text, POD_A)[2:], ["FAIL", "exit", "2"])

    def test_a_denied_tokenrequest_probes_nothing(self):
        kube = FakeKube(token_error="POST /api/v1/namespaces/strive-ailab/serviceaccounts/harness/token: HTTP 403 reason=Forbidden")
        router = self.emulators()
        rc, text = self.run_job(kube, router)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL token harness", text)
        self.assertEqual(router.seen, [])
        self.assertTrue(text.rstrip().endswith("RESULT fail"))

    def test_an_unexpected_error_is_a_fail_without_a_traceback(self):
        class Boom(FakeKube):
            def endpoints(self, namespace, name):
                raise RuntimeError("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig-material")

        out = io.StringIO()
        rc = job.main(["--gatekeeper-namespace", "strive-ailab", "--work-dir", self.work], kube=Boom(), out=out)
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertTrue(text.endswith("FAIL internal: unexpected RuntimeError\nRESULT fail\n"), text)
        self.assertNotIn("sig-material", text)
        self.assertNotIn("Traceback", text)

    def test_outside_a_pod_is_a_bad_invocation(self):
        saved = {k: os.environ.pop(k) for k in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT") if k in os.environ}
        self.addCleanup(os.environ.update, saved)
        out = io.StringIO()
        self.assertEqual(job.main(["--gatekeeper-namespace", "strive-ailab"], out=out), 2)
        self.assertIn("not in a pod", out.getvalue())


class EndpointsParsing(unittest.TestCase):
    def test_live_shape(self):
        replicas, problems = job.replicas_from_endpoints(LIVE_ENDPOINTS)
        self.assertEqual(problems, [])
        self.assertEqual(replicas, sorted([(POD_A, IP_A, 5000), (POD_B, IP_B, 5000)]))

    def test_port_choice_and_bad_addresses(self):
        doc = {"subsets": [
            {"addresses": [{"ip": "10.0.0.1"}], "ports": [{"name": "metrics", "port": 9}, {"name": "http", "port": 5000}]},
            {"addresses": [{"ip": "10.0.0.2"}], "ports": [{"name": "metrics", "port": 9}, {"name": "grpc", "port": 7}]},
            {"addresses": [{"ip": "not-an-ip"}], "ports": [{"port": 5000}]},
        ]}
        replicas, problems = job.replicas_from_endpoints(doc)
        self.assertEqual(replicas, [("10.0.0.1", "10.0.0.1", 5000)])
        self.assertEqual(len(problems), 2, problems)

    def test_base_url(self):
        self.assertEqual(job.base_url("10.244.0.29", 5000), "http://10.244.0.29:5000")
        self.assertEqual(job.base_url("fd00::1", 5000), "http://[fd00::1]:5000")
        self.assertTrue(probe.is_pod_ip_http(job.base_url("fd00::1", 5000)))


class PodIpOpening(unittest.TestCase):
    """phase4_probe.main admits a pod IP only through the Python call, never argv alone."""

    def test_is_pod_ip_http(self):
        for url in ("http://10.244.0.29:5000", "http://10.244.0.29:5000/", "http://192.168.1.2:5000", "http://[fd00::1]:5000"):
            self.assertTrue(probe.is_pod_ip_http(url), url)
        for url in (
            "https://10.244.0.29:5000",
            "http://10.244.0.29",
            "http://10.244.0.29:5000/auth",
            "http://10.244.0.29:5000?x=1",
            "http://user@10.244.0.29:5000",
            "http://8.8.8.8:5000",
            "http://gatekeeper:5000",
            "http://0.0.0.0:5000",
            "http://10.244.0.29:notaport",
        ):
            self.assertFalse(probe.is_pod_ip_http(url), url)

    def test_main_guards(self):
        stdin = io.StringIO("")
        out = io.StringIO()
        self.assertEqual(probe.main(["--base-url", "http://10.1.2.3:5000"], stdin=stdin, out=out), 2)
        self.assertIn("loopback", out.getvalue())
        out = io.StringIO()
        self.assertEqual(probe.main(["--base-url", "http://8.8.8.8:5000"], stdin=io.StringIO(""), out=out, pod_ip_ok=True), 2)
        out = io.StringIO()
        # Admitted: it gets as far as the stdin check (no tokens -> FAIL stdin, exit 1).
        rc = probe.main(["--base-url", "http://10.1.2.3:5000"], stdin=io.StringIO(""), out=out, pod_ip_ok=True, http=lambda *a, **k: probe.Response(0, {}, b"x"))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL stdin", out.getvalue())


# ── The Kubernetes API client against a local HTTP server ──────────────────


class ApiStub(object):
    def __init__(self):
        self.requests = []
        self.answers = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, method):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append((method, self.path, dict((k.lower(), v) for k, v in self.headers.items()), body))
                status, payload = stub.answers.get((method, self.path), (404, {"kind": "Status", "reason": "NotFound"}))
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._answer("GET")

            def do_POST(self):
                self._answer("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class KubeClient(unittest.TestCase):
    TOKEN_PATH = "/api/v1/namespaces/strive-ailab/serviceaccounts/harness/token"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="phase4-kube-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.token_file = os.path.join(self.dir, "token")
        self.write_own("own-token-1")
        self.api = ApiStub()
        self.addCleanup(self.api.close)
        self.kube = job.Kube(self.api.url, self.token_file)

    def write_own(self, value):
        with open(self.token_file, "w") as f:
            f.write(value + "\n")

    def test_tokenrequest_shape_and_rotation(self):
        minted = t4.sa_token("harness", "strive-gatekeeper")
        self.api.answers[("POST", self.TOKEN_PATH)] = (201, {"kind": "TokenRequest", "status": {"token": minted, "expirationTimestamp": "x"}})
        self.assertEqual(self.kube.token("strive-ailab", "harness", "strive-gatekeeper"), minted)
        method, path, headers, body = self.api.requests[-1]
        self.assertEqual((method, path), ("POST", self.TOKEN_PATH))
        self.assertEqual(headers["authorization"], "Bearer own-token-1")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(
            json.loads(body),
            {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest", "spec": {"audiences": ["strive-gatekeeper"], "expirationSeconds": 600}},
        )
        # The kubelet rotates the pod's token: every call re-reads the file.
        self.write_own("own-token-2")
        self.kube.token("strive-ailab", "harness", "strive-gatekeeper")
        self.assertEqual(self.api.requests[-1][2]["authorization"], "Bearer own-token-2")

    def test_errors_never_carry_a_body(self):
        leak = t4.sa_token("harness", "strive-gatekeeper")
        self.api.answers[("POST", self.TOKEN_PATH)] = (403, {"kind": "Status", "reason": "Forbidden", "message": "nope " + leak})
        with self.assertRaises(job.KubeError) as caught:
            self.kube.token("strive-ailab", "harness", "strive-gatekeeper")
        self.assertIn("HTTP 403 reason=Forbidden", str(caught.exception))
        self.assertNotIn(leak, str(caught.exception))
        self.assertNotIn("nope", str(caught.exception))
        # A reason that is not a plain word is not echoed either.
        self.api.answers[("POST", self.TOKEN_PATH)] = (500, {"reason": leak})
        with self.assertRaises(job.KubeError) as caught:
            self.kube.token("strive-ailab", "harness", "strive-gatekeeper")
        self.assertNotIn(leak, str(caught.exception))
        # 201 without a JWT, or not JSON at all.
        for payload in ({"status": {"token": "not a jwt"}}, {"status": "x"}, b"<html>"):
            self.api.answers[("POST", self.TOKEN_PATH)] = (201, payload)
            with self.assertRaises(job.KubeError) as caught:
                self.kube.token("strive-ailab", "harness", "strive-gatekeeper")
            self.assertNotIn("not a jwt", str(caught.exception))

    def test_configmap_get_and_status(self):
        path = "/api/v1/namespaces/strive-gatekeeper/configmaps/gatekeeper-registry-extras"
        self.api.answers[("GET", path)] = (200, {"kind": "ConfigMap", "data": {"registry.yaml": "x: y\n"}})
        self.assertEqual(self.kube.configmap("strive-gatekeeper", "gatekeeper-registry-extras")["data"], {"registry.yaml": "x: y\n"})
        self.assertEqual(self.api.requests[-1][:2], ("GET", path))
        with self.assertRaises(job.KubeError) as caught:
            self.kube.configmap("strive-gatekeeper", "missing")
        self.assertEqual(caught.exception.status, 404)

    def test_endpoints_get(self):
        path = "/api/v1/namespaces/strive-ailab/endpoints/gatekeeper"
        self.api.answers[("GET", path)] = (200, LIVE_ENDPOINTS)
        self.assertEqual(self.kube.endpoints("strive-ailab", "gatekeeper"), LIVE_ENDPOINTS)
        self.assertEqual(self.api.requests[-1][:2], ("GET", path))

    def test_no_answer(self):
        dead = job.Kube("http://127.0.0.1:9", self.token_file, timeout=2)
        with self.assertRaises(job.KubeError) as caught:
            dead.endpoints("strive-ailab", "gatekeeper")
        self.assertIn("no answer", str(caught.exception))

    def test_in_cluster_config(self):
        env = {"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443"}
        with self.assertRaises(job.KubeError) as caught:  # no ca.crt: a clean error, not a traceback
            job.Kube.in_cluster(sa_dir=self.dir, environ=env)
        self.assertIn("cluster CA", str(caught.exception))
        bundle = ssl.get_default_verify_paths().cafile or "/etc/ssl/certs/ca-certificates.crt"
        if not os.path.exists(bundle):
            self.skipTest("no system CA bundle to stand in for the cluster CA")
        shutil.copy(bundle, os.path.join(self.dir, "ca.crt"))
        kube = job.Kube.in_cluster(sa_dir=self.dir, environ={"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443"})
        self.assertEqual(kube.base_url, "https://10.96.0.1:443")
        self.assertEqual(kube.token_file, os.path.join(self.dir, "token"))
        with self.assertRaises(job.KubeError):
            job.Kube.in_cluster(sa_dir=self.dir, environ={})


# ── The stdlib extras reader ────────────────────────────────────────────────


class YamlSubset(unittest.TestCase):
    def test_the_live_extras(self):
        doc = probe.parse_yaml_subset(LIVE_EXTRAS)
        entry = doc["services"][0]
        self.assertEqual(entry["client_id"], "svc-harness")
        self.assertEqual(entry["k8s_subject"], "system:serviceaccount:strive-ailab:harness")
        self.assertEqual(entry["audiences"]["svc-mcp"], {"grant_types": ["client_credentials", "token_exchange"], "scopes": ["mcp:read", "mcp:write"]})
        self.assertEqual(entry["may_act_for_audiences"], ["svc-mcp", "svc-integration", "svc-airlock", "svc-workflow", "svc-knowledge", "svc-profile"])
        policy = probe.harness_policy(doc)
        self.assertEqual(policy["problems"], [])
        self.assertEqual(policy["scopes"], ["mcp:read", "mcp:write"])
        self.assertEqual(sorted(policy["tx_only"]), sorted(probe.D3_TX_ONLY_AUDIENCES))

    def test_same_as_pyyaml_when_present(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML absent: the reader is checked against the literal live document above")
        for text in (LIVE_EXTRAS, yaml.safe_dump({"services": [t4.harness_entry(), {"client_id": "svc-other", "auth_method": "k8s", "k8s_subject": "a:b"}]})):
            self.assertEqual(probe.parse_yaml_subset(text), yaml.safe_load(text))

    def test_json_and_quoting(self):
        self.assertEqual(probe.parse_yaml_subset('{"services": []}'), {"services": []})
        text = "# head\n---\na: 'it''s'\nb: \"x\\ty\"\nc: []\nd:\n  - e: f\n    g: h\n  - i\n"
        self.assertEqual(probe.parse_yaml_subset(text), {"a": "it's", "b": "x\ty", "c": [], "d": [{"e": "f", "g": "h"}, "i"]})

    def test_rejects_what_it_cannot_read_exactly(self):
        for text in (
            "a: {b: c}\n",  # flow mapping
            "a: [b, c]\n",  # flow sequence
            "a: &x b\n",  # anchor
            "a: !tag b\n",  # tag
            "a: |\n  multi\n",  # block scalar
            "a: b # comment\n",  # inline comment
            "a:\n\tb: c\n",  # tab
            "a: b\n---\nc: d\n",  # two documents
            "a: b\n    c: d\n",  # stray indentation
            "a: b\na: c\n",  # duplicate key
            "a:b\n",  # not a mapping line
            "- a\nb: c\n",  # sequence then mapping at one level
        ):
            with self.assertRaises(ValueError, msg=text):
                probe.parse_yaml_subset(text)

    def test_unparsable_extras_fail_the_probe_without_pyyaml(self):
        saved = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # `import yaml` raises ImportError: the Job image's situation
        try:
            path = os.path.join(tempfile.mkdtemp(prefix="phase4-yaml-"), "registry.yaml")
            self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
            with open(path, "w") as f:
                f.write(LIVE_EXTRAS)
            doc, sha = probe.load_extras(path)
            self.assertEqual(probe.harness_policy(doc)["problems"], [])
            with open(path, "w") as f:
                f.write("services: {flow: style}\n")
            doc, sha = probe.load_extras(path)
            self.assertIsNone(doc)
            self.assertIsNotNone(sha)
            emulator = t4.Emulator(os.path.dirname(path))
            self.addCleanup(emulator.close)
            tokens = {"harness": t4.sa_token("harness", "strive-gatekeeper"), "wrong_aud": t4.sa_token("harness", "x"), "alt_sa": t4.sa_token("default", "strive-gatekeeper")}
            out = io.StringIO()
            rc = probe.main(
                ["--base-url", emulator.url, "--extras-file", path, "--own-token-file", "", "--no-registry-check"],
                stdin=io.StringIO("".join("%s=%s\n" % kv for kv in tokens.items())),
                out=out,
            )
            self.assertEqual(rc, 1)
            self.assertIn("FAIL d3-policy: the mounted extras file", out.getvalue())
        finally:
            if saved is None:
                del sys.modules["yaml"]
            else:
                sys.modules["yaml"] = saved


# ── The vendored copies and the manifests ───────────────────────────────────


def manifest_docs(path):
    docs = []
    for chunk in pathlib.Path(path).read_text(encoding="utf-8").split("\n---\n"):
        doc = probe.parse_yaml_subset(chunk)
        if doc is not None:
            docs.append(doc)
    return docs


def by_kind(docs, kind):
    found = [d for d in docs if d.get("kind") == kind]
    assert len(found) == 1, (kind, len(found))
    return found[0]


GK_TREE = TREE / "gatekeeper-ns"


def script_gk_ns():
    """GK_NS in phase4-probes.sh: the shared source for gatekeeper's namespace."""
    lines = [l for l in (HERE / "phase4-probes.sh").read_text(encoding="utf-8").splitlines() if l.startswith("GK_NS=")]
    assert len(lines) == 1, lines
    return lines[0].split("=", 1)[1]


class Vendored(unittest.TestCase):
    def test_copies_are_byte_identical(self):
        for name in ("phase4_probe.py", "phase4_job.py"):
            self.assertEqual((TREE / name).read_bytes(), (HERE / name).read_bytes(), "%s drifted: cp scripts/s2s/%s %s/" % (name, name, TREE.relative_to(REPO)))

    def test_the_generator_ships_both(self):
        kustomization = manifest_docs(TREE / "kustomization.yaml")[0]
        self.assertEqual(kustomization["resources"], ["rbac.yaml", "gatekeeper-ns", "job.yaml"])
        [gen] = kustomization["configMapGenerator"]
        self.assertEqual(gen["name"], "s2s-phase4-probe-script")
        self.assertEqual(gen["namespace"], "strive-ailab")
        self.assertEqual(sorted(gen["files"]), ["phase4_job.py", "phase4_probe.py"])


class Manifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rbac = manifest_docs(TREE / "rbac.yaml")
        cls.sa, cls.role, cls.binding = by_kind(rbac, "ServiceAccount"), by_kind(rbac, "Role"), by_kind(rbac, "RoleBinding")
        gk = manifest_docs(GK_TREE / "rbac.yaml")
        cls.gk_role, cls.gk_binding = by_kind(gk, "Role"), by_kind(gk, "RoleBinding")
        cls.gk_kustomization = manifest_docs(GK_TREE / "kustomization.yaml")[0]
        cls.netpol = by_kind(manifest_docs(GK_TREE / "networkpolicy.yaml"), "NetworkPolicy")
        cls.kustomization = manifest_docs(TREE / "kustomization.yaml")[0]
        cls.job = by_kind(manifest_docs(TREE / "job.yaml"), "Job")
        cls.pod = cls.job["spec"]["template"]

    def test_token_role_is_exactly_one_rule(self):
        self.assertEqual(
            self.role["rules"],
            [{"apiGroups": [""], "resources": ["serviceaccounts/token"], "resourceNames": ["harness", "default"], "verbs": ["create"]}],
        )
        self.assertEqual(self.role["metadata"]["namespace"], "strive-ailab")

    def test_gatekeeper_role_is_exactly_two_gets(self):
        self.assertEqual(
            self.gk_role["rules"],
            [
                {"apiGroups": [""], "resources": ["endpoints"], "resourceNames": ["gatekeeper"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [job.EXTRAS_CM], "verbs": ["get"]},
            ],
        )
        self.assertEqual(self.gk_binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": self.gk_role["metadata"]["name"]})
        # The subject is pinned to strive-ailab: it must not follow the Role to gatekeeper's namespace.
        self.assertEqual(self.gk_binding["subjects"], [{"kind": "ServiceAccount", "name": "s2s-phase4-probe", "namespace": "strive-ailab"}])

    def test_gatekeeper_namespace_is_one_line_shared_with_the_script(self):
        self.assertEqual(self.gk_kustomization["namespace"], script_gk_ns())
        self.assertEqual(self.gk_kustomization["resources"], ["rbac.yaml", "networkpolicy.yaml"])
        # No object below sets its own namespace: the kustomization's line is the only one.
        for name in ("rbac.yaml", "networkpolicy.yaml"):
            for doc in manifest_docs(GK_TREE / name):
                self.assertNotIn("namespace", doc["metadata"], name)
        # The Job's GATEKEEPER_NAMESPACE follows that line through a kustomize replacement...
        self.assertEqual(
            self.kustomization["replacements"],
            [{
                "source": {"kind": "NetworkPolicy", "name": self.netpol["metadata"]["name"], "fieldPath": "metadata.namespace"},
                "targets": [{
                    "select": {"kind": "Job", "name": "s2s-phase4-probe"},
                    "fieldPaths": ["spec.template.spec.containers.[name=probe].env.[name=GATEKEEPER_NAMESPACE].value"],
                }],
            }],
        )
        # ...into a placeholder that is never a namespace, so a broken wiring cannot pass silently.
        [container] = self.pod["spec"]["containers"]
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(container["name"], "probe")
        self.assertEqual(env["GATEKEEPER_NAMESPACE"]["value"], "set-by-kustomize-replacement")

    def test_binding_and_service_account(self):
        self.assertEqual(self.binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "s2s-phase4-probe"})
        self.assertEqual(self.binding["subjects"], [{"kind": "ServiceAccount", "name": "s2s-phase4-probe", "namespace": "strive-ailab"}])
        self.assertEqual(self.sa["metadata"]["name"], "s2s-phase4-probe")
        self.assertEqual(self.sa["automountServiceAccountToken"], "false")
        self.assertEqual(self.pod["spec"]["serviceAccountName"], "s2s-phase4-probe")
        self.assertEqual(self.pod["spec"]["automountServiceAccountToken"], "true")

    def test_job_shape(self):
        spec = self.job["spec"]
        self.assertEqual(self.job["metadata"]["namespace"], "strive-ailab")
        self.assertEqual(self.job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"], "enabled")
        self.assertEqual(spec["backoffLimit"], "0")
        self.assertGreaterEqual(int(spec["ttlSecondsAfterFinished"]), 86400)
        self.assertLessEqual(int(spec["activeDeadlineSeconds"]), 900)
        self.assertTrue(self.pod["metadata"]["annotations"]["probe/revision"])
        pod = self.pod["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["securityContext"]["runAsNonRoot"], "true")
        [container] = pod["containers"]
        sc = container["securityContext"]
        self.assertEqual(sc["readOnlyRootFilesystem"], "true")
        self.assertEqual(sc["allowPrivilegeEscalation"], "false")
        self.assertEqual(sc["privileged"], "false")
        self.assertEqual(sc["capabilities"], {"drop": ["ALL"]})
        self.assertRegex(container["image"], r"^mirror\.gcr\.io/library/python:3\.14-slim@sha256:[0-9a-f]{64}$")
        self.assertEqual(container["command"][:4], ["python3", "-u", "-B", "/probe/phase4_job.py"])
        self.assertEqual(
            [e for e in container["env"] if e["name"] == "PROBE_REVISION"][0]["valueFrom"]["fieldRef"]["fieldPath"],
            "metadata.annotations['probe/revision']",
        )
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(sorted(mounts), ["probe", "work"])
        self.assertEqual(sorted(mounts), sorted(volumes))
        self.assertEqual(mounts["probe"]["mountPath"], "/probe")
        self.assertEqual(mounts["probe"]["readOnly"], "true")
        self.assertEqual(volumes["probe"]["configMap"]["name"], "s2s-phase4-probe-script")
        # The extras copy goes to the one writable place, the Job's default --work-dir.
        self.assertEqual(mounts["work"]["mountPath"], job.parse_args(["--gatekeeper-namespace", "x"]).work_dir)
        self.assertIn("emptyDir", volumes["work"])

    def test_netpol_admits_exactly_the_probe_pod_to_gatekeeper(self):
        spec = self.netpol["spec"]
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        # Selects the gatekeeper Deployment's pods (live labels), not its job pods.
        live_gatekeeper = {"app.kubernetes.io/instance": "strive", "app.kubernetes.io/name": "gatekeeper", "strive.io/service": "gatekeeper"}
        selector = spec["podSelector"]["matchLabels"]
        self.assertTrue(set(selector.items()) <= set(live_gatekeeper.items()))
        self.assertEqual(selector["app.kubernetes.io/name"], "gatekeeper")
        [rule] = spec["ingress"]
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": "5000"}])
        # ONE source: the Job's namespace AND the Job's pod labels (one element = both must match).
        [source] = rule["from"]
        self.assertEqual(sorted(source), ["namespaceSelector", "podSelector"])
        self.assertEqual(source["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": self.job["metadata"]["namespace"]}})
        wanted = source["podSelector"]["matchLabels"]
        self.assertTrue(set(wanted.items()) <= set(self.pod["metadata"]["labels"].items()))
        # Not a label any platform policy trusts.
        self.assertNotIn("strive.io/service", self.pod["metadata"]["labels"])
        self.assertEqual(LIVE_ENDPOINTS["subsets"][0]["ports"][0]["port"], int(rule["ports"][0]["port"]))

    def test_flux_kustomization(self):
        ks = manifest_docs(FLUX_KS)[0]
        self.assertEqual(ks["spec"]["path"], "./" + str(TREE.relative_to(REPO)).replace(os.sep, "/"))
        self.assertEqual(ks["spec"]["wait"], "false")
        self.assertEqual(ks["spec"]["prune"], "true")


if __name__ == "__main__":
    unittest.main()
