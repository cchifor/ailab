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
import subprocess
import threading
from unittest.mock import patch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "kubernetes" / "apps" / "apps" / "relay"
REAPER = RELAY / "operator-reaper"


def load_lease(go_file):
    spec = importlib.util.spec_from_file_location("lease", REAPER / "lease.py")
    m = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"REAPER_GO_FILE": go_file}):
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
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.go = os.path.join(tmp, "go")
        self.m = load_lease(self.go)

    def run_take(self, api):
        self.m.api = api
        return self.m.take()

    def test_absent_lease_is_taken_and_the_fence_is_its_expiry(self):
        api = FakeAPI()
        self.assertEqual(self.run_take(api), 0)
        self.assertTrue(os.path.exists(self.go))
        self.assertEqual(api.lease["spec"]["holderIdentity"], self.m.HOLDER)
        record = json.loads(Path(self.go).read_text())
        self.assertEqual((record["holder"], record["expiresAt"]),
                         (self.m.HOLDER, int(self.m.expires(api.lease))))

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

    def test_expired_acquisition_readback_never_authorizes_reap(self):
        api = FakeAPI()
        original = api.__call__

        def delayed(method, url, body=None):
            result = original(method, url, body)
            if method == "GET" and result[0] == 200:
                result[1]["metadata"]["annotations"]["chifor.me/expires-at"] = "1"
            return result

        self.m.api = delayed
        self.assertNotEqual(self.m.take(), 0)
        self.assertFalse(os.path.exists(self.go))

    def test_permit_rechecks_exact_lease_and_never_renews(self):
        api = FakeAPI()
        self.run_take(api)
        record = json.loads(Path(self.go).read_text())
        self.assertEqual(self.m.permit(record), record["expiresAt"])
        self.assertEqual(api.calls[-1], "GET")
        original = json.loads(json.dumps(api.lease))
        for mutate in (
            lambda row: row["spec"].update(holderIdentity="successor"),
            lambda row: row["metadata"].update(resourceVersion="other"),
            lambda row: row["metadata"]["annotations"].update({"chifor.me/expires-at": str(record["expiresAt"] + 1)}),
        ):
            with self.subTest(mutate=mutate):
                api.lease = json.loads(json.dumps(original))
                mutate(api.lease)
                before = len(api.calls)
                with self.assertRaises(ValueError):
                    self.m.permit(record)
                self.assertEqual(api.calls[before:], ["GET"])
        api.lease = original
        with patch.object(self.m.time, "time", return_value=record["expiresAt"] - 44):
            with self.assertRaises(ValueError):
                self.m.permit(record)
        with patch.object(self.m.time, "time", return_value=record["expiresAt"] + 1):
            with self.assertRaises(ValueError):
                self.m.permit(record)

    def test_malformed_held_expiry_never_allows_takeover(self):
        for expiry in ("NaN", "Infinity", "-1", "1.5", "bad", "0"):
            api = FakeAPI(lease_of("other", 1))
            api.lease["metadata"]["annotations"]["chifor.me/expires-at"] = expiry
            self.m.api = api
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                self.m.take()
            self.assertEqual(api.calls, ["GET"])
            self.assertFalse(Path(self.go).exists())

    def test_delayed_guard_request_refuses_without_permit_or_release(self):
        api = FakeAPI()
        self.run_take(api)
        record = json.loads(Path(self.go).read_text())
        Path(self.go).with_name("request").touch()
        with patch.object(self.m.time, "time", return_value=record["expiresAt"] + 1):
            with self.assertRaises(ValueError):
                self.m.guard()
        self.assertFalse(Path(self.go).with_name("permit").exists())
        self.assertEqual(api.lease["spec"]["holderIdentity"], self.m.HOLDER)

    def test_actual_shell_and_guard_handshake(self):
        api = FakeAPI()
        self.run_take(api)
        root = Path(self.go).parent
        executable = root / "psql"
        executable.write_text("#!/bin/sh\ncat > \"$CAPTURE\"\nprintf '1 1\\n'\n")
        executable.chmod(0o755)
        docs = list(yaml.safe_load_all((REAPER / "reaper.yaml").read_text()))
        pod = next(d for d in docs if d["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        command = next(c for c in pod["containers"] if c["name"] == "reap")["command"][-1]
        command = command.replace("/work/", str(root) + "/")
        result = []
        worker = threading.Thread(target=lambda: result.append(self.m.guard()), daemon=True)
        worker.start()
        env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"], "CAPTURE": str(root / "query")}
        child = subprocess.run(["sh", "-c", command], env=env, capture_output=True, timeout=10)
        worker.join(3)
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])
        self.assertEqual(api.lease["spec"]["holderIdentity"], "")
        original = json.loads(Path(self.go).read_text())["expiresAt"]
        self.assertIn(f"ops.relay_operator_reap({original})", (root / "query").read_text())

    def test_failed_sql_reply_does_not_release_lease(self):
        api = FakeAPI()
        self.run_take(api)
        root = Path(self.go).parent
        (root / "permit").write_text(str(json.loads(Path(self.go).read_text())["expiresAt"]) + "\n")
        executable = root / "psql"
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o755)
        docs = list(yaml.safe_load_all((REAPER / "reaper.yaml").read_text()))
        pod = next(d for d in docs if d["kind"] == "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        command = next(c for c in pod["containers"] if c["name"] == "reap")["command"][-1]
        child = subprocess.run(["sh", "-c", command.replace("/work/", str(root) + "/")],
                               env={**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]},
                               capture_output=True, timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertFalse((root / "done").exists())
        with patch.object(self.m.time, "monotonic", side_effect=[0, 111]):
            with self.assertRaises(ValueError):
                self.m.guard()
        self.assertEqual(api.lease["spec"]["holderIdentity"], self.m.HOLDER)

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
        Path(self.go).write_text("x")
        self.assertEqual(self.m.release(), 0)
        self.assertEqual(api2.lease["spec"]["holderIdentity"], "session-abc")


class Manifest(unittest.TestCase):
    def setUp(self):
        self.docs = [d for d in yaml.safe_load_all((REAPER / "reaper.yaml").read_text()) if d]
        self.cron = self.kind("CronJob")[0]
        self.pod = self.cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    def kind(self, k):
        return [d for d in self.docs if d["kind"] == k]

    def test_setup_job_is_force_replaced(self):
        job = self.kind("Job")[0]
        self.assertEqual(job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"], "enabled")

    def test_password_is_not_a_psql_argument(self):
        command = self.kind("Job")[0]["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertNotIn('pw="$REAPER_PASSWORD"', " ".join(command))
        self.assertIn("\\getenv pw REAPER_PASSWORD", (REAPER / "setup.sql").read_text())

    def test_database_container_has_no_service_account_token(self):
        pod = self.kind("CronJob")[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        reap = next(c for c in pod["containers"] if c["name"] == "reap")
        self.assertNotIn("lease-api", [m["name"] for m in reap["volumeMounts"]])
        guard = next(c for c in pod["containers"] if c["name"] == "lease-guard")
        self.assertIn("lease-api", [m["name"] for m in guard["volumeMounts"]])
        self.assertFalse(guard.get("env"))
        self.assertEqual(pod["securityContext"]["fsGroup"], 65534)

    def test_reap_runs_only_after_the_lease_is_taken_and_images_are_pinned(self):
        spec = self.kind("CronJob")[0]["spec"]
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["lease-take"])
        self.assertEqual([c["name"] for c in pod["containers"]], ["reap", "lease-guard"])
        self.assertIn("/work/go", pod["containers"][0]["command"][-1])
        module = load_lease("unused")
        self.assertGreater(module.HOLD_S, spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] + pod["terminationGracePeriodSeconds"])
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
        sql = (REAPER / "setup.sql").read_text()
        self.assertEqual(sql.count("subject = 'https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f'"),
                         2)
        self.assertIn("SECURITY DEFINER", sql)
        self.assertIn("REVOKE ALL ON FUNCTION ops.relay_operator_reap(bigint) FROM PUBLIC", sql)
        self.assertIn("DROP FUNCTION IF EXISTS ops.relay_operator_reap();", sql)

    def test_generator_has_a_namespace(self):
        parent = yaml.safe_load((RELAY / "kustomization.yaml").read_text())
        self.assertIn("operator-reaper", parent["resources"])
        self.assertEqual(set(parent) - {"apiVersion", "kind", "resources", "components"}, set())
        self.assertNotIn("configMapGenerator", parent)
        k = yaml.safe_load((REAPER / "kustomization.yaml").read_text())
        gen = [g for g in k.get("configMapGenerator", []) if g["name"] == "relay-operator-reaper-scripts"][0]
        self.assertEqual(k["namespace"], "relay")
        self.assertEqual([g["name"] for g in k["configMapGenerator"]], ["relay-operator-reaper-scripts"])


    def test_the_reap_is_fenced_by_the_reapers_own_lease_expiry(self):
        sql = (REAPER / "setup.sql").read_text()
        self.assertIn("fence < extract(epoch FROM clock_timestamp()) + 30", sql)
        self.assertIn("extract(epoch FROM clock_timestamp()) >= fence", sql)
        reap = next(c for c in self.pod["containers"] if c["name"] == "reap")
        self.assertIn("/work/permit", reap["command"][-1])
        self.assertIn("ops.relay_operator_reap(%s)", reap["command"][-1])
        self.assertIn('"$lease_expires"', reap["command"][-1])

    def test_the_hold_outlives_any_execution_of_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_lease(os.path.join(tmp, "go"))
        job = self.cron["spec"]["jobTemplate"]["spec"]
        self.assertGreaterEqual(m.HOLD_S, job["activeDeadlineSeconds"] + self.pod["terminationGracePeriodSeconds"] + 30)

    def test_the_api_token_reaches_only_the_lease_containers(self):
        self.assertFalse(self.pod["automountServiceAccountToken"])
        mounts = {c["name"]: [v["name"] for v in c.get("volumeMounts", [])]
                  for c in self.pod["initContainers"] + self.pod["containers"]}
        self.assertIn("lease-api", mounts["lease-take"])
        self.assertIn("lease-api", mounts["lease-guard"])
        self.assertNotIn("lease-api", mounts["reap"])

    def test_the_setup_password_never_reaches_argv(self):
        job = self.kind("Job")[0]["spec"]["template"]["spec"]["containers"][0]
        self.assertNotIn("pw=", " ".join(job["command"]))
        self.assertIn("\\getenv pw REAPER_PASSWORD", (REAPER / "setup.sql").read_text())

    def test_the_stale_alert_also_fires_for_a_reaper_that_never_succeeded(self):
        rule = yaml.safe_load((REAPER / "alerts.yaml").read_text())
        alerts = {r["alert"]: r for g in rule["spec"]["groups"] for r in g["rules"]}
        self.assertIn("kube_cronjob_created", alerts["RelayOperatorReaperStale"]["expr"])
        self.assertIn("absent(kube_cronjob_status_last_successful_time", alerts["RelayOperatorReaperUnobserved"]["expr"])


if __name__ == "__main__":
    unittest.main()
