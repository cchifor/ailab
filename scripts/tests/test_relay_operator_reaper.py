"""Tests for the relay-operator reaper (kubernetes/apps/apps/relay/operator-reaper*): the Lease script against a fake
Kubernetes API with resourceVersion conflicts, and the manifest's safety properties. The database function was checked
against a disposable Postgres 17 (setup idempotent; only the JIT subject's rows deleted; the role can do nothing else).
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "kubernetes" / "apps" / "apps" / "relay"


def load_lease(go_file):
    os.environ["REAPER_GO_FILE"] = go_file
    spec = importlib.util.spec_from_file_location("lease", RELAY / "operator-reaper" / "lease.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeAPI:
    def __init__(self, lease=None):
        self.lease, self.rv, self.calls = lease, 1, []

    def __call__(self, method, url, body=None):
        self.calls.append(method)
        if method == "GET":
            return (200, json.loads(json.dumps(self.lease))) if self.lease else (404, None)
        if method == "POST":
            if self.lease:
                return 409, None
        elif method == "PUT":
            if not self.lease or body["metadata"].get("resourceVersion") != self.lease["metadata"]["resourceVersion"]:
                return 409, None
        self.rv += 1
        body["metadata"]["resourceVersion"] = str(self.rv)
        self.lease = body
        return (201 if method == "POST" else 200), body


def lease_of(holder, expires_at, rv="7"):
    return {"metadata": {"name": "relay-operator-jit", "resourceVersion": rv,
                         "annotations": {"chifor.me/expires-at": str(int(expires_at))}},
            "spec": {"holderIdentity": holder}}


class LeaseScript(unittest.TestCase):
    def setUp(self):
        self.go = os.path.join(tempfile.mkdtemp(), "go")
        self.m = load_lease(self.go)

    def run_take(self, api):
        self.m.api = api
        return self.m.take()

    def test_absent_lease_is_taken(self):
        api = FakeAPI()
        self.assertEqual(self.run_take(api), 0)
        self.assertTrue(os.path.exists(self.go))
        self.assertEqual(api.lease["spec"]["holderIdentity"], self.m.HOLDER)

    def test_live_lease_is_never_touched(self):
        api = FakeAPI(lease_of("session-abc", time.time() + 600))
        self.assertEqual(self.run_take(api), 0)
        self.assertFalse(os.path.exists(self.go))
        self.assertEqual(api.calls, ["GET"])
        self.assertEqual(api.lease["spec"]["holderIdentity"], "session-abc")

    def test_expired_and_released_leases_are_taken_over(self):
        for lease in (lease_of("session-old", time.time() - 5), lease_of("", 0)):
            api = FakeAPI(lease)
            if os.path.exists(self.go):
                os.remove(self.go)
            self.assertEqual(self.run_take(api), 0)
            self.assertTrue(os.path.exists(self.go))

    def test_lost_race_skips(self):
        api = FakeAPI(lease_of("session-old", time.time() - 5))
        real = api.__call__

        def racing(method, url, body=None):
            if method == "PUT":  # someone else wrote first: the resourceVersion moved
                api.lease["metadata"]["resourceVersion"] = "999"
            return real(method, url, body)
        self.m.api = racing
        self.assertEqual(self.m.take(), 0)
        self.assertFalse(os.path.exists(self.go))

    def test_release_only_its_own_lease(self):
        api = FakeAPI()
        self.run_take(api)
        self.m.api = api
        self.assertEqual(self.m.release(), 0)
        self.assertEqual(api.lease["spec"]["holderIdentity"], "")
        # without the go file (it never took the Lease) a release touches nothing
        os.remove(self.go)
        api2 = FakeAPI(lease_of("session-abc", time.time() + 600))
        self.m.api = api2
        self.assertEqual(self.m.release(), 0)
        self.assertEqual(api2.calls, [])
        # with the go file but a successor holding the Lease: nothing is written
        open(self.go, "w").write("x")
        self.assertEqual(self.m.release(), 0)
        self.assertEqual(api2.lease["spec"]["holderIdentity"], "session-abc")


class Manifest(unittest.TestCase):
    def setUp(self):
        self.docs = [d for d in yaml.safe_load_all((RELAY / "operator-reaper.yaml").read_text()) if d]

    def kind(self, k):
        return [d for d in self.docs if d["kind"] == k]

    def test_setup_job_is_force_replaced(self):
        job = self.kind("Job")[0]
        self.assertEqual(job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"], "enabled")

    def test_reap_runs_only_after_the_lease_is_taken_and_images_are_pinned(self):
        spec = self.kind("CronJob")[0]["spec"]
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["lease-take", "reap"])
        self.assertIn("/work/go", pod["initContainers"][1]["command"][-1])
        for c in pod["initContainers"] + pod["containers"] + self.kind("Job")[0]["spec"]["template"]["spec"]["containers"]:
            self.assertRegex(c["image"], r"@sha256:[0-9a-f]{64}$")
            self.assertFalse(c["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])

    def test_role_is_lease_only(self):
        rules = self.kind("Role")[0]["rules"]
        self.assertTrue(all(r["resources"] == ["leases"] for r in rules))
        named = [r for r in rules if "resourceNames" in r]
        self.assertEqual(named[0]["resourceNames"], ["relay-operator-jit"])

    def test_setup_sql_pins_the_subject(self):
        sql = (RELAY / "operator-reaper" / "setup.sql").read_text()
        self.assertEqual(sql.count("subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f'"),
                         2)
        self.assertIn("SECURITY DEFINER", sql)
        self.assertIn("REVOKE ALL ON FUNCTION ops.relay_operator_reap() FROM PUBLIC", sql)

    def test_generator_has_a_namespace(self):
        k = yaml.safe_load((RELAY / "kustomization.yaml").read_text())
        gen = [g for g in k.get("configMapGenerator", []) if g["name"] == "relay-operator-reaper-scripts"][0]
        self.assertEqual(gen["namespace"], "relay")


if __name__ == "__main__":
    unittest.main()
