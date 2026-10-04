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
import importlib.util
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
        return ts.plan_slot(published, until, entry, NOW, VALIDITY, ROTATE_BEFORE, OVERLAP, force)

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


if __name__ == "__main__":
    unittest.main()
