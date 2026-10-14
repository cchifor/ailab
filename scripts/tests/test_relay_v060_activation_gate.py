#!/usr/bin/env python3
"""Keep the retired Relay trust bridge out of every deployment overlay."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/runbooks/relay-v060-activation-evidence.yaml"
RELAY = ROOT / "kubernetes/apps/apps/relay/kustomization.yaml"
ROUTER = ROOT / "kubernetes/apps/apps/llm-router/kustomization.yaml"
RELAY_MANIFEST = ROOT / "kubernetes/apps/apps/relay/relay.yaml"


class ActivationGate(unittest.TestCase):
    def test_retired_trust_bridge_is_not_deployable(self):
        evidence = yaml.safe_load(EVIDENCE.read_text())
        relay = yaml.safe_load(RELAY.read_text())
        router = yaml.safe_load(ROUTER.read_text())
        relay_text = RELAY_MANIFEST.read_text()
        for resources in (relay.get("components", []), router.get("components", [])):
            self.assertNotIn("../../../components/relay-router-control-plane", resources)
            self.assertNotIn("../../../components/llm-router-relay-keys", resources)
        self.assertEqual(evidence["version"], 2)
        self.assertEqual(evidence["status"], "pending")
        self.assertEqual(evidence["accessFlow"]["status"], "pending")
        self.assertNotIn("RELAY_AGENT_CONTROL_PLANE", relay_text)
        self.assertNotIn("RELAY_ROUTER_CONNECTIONS_FILE", relay_text)

    def test_evidence_contains_no_bridge_credentials_or_activation_material(self):
        text = EVIDENCE.read_text()
        for forbidden in ("admin-key.pem", "reconciler-key.pem", "trust.json", "relay-keys"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
