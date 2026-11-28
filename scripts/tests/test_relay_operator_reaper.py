"""Tests for the relay-operator reaper (kubernetes/apps/apps/relay/operator-reaper*): the Lease script against a fake
Kubernetes API with resourceVersion conflicts, and the manifest's safety properties. The database function was checked
against a disposable Postgres 17 (setup idempotent; only the JIT subject's rows deleted; the role can do nothing else).
"""
import contextlib
import importlib.util
import io
import json
import subprocess
import os
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "kubernetes" / "apps" / "apps" / "relay"
REAPER = RELAY / "operator-reaper"


def load_lease(go_file):
    os.environ["REAPER_GO_FILE"] = go_file
    spec = importlib.util.spec_from_file_location("lease", REAPER / "lease.py")
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

    def test_absent_lease_is_taken_and_the_fence_is_its_expiry(self):
        api = FakeAPI()
        self.assertEqual(self.run_take(api), 0)
        self.assertTrue(os.path.exists(self.go))
        self.assertEqual(api.lease["spec"]["holderIdentity"], self.m.HOLDER)
        holder, fence = open(self.go).read().split(" ")
        self.assertEqual((holder, int(fence)), (self.m.HOLDER, int(self.m.expires(api.lease))))

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
        self.docs = [d for d in yaml.safe_load_all((REAPER / "reaper.yaml").read_text()) if d]
        self.cron = [d for d in self.docs if d["kind"] == "CronJob"][0]
        self.pod = self.cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    def kind(self, k):
        return [d for d in self.docs if d["kind"] == k]

    def test_no_setup_job_the_superuser_cannot_log_in_over_the_network(self):
        self.assertEqual(self.kind("Job"), [])  # scripts/relay-operator-reaper-setup.py, over the local socket
        np = self.kind("NetworkPolicy")[0]
        labels = [f["podSelector"]["matchLabels"]["app"] for f in np["spec"]["ingress"][0]["from"]]
        self.assertEqual(labels, ["relay-operator-reaper"])

    def test_reap_runs_only_after_the_lease_is_taken_and_images_are_pinned(self):
        spec = self.kind("CronJob")[0]["spec"]
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["lease-take", "reap"])
        self.assertIn("/work/go", pod["initContainers"][1]["command"][-1])
        for c in pod["initContainers"] + pod["containers"]:
            self.assertRegex(c["image"], r"@sha256:[0-9a-f]{64}$")
            self.assertFalse(c["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])

    def test_role_is_lease_only(self):
        rules = self.kind("Role")[0]["rules"]
        self.assertTrue(all(r["resources"] == ["leases"] for r in rules))
        named = [r for r in rules if "resourceNames" in r]
        self.assertEqual(named[0]["resourceNames"], ["relay-operator-jit"])

    def test_setup_sql_pins_the_subject(self):
        sql = (REAPER / "setup.sql").read_text()
        self.assertEqual(sql.count("subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f'"),
                         2)
        self.assertIn("SECURITY DEFINER", sql)
        self.assertIn("REVOKE ALL ON FUNCTION ops.relay_operator_reap(bigint) FROM PUBLIC", sql)
        self.assertIn("DROP FUNCTION IF EXISTS ops.relay_operator_reap();", sql)  # no unfenced overload remains

    def test_the_reap_is_fenced_by_the_reapers_own_lease_expiry(self):
        sql = (REAPER / "setup.sql").read_text()
        self.assertIn("extract(epoch FROM clock_timestamp()) > fence - 30", sql)
        reap = [c for c in self.pod["initContainers"] if c["name"] == "reap"][0]
        self.assertIn("ops.relay_operator_reap(:fence)", reap["command"][-1])
        self.assertIn("cut -d' ' -f2 /work/go", reap["command"][-1])

    def test_the_hold_outlives_any_execution_of_the_job(self):
        m = load_lease(os.path.join(tempfile.mkdtemp(), "go"))
        job = self.cron["spec"]["jobTemplate"]["spec"]
        grace = self.pod["terminationGracePeriodSeconds"]
        # deadline + grace + the SQL fence's 30 s margin: a reap can never run past the hold it was fenced by
        self.assertGreaterEqual(m.HOLD_S, job["activeDeadlineSeconds"] + grace + 30)

    def test_the_api_token_reaches_only_the_lease_containers(self):
        self.assertFalse(self.pod["automountServiceAccountToken"])
        mounts = {c["name"]: [v["name"] for v in c.get("volumeMounts", [])]
                  for c in self.pod["initContainers"] + self.pod["containers"]}
        self.assertIn("kube-api", mounts["lease-take"])
        self.assertIn("kube-api", mounts["lease-release"])
        self.assertNotIn("kube-api", mounts["reap"])

    def test_the_setup_script_sends_the_password_only_on_stdin(self):
        import base64
        spec = importlib.util.spec_from_file_location("setup", ROOT / "scripts" / "relay-operator-reaper-setup.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        pw = "Zq9_-abcdefghijklmnopqrstuvwxyz0123456789AB"
        seen = []

        class P:
            def __init__(self, out=b"", rc=0, err=b""):
                self.stdout, self.returncode, self.stderr = out, rc, err

        def run(argv, input=None, capture_output=True, timeout=None):
            seen.append((argv, input))
            if "secret" in argv:
                return P(json.dumps({"data": {"password": base64.b64encode(pw.encode()).decode()}}).encode())
            return P(b"reaper objects ready: 1\n")
        self.assertEqual(m.main(run), 0)
        self.assertFalse(any(pw in " ".join(a) for a, _ in seen))  # never on argv
        stdin = seen[-1][1].decode()
        self.assertTrue(stdin.startswith("SET log_statement = 'none';"))  # logging off before the password
        self.assertIn("\\set pw '" + pw + "'", stdin)
        self.assertNotIn("\\getenv pw", stdin)
        self.assertIn("-c", seen[-1][0])  # into the postgres container, over its local socket
        self.assertNotIn("host=", " ".join(seen[-1][0]))

        def failing(argv, input=None, capture_output=True, timeout=None):
            if "secret" in argv:
                return run(argv, input)
            return P(b"", 3, ("ERROR: x\nLINE 1: ALTER ROLE relay_operator_reaper PASSWORD '" + pw + "'").encode())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(m.main(failing), 1)
        self.assertNotIn(pw, err.getvalue())  # redacted from a failing statement's echo

        def truncated(argv, input=None, capture_output=True, timeout=None):  # psql's LINE display cuts the value
            if "secret" in argv:
                return run(argv, input)
            return P(b"", 3, ("ERROR:  syntax error\nLINE 1: ...lay_operator_reaper PASSWORD '" + pw[:40] + "...\n"
                              "        ^").encode())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(m.main(truncated), 1)
        self.assertNotIn(pw[:20], err.getvalue())  # no fragment survives
        self.assertIn("ERROR:  syntax error", err.getvalue())

        def hung(argv, input=None, capture_output=True, timeout=None):
            if "secret" in argv:
                return run(argv, input)
            raise subprocess.TimeoutExpired(argv, timeout)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(m.main(hung), 1)  # one line, no traceback
        self.assertIn("TimeoutExpired", err.getvalue())

    def test_the_stale_alert_also_fires_for_a_reaper_that_never_succeeded(self):
        rule = yaml.safe_load((REAPER / "alerts.yaml").read_text())
        stale = [r for g in rule["spec"]["groups"] for r in g["rules"] if r["alert"] == "RelayOperatorReaperStale"][0]
        self.assertIn("absent(kube_cronjob_status_last_successful_time", stale["expr"])

    def test_generator_has_a_namespace(self):
        relay = yaml.safe_load((RELAY / "kustomization.yaml").read_text())
        self.assertEqual(set(relay) - {"apiVersion", "kind", "resources", "components"}, set())  # overlay helpers
        self.assertIn("operator-reaper", relay["resources"])
        k = yaml.safe_load((REAPER / "kustomization.yaml").read_text())
        self.assertEqual(k["namespace"], "relay")
        self.assertEqual([g["name"] for g in k["configMapGenerator"]], ["relay-operator-reaper-scripts"])


if __name__ == "__main__":
    unittest.main()
