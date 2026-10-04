#!/usr/bin/env python3
"""Decision logic of trueswarm-e2e-token-sync (kubernetes/apps/trueswarm-e2e-tokens/token_sync.py).

WHY THIS EXISTS. The sync is the only writer of the files the Trueswarm apps trust for their e2e
login (ADR 0035), and every failure mode is quiet: a wrong rotation decision either never rotates (a
leaked token lives on) or rotates every run (each run breaks the agents for a propagation window); a
dropped overlap entry logs out an in-flight Playwright run; an admin entry with the wrong role hands
an agent more than operator. These tests pin the pure functions; the I/O shell around them follows
openbao-k8stoken-sync line for line.

Run:

    python3 -m unittest scripts.tests.test_trueswarm_e2e_token_sync -v
"""
import base64
import importlib.util
import json
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
TREE = ROOT / "kubernetes" / "apps" / "trueswarm-e2e-tokens"

spec = importlib.util.spec_from_file_location("token_sync", TREE / "token_sync.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)  # main() only runs under __main__, so importing reads no env

NOW = 1_800_000_000
DAY = 86400
VALIDITY, ROTATE_BEFORE, OVERLAP = 14 * DAY, 7 * DAY, 3600


def entry_for(token, not_after):
    return {"name": "dev-worker-4", "tokens": [{"sha256": ts.sha256_hex(token), "not_after": not_after}]}


class PlanSlot(unittest.TestCase):
    def plan(self, published, until, entry, force=False):
        return ts.plan_slot(published, until, entry, NOW, ROTATE_BEFORE, force)

    def test_steady_state_keeps(self):
        tok = "tse2e.dev-worker-4.x"
        self.assertEqual(self.plan(tok, NOW + 10 * DAY, entry_for(tok, NOW + 10 * DAY)), (False, "current"))

    def test_nothing_published_rotates(self):
        self.assertTrue(self.plan(None, None, None)[0])
        self.assertTrue(self.plan("tse2e.dev-worker-4.x", None, None)[0])

    def test_due_inside_rotate_window(self):
        tok = "tse2e.dev-worker-4.x"
        self.assertEqual(self.plan(tok, NOW + 6 * DAY, entry_for(tok, NOW + 6 * DAY)), (True, "due"))

    def test_app_holding_another_current_token_rotates(self):
        # A run that died between the Secret write and the OpenBao patch: never guess, re-mint.
        self.assertEqual(self.plan("tse2e.dev-worker-4.old", NOW + 10 * DAY, entry_for("tse2e.dev-worker-4.new", NOW + 10 * DAY)),
                         (True, "app does not hold the published token"))

    def test_missing_app_entry_rotates(self):
        self.assertTrue(self.plan("tse2e.dev-worker-4.x", NOW + 10 * DAY, None)[0])

    def test_force(self):
        tok = "tse2e.dev-worker-4.x"
        self.assertEqual(self.plan(tok, NOW + 10 * DAY, entry_for(tok, NOW + 10 * DAY), force=True), (True, "forced"))


class BuildEntry(unittest.TestCase):
    def test_rotation_keeps_previous_for_overlap_only(self):
        old = entry_for("tse2e.dev-worker-4.old", NOW + 6 * DAY)
        e = ts.build_entry("dev-worker-4", "tse2e.dev-worker-4.new", NOW + VALIDITY, old, True, NOW, OVERLAP,
                           False, "operator", [])
        self.assertEqual(e["tokens"][0], {"sha256": ts.sha256_hex("tse2e.dev-worker-4.new"), "not_after": NOW + VALIDITY})
        self.assertEqual(e["tokens"][1], {"sha256": ts.sha256_hex("tse2e.dev-worker-4.old"), "not_after": NOW + OVERLAP})
        self.assertNotIn("role", e)
        self.assertNotIn("access_client_ids", e)

    def test_overlap_never_extends_a_token(self):
        old = entry_for("tse2e.dev-worker-4.old", NOW + 60)
        e = ts.build_entry("dev-worker-4", "tse2e.dev-worker-4.new", NOW + VALIDITY, old, True, NOW, OVERLAP,
                           False, "operator", [])
        self.assertEqual(e["tokens"][1]["not_after"], NOW + 60)

    def test_expired_overlap_is_dropped_on_a_later_run(self):
        cur = "tse2e.dev-worker-4.cur"
        live = {"name": "dev-worker-4", "tokens": [{"sha256": ts.sha256_hex(cur), "not_after": NOW + 10 * DAY},
                                                    {"sha256": "a" * 64, "not_after": NOW - 1}]}
        e = ts.build_entry("dev-worker-4", cur, NOW + 10 * DAY, live, False, NOW, OVERLAP, False, "operator", [])
        self.assertEqual(len(e["tokens"]), 1)

    def test_unexpired_overlap_survives_a_keep_run(self):
        cur = "tse2e.dev-worker-4.cur"
        live = {"name": "dev-worker-4", "tokens": [{"sha256": ts.sha256_hex(cur), "not_after": NOW + 10 * DAY},
                                                    {"sha256": "b" * 64, "not_after": NOW + 600}]}
        e = ts.build_entry("dev-worker-4", cur, NOW + 10 * DAY, live, False, NOW, OVERLAP, False, "operator", [])
        self.assertEqual([t["sha256"] for t in e["tokens"]], [ts.sha256_hex(cur), "b" * 64])

    def test_admin_entry_carries_role_and_access_ids(self):
        e = ts.build_entry("dev-worker-4", "tsadmine2e.dev-worker-4.x", NOW + VALIDITY, None, True, NOW, OVERLAP,
                           True, "operator", ["b.access", "a.access"])
        self.assertEqual(e["role"], "operator")
        self.assertEqual(e["access_client_ids"], ["a.access", "b.access"])


class Document(unittest.TestCase):
    def doc(self, admin, role="operator", names=("dev-worker-1", "dev-worker-4")):
        entries = [ts.build_entry(n, f"x.{n}.y", NOW + VALIDITY, None, True, NOW, OVERLAP, admin, role, [])
                   for n in names]
        return ts.build_document(entries)

    def test_valid_documents_pass(self):
        ts.validate_document(self.doc(False), ["dev-worker-1", "dev-worker-4"], False, "operator")
        ts.validate_document(self.doc(True), ["dev-worker-1", "dev-worker-4"], True, "operator")

    def test_principals_must_be_exactly_the_live_slots(self):
        with self.assertRaises(AssertionError):
            ts.validate_document(self.doc(False), ["dev-worker-1"], False, "operator")

    def test_administrator_is_never_a_valid_role(self):
        self.assertNotIn("administrator", ts.ADMIN_ROLES)
        with self.assertRaises(AssertionError):
            ts.validate_document(self.doc(True, role="administrator"), ["dev-worker-1", "dev-worker-4"], True,
                                 "administrator")

    def test_parse_refuses_an_unknown_version(self):
        with self.assertRaises(RuntimeError):
            ts.parse_document('{"version": 2, "principals": []}')
        self.assertEqual(ts.parse_document(None), {})

    def test_round_trip(self):
        doc = self.doc(True)
        parsed = ts.parse_document(__import__("json").dumps(doc))
        self.assertEqual(ts.build_document(list(parsed.values())), doc)


class Tokens(unittest.TestCase):
    def test_format_matches_the_app_contract(self):
        for app, (_, _, prefix, _) in ts.APPS.items():
            tok = ts.new_token(prefix, "dev-worker-4")
            parts = tok.split(".")
            self.assertEqual(len(parts), 3, app)
            self.assertEqual(parts[:2], [prefix, "dev-worker-4"])
            self.assertRegex(parts[2], r"^[A-Za-z0-9_-]{40,}$")

    def test_prefixes_differ_per_app(self):
        prefixes = [p for (_, _, p, _) in ts.APPS.values()]
        self.assertEqual(len(prefixes), len(set(prefixes)))


class Manifests(unittest.TestCase):
    """Both copies of the env agree, and the script the CronJob runs is the one tested here."""

    def docs(self):
        return list(yaml.safe_load_all((TREE / "token-sync.yaml").read_text(encoding="utf-8")))

    def envs(self):
        out = []
        for d in self.docs():
            spec = d["spec"]["jobTemplate"]["spec"] if d["kind"] == "CronJob" else d["spec"]
            container = spec["template"]["spec"]["containers"][0]
            out.append({e["name"]: e["value"] for e in container["env"]})
            self.assertEqual(container["command"][-1], "/scripts/token_sync.py")
        return out

    def test_cronjob_and_bootstrap_agree(self):
        cron, boot = self.envs()
        self.assertEqual(cron, boot)
        self.assertIn(cron["ADMIN_ROLE"], ts.ADMIN_ROLES)
        self.assertRegex(cron["LIVE_SLOTS"], r"^\d+( \d+)*$")

    def test_configmap_generator_ships_the_tested_file(self):
        k = yaml.safe_load((TREE / "kustomization.yaml").read_text(encoding="utf-8"))
        gen = k["configMapGenerator"][0]
        self.assertEqual(gen["name"], "trueswarm-e2e-token-sync")
        self.assertEqual(gen["files"], ["token_sync.py"])

    def test_vault_role_exists(self):
        provision = (ROOT / "kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml").read_text(encoding="utf-8")
        m = re.search(r"bao write auth/kubernetes/role/trueswarm-e2e-sync \\\n(.*?)(?:\n\s*\n|\n\s*#)", provision, re.S)
        self.assertIsNotNone(m, "devworker-provision-job.yaml must create k8s-auth role trueswarm-e2e-sync")
        body = m.group(1)
        self.assertIn("bound_service_account_names=trueswarm-e2e-token-sync", body)
        self.assertIn("bound_service_account_namespaces=openbao", body)
        self.assertIn("token_policies=k8stoken-sync", body)


class FakeCluster:
    """Just enough of the Kubernetes Secret API (resourceVersion CAS) and OpenBao KV-v2 for sync()."""

    def __init__(self):
        self.secrets = {}  # ns -> obj with metadata.resourceVersion
        self.kv = {}  # slot -> data dict
        self.rv = 0
        self.kv_writes = 0

    def k8s(self, path, method="GET", payload=None):
        if "/leases" in path:
            return self.lease_api(method, payload)
        parts = path.strip("/").split("/")  # api v1 namespaces <ns> secrets [<name>]
        ns = parts[3]
        if method == "GET":
            return (200, json.loads(json.dumps(self.secrets[ns]))) if ns in self.secrets else (404, {})
        if method == "POST":
            if ns in self.secrets:
                return 409, {}
        elif method == "PUT":
            if ns not in self.secrets or payload["metadata"]["resourceVersion"] != self.secrets[ns]["metadata"]["resourceVersion"]:
                return 409, {}
        self.rv += 1
        obj = json.loads(json.dumps(payload))
        obj["metadata"]["resourceVersion"] = str(self.rv)
        self.secrets[ns] = obj
        return (201 if method == "POST" else 200), obj

    lease = None

    def lease_api(self, method, payload):
        if method == "GET":
            return (200, json.loads(json.dumps(self.lease))) if self.lease else (404, {})
        if method == "POST" and self.lease:
            return 409, {}
        if method == "PUT" and (not self.lease or payload["metadata"].get("resourceVersion") != self.lease["metadata"]["resourceVersion"]):
            return 409, {}
        self.rv += 1
        self.lease = json.loads(json.dumps(payload))
        self.lease["metadata"]["resourceVersion"] = str(self.rv)
        return (201 if method == "POST" else 200), json.loads(json.dumps(self.lease))

    def bao(self, path, method="GET", payload=None, ctype="application/json"):
        slot = path.rsplit("/", 1)[1]
        if method == "GET":
            return (200, {"data": {"data": dict(self.kv[slot])}}) if slot in self.kv else (404, {})
        if method == "POST":
            if slot in self.kv:
                return 400, {}
            self.kv[slot] = dict(payload["data"])
        elif method == "PATCH":
            if slot not in self.kv:
                return 404, {}
            self.kv[slot].update(payload["data"])
        self.kv_writes += 1
        return 200, {}

    def doc(self, ns):
        return json.loads(base64.b64decode(self.secrets[ns]["data"]["tokens.json"]))

    def assert_consistent(self, case, slots):
        """Every published token is its app's CURRENT token (tokens[0]) and is still valid."""
        for app, (ns, field, _, _) in ts.APPS.items():
            entries = {p["name"]: p for p in self.doc(ns)["principals"]}
            case.assertEqual(sorted(entries), sorted(slots))
            for slot in slots:
                token = self.kv[slot][f"{field}_e2e_token"]
                case.assertEqual(entries[slot]["tokens"][0]["sha256"], ts.sha256_hex(token), f"{app}/{slot}")
                case.assertEqual(entries[slot]["tokens"][0]["not_after"], int(self.kv[slot][f"{field}_e2e_valid_until"]))


def cfg(**over):
    c = ts.config_from_env({"LIVE_SLOTS": "1 4", "ADMIN_ACCESS_CLIENT_IDS": "svc.access"})
    c.update(over)
    return c


SLOTS = ["dev-worker-1", "dev-worker-4"]
quiet = lambda line: None  # noqa: E731


class SyncRuns(unittest.TestCase):
    def test_first_run_publishes_consistent_tokens_and_a_rerun_is_a_noop(self):
        fake = FakeCluster()
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet)
        fake.assert_consistent(self, SLOTS)
        admin = {p["name"]: p for p in fake.doc("trueswarm-admin")["principals"]}
        self.assertEqual(admin["dev-worker-4"]["role"], "operator")
        self.assertEqual(admin["dev-worker-4"]["access_client_ids"], ["svc.access"])
        versions = lambda: {ns: o["metadata"]["resourceVersion"] for ns, o in fake.secrets.items()}  # noqa: E731
        before, writes = versions(), fake.kv_writes
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: self.fail("a no-op run must not wait"), NOW + 60, quiet)
        self.assertEqual((versions(), fake.kv_writes), (before, writes))

    def test_overtaken_run_does_not_publish_over_the_later_one(self):
        """Codex #1059: run A writes its Secrets and pauses; run B reads them while OpenBao still holds
        nothing/the old tokens, rotates again and publishes; A must not then overwrite B in OpenBao."""
        fake = FakeCluster()

        def a_pauses(_):
            with self.assertRaises(SystemExit) as refused:  # run B, while A holds the lease
                ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW + 1, quiet, "run-b")
            self.assertIn("holds the", str(refused.exception))

        ts.sync(cfg(), fake.k8s, fake.bao, a_pauses, NOW, quiet, "run-a")
        fake.assert_consistent(self, SLOTS)

    def test_overtaken_rotation_of_existing_tokens_keeps_the_later_one(self):
        fake = FakeCluster()
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet)
        later = NOW + 8 * DAY  # both runs find the tokens due

        def a_pauses(_):
            with self.assertRaises(SystemExit):
                ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, later + 1, quiet, "run-b")

        ts.sync(cfg(), fake.k8s, fake.bao, a_pauses, later, quiet, "run-a")
        fake.assert_consistent(self, SLOTS)

    def test_run_dying_between_its_two_writes_self_heals(self):
        fake = FakeCluster()
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet)

        def die(_):
            raise RuntimeError("pod killed during the propagation wait")

        with self.assertRaises(RuntimeError):
            ts.sync(cfg(force=True), fake.k8s, fake.bao, die, NOW + 60, quiet)
        # OpenBao still holds the old tokens; the apps hold new current + old as a 1 h overlap.
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW + 120, quiet)
        fake.assert_consistent(self, SLOTS)

    def test_concurrent_secret_write_is_refused_before_anything_is_published(self):
        fake = FakeCluster()
        real = fake.k8s

        def racing_k8s(path, method="GET", payload=None):
            if method == "PUT" and "/secrets/" in path:
                real(path, "PUT", dict(payload, metadata=dict(payload["metadata"])))  # someone else wins
            return real(path, method, payload)

        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet)
        writes = fake.kv_writes
        with self.assertRaises(SystemExit) as stopped:
            ts.sync(cfg(force=True), racing_k8s, fake.bao, lambda s: None, NOW + 60, quiet)
        self.assertIn("changed under this run", str(stopped.exception))
        self.assertEqual(fake.kv_writes, writes)

    def test_retired_slot_is_dropped_from_both_secrets(self):
        fake = FakeCluster()
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet)
        ts.sync(cfg(live=[1]), fake.k8s, fake.bao, lambda s: None, NOW + 60, quiet)
        for ns in ("trueswarm", "trueswarm-admin"):
            self.assertEqual([p["name"] for p in fake.doc(ns)["principals"]], ["dev-worker-1"])


    def test_a_run_between_the_fence_and_the_publish_is_refused(self):
        """Codex #1059 (second round): A passes the fence, stalls before its OpenBao PATCH; B runs to
        completion; A resumes. The lease must keep B out, so A's publication is the final state."""
        fake = FakeCluster()
        real_bao = fake.bao
        state = {"b_tried": False}

        def stalling_bao(path, method="GET", payload=None, ctype="application/json"):
            if method in ("PATCH", "POST") and not state["b_tried"]:
                state["b_tried"] = True
                with self.assertRaises(SystemExit):
                    ts.sync(cfg(), fake.k8s, real_bao, lambda s: None, NOW + 300, quiet, "run-b")
            return real_bao(path, method, payload, ctype)

        ts.sync(cfg(), fake.k8s, stalling_bao, lambda s: None, NOW, quiet, "run-a")
        self.assertTrue(state["b_tried"])
        fake.assert_consistent(self, SLOTS)

    def test_the_fence_still_stops_a_run_whose_lease_was_taken_over(self):
        """Second layer: if the lease expired under a hung run and another run took over and rotated,
        the hung run must not publish when it wakes up."""
        fake = FakeCluster()

        def hung(_):
            fake.lease["spec"]["renewTime"] = ts._micro(NOW - 10 * ts.LEASE_SECONDS)  # expired
            ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW + 1, quiet, "run-b")

        with self.assertRaises(SystemExit) as stopped:
            ts.sync(cfg(), fake.k8s, fake.bao, hung, NOW, quiet, "run-a")
        self.assertIn("later run replaced", str(stopped.exception))
        fake.assert_consistent(self, SLOTS)

    def test_lease_is_released_after_success_and_failure_and_an_expired_one_is_taken(self):
        fake = FakeCluster()
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet, "run-a")
        self.assertIsNone(fake.lease["spec"]["holderIdentity"])

        def die(_):
            raise RuntimeError("killed")

        with self.assertRaises(RuntimeError):
            ts.sync(cfg(force=True), fake.k8s, fake.bao, die, NOW + 60, quiet, "run-b")
        self.assertIsNone(fake.lease["spec"]["holderIdentity"])
        fake.lease["spec"].update(holderIdentity="crashed-pod", renewTime=ts._micro(NOW - 2 * ts.LEASE_SECONDS))
        ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW + 120, quiet, "run-c")
        fake.assert_consistent(self, SLOTS)

    def test_a_live_lease_held_elsewhere_stops_the_run_before_any_read_or_write(self):
        fake = FakeCluster()
        fake.lease_api("POST", {"metadata": {"name": "x"}, "spec": {
            "holderIdentity": "other-pod", "leaseDurationSeconds": ts.LEASE_SECONDS, "renewTime": ts._micro(NOW - 5)}})
        with self.assertRaises(SystemExit):
            ts.sync(cfg(), fake.k8s, fake.bao, lambda s: None, NOW, quiet, "run-a")
        self.assertEqual((fake.secrets, fake.kv), ({}, {}))


if __name__ == "__main__":
    unittest.main()
