"""scripts/talos-cp-plan-check.py: PASS only for exactly the audit-rules change on the targeted CP."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("plan_check", ROOT / "scripts/talos-cp-plan-check.py")
plan_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_check)

V1 = {"version": "v1alpha1", "machine": {"type": "controlplane", "token": "SECRET-TOKEN"},
      "cluster": {"apiServer": {"auditPolicy": {"apiVersion": "audit.k8s.io/v1", "kind": "Policy",
                                                "rules": [{"level": "Metadata"}]}}}}
EXTRA = {"apiVersion": "v1alpha1", "kind": "HostnameConfig", "name": "", "hostname": "talos-cp1"}
R7 = [{"level": "Metadata", "resources": [{"group": "", "resources": ["secrets"]}]},
      {"level": "RequestResponse", "verbs": ["create"], "namespaces": ["relay-researcher-1"]},
      {"level": "Metadata"}]


def config(v1):
    return yaml.safe_dump_all([v1, EXTRA], sort_keys=False)


def plan(before, after, others=()):
    changes = [{"address": 'talos_machine_configuration_apply.cp["cp1"]',
                "change": {"actions": ["update"], "before": {"machine_configuration": before},
                           "after": {"machine_configuration": after}}},
               {"address": 'talos_machine_configuration_apply.cp["cp2"]',
                "change": {"actions": ["no-op"], "before": {}, "after": {}}}]
    changes += [{"address": a, "change": {"actions": ["update"], "before": {}, "after": {}}} for a in others]
    return {"resource_changes": changes}


def live(text):
    return json.dumps({"node": "192.168.0.41", "metadata": {"id": "v1alpha1"}, "spec": text}) + "\n"


class PlanCheck(unittest.TestCase):
    def setUp(self):
        self.before = config(V1)
        v1 = copy.deepcopy(V1)
        v1["cluster"]["apiServer"]["auditPolicy"]["rules"] = R7
        self.after = config(v1)

    def run_check(self, p, mc, node="cp1"):
        return plan_check.check(p, mc, node, plan_check.DEFAULT_EXPECTED)

    def test_exactly_the_rules_passes_and_prints_no_value(self):
        ok, lines = self.run_check(plan(self.before, self.after), live(self.before))
        self.assertTrue(ok, lines)
        self.assertIn("state-before vs planned: ['v1alpha1~/cluster/apiServer/auditPolicy/rules']", lines)
        self.assertNotIn("SECRET-TOKEN", "\n".join(lines))

    def test_the_removal_is_the_same_path(self):
        ok, lines = self.run_check(plan(self.after, self.before), live(self.after))
        self.assertTrue(ok, lines)

    def test_live_drift_stops(self):
        drifted = copy.deepcopy(V1)
        drifted["machine"]["token"] = "OTHER"
        ok, lines = self.run_check(plan(self.before, self.after), live(config(drifted)))
        self.assertFalse(ok)
        self.assertIn("live vs state-before: ['v1alpha1~/machine/token']", lines)
        self.assertNotIn("OTHER", "\n".join(lines))

    def test_any_other_change_stops(self):
        v1 = copy.deepcopy(V1)
        v1["cluster"]["apiServer"]["auditPolicy"]["rules"] = R7
        v1["machine"]["token"] = "ROTATED"
        ok, _ = self.run_check(plan(self.before, config(v1)), live(self.before))
        self.assertFalse(ok)

    def test_another_resource_changing_stops(self):
        ok, lines = self.run_check(plan(self.before, self.after, others=["proxmox_virtual_environment_vm.cp"]),
                                   live(self.before))
        self.assertFalse(ok)
        self.assertIn("other resources changing: ['proxmox_virtual_environment_vm.cp']", lines)

    def test_wrong_node_stops(self):
        ok, lines = self.run_check(plan(self.before, self.after), live(self.before), node="cp3")
        self.assertFalse(ok)

    def test_unknown_planned_config_stops(self):
        ok, _ = self.run_check(plan(self.before, None), live(self.before))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
