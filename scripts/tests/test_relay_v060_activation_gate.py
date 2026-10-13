#!/usr/bin/env python3
"""Keep v0.6.0 activation behind a reviewed, nonsecret evidence record."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/runbooks/relay-v060-activation-evidence.yaml"
RELAY = ROOT / "kubernetes/apps/apps/relay/kustomization.yaml"
ROUTER = ROOT / "kubernetes/apps/apps/llm-router/kustomization.yaml"
RELAY_COMPONENT = "../../../components/relay-router-control-plane"
ROUTER_COMPONENT = "../../../components/llm-router-relay-keys"


class ActivationGate(unittest.TestCase):
    def test_evidence_status_matches_activation_shape(self):
        evidence = yaml.safe_load(EVIDENCE.read_text())
        relay = yaml.safe_load(RELAY.read_text())
        router = yaml.safe_load(ROUTER.read_text())
        relay_active = RELAY_COMPONENT in relay.get("components", [])
        router_active = ROUTER_COMPONENT in router.get("components", [])
        self.assertIn(evidence["status"], {"pending", "approved"})
        self.assertEqual(relay_active, router_active, "Relay and router activation must move together")
        if evidence["status"] == "pending":
            self.assertFalse(relay_active, "Relay activation requires approved evidence")
            self.assertFalse(router_active, "router bridge activation requires approved evidence")
        else:
            self.assertTrue(relay_active, "approved evidence must be accompanied by activation")
            self.assertTrue(router_active, "approved evidence must be accompanied by router activation")

    def test_activation_requires_exact_release_and_complete_nonsecret_evidence(self):
        evidence = yaml.safe_load(EVIDENCE.read_text())
        relay = yaml.safe_load(RELAY.read_text())
        router = yaml.safe_load(ROUTER.read_text())
        relay_active = RELAY_COMPONENT in relay.get("components", [])
        router_active = ROUTER_COMPONENT in router.get("components", [])
        self.assertEqual(relay_active, router_active, "Relay and router activation must move together")
        if not relay_active:
            return
        self.assertEqual(evidence["status"], "approved")
        self.assertEqual(evidence["release"], {
            "sourceCommit": "be1b72ebb7c19ac312500b9c4df12eea0f971dc2",
            "relayImageDigest": "sha256:5e01b323d6e5cacd4980d7bcc14082ec937d1731a69a89af2568f8f322b9f774",
            "routerImageDigest": "sha256:f8ddc9ce9353f6e3a5f7bcc75b8ff16d2cfa29cf0a5e801358b57d9dbc4bec54",
        })
        self.assertEqual(evidence["tenantId"], "00000000-0000-4000-8000-000000000001")
        for item in evidence["preflight"].values():
            self.assertEqual(item["status"], "approved")
            self.assertTrue(item["reference"])
        # Acceptance is exercised during the constrained window after these
        # components start. It may be pending in the activation PR, but a
        # completed report must carry its nonsecret receipt and operation IDs.
        self.assertIn(evidence["acceptance"]["status"], {"pending", "approved"})
        if evidence["acceptance"]["status"] == "approved":
            self.assertTrue(evidence["acceptance"]["reference"])
            self.assertTrue(evidence["acceptance"]["operationIds"])


if __name__ == "__main__":
    unittest.main()
