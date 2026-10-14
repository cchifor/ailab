#!/usr/bin/env python3
"""Keep the retired Relay trust bridge out of every deployment overlay."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/runbooks/relay-v060-activation-evidence.yaml"
RELAY = ROOT / "kubernetes/apps/apps/relay/kustomization.yaml"
ROUTER = ROOT / "kubernetes/apps/apps/llm-router/kustomization.yaml"
COMMUNICATION_COMPONENT = (
    ROOT / "kubernetes/components/relay-default-communication"
)


class ActivationGate(unittest.TestCase):
    def test_retired_trust_bridge_is_not_deployable(self):
        evidence = yaml.safe_load(EVIDENCE.read_text())
        relay = yaml.safe_load(RELAY.read_text())
        router = yaml.safe_load(ROUTER.read_text())
        for resources in (relay.get("components", []), router.get("components", [])):
            self.assertNotIn("../../../components/relay-router-control-plane", resources)
            self.assertNotIn("../../../components/llm-router-relay-keys", resources)
        # Check all active overlay YAML, including referenced components. A
        # flag or trust component must not be smuggled in through a merge patch.
        active_roots = [
            ROOT / "kubernetes/apps/apps/relay",
            ROOT / "kubernetes/apps/apps/llm-router",
        ]
        forbidden = (
            "RELAY_AGENT_CONTROL_PLANE",
            "RELAY_ROUTER_CONNECTIONS_FILE",
            "relay-router-control-plane",
            "llm-router-relay-keys",
            "llm-router-relay-keys-trust",
            "relay-router-connection",
        )
        for root in active_roots:
            for path in root.rglob("*"):
                if path.suffix not in {".yaml", ".yml"} or not path.is_file():
                    continue
                text = path.read_text()
                for value in forbidden:
                    self.assertNotIn(value, text, f"retired bridge material in {path}")
        self.assertEqual(evidence["version"], 2)
        self.assertIn(evidence["status"], {"pending", "approved"})
        self.assertIn(evidence["accessFlow"]["status"], {"pending", "approved"})

    def test_evidence_contains_no_bridge_credentials_or_activation_material(self):
        text = EVIDENCE.read_text()
        for forbidden in ("admin-key.pem", "reconciler-key.pem", "trust.json", "relay-keys"):
            self.assertNotIn(forbidden, text)

    def test_default_communication_component_is_a_narrow_relay_patch(self):
        component_path = COMMUNICATION_COMPONENT / "kustomization.yaml"
        component = yaml.safe_load(component_path.read_text())
        self.assertEqual(component["kind"], "Component")
        self.assertEqual(component.get("resources", []), [])
        self.assertEqual(component.get("patches"), [{"path": "deployment.yaml"}])

        relay_manifest = next(
            document
            for document in yaml.safe_load_all(
                (ROOT / "kubernetes/apps/apps/relay/relay.yaml").read_text()
            )
            if document and document.get("kind") == "Deployment"
        )
        relay_containers = relay_manifest["spec"]["template"]["spec"]["containers"]
        self.assertEqual(
            [container.get("name") for container in relay_containers].count("relay"),
            1,
        )

        patch = yaml.safe_load(
            (COMMUNICATION_COMPONENT / "deployment.yaml").read_text()
        )
        patch_containers = patch["spec"]["template"]["spec"]["containers"]
        self.assertEqual([container.get("name") for container in patch_containers], ["relay"])
        self.assertEqual(
            patch_containers[0]["env"],
            [{"name": "RELAY_AGENT_COMMUNICATION", "value": "1"}],
        )

        # The component may only patch the existing Deployment. Any future
        # Secret, router, or other standalone resource must be reviewed as a
        # separate deployment change rather than silently widening this toggle.
        for path in COMMUNICATION_COMPONENT.rglob("*"):
            if path.suffix not in {".yaml", ".yml"} or not path.is_file():
                continue
            document = yaml.safe_load(path.read_text())
            self.assertIn(document.get("kind"), {"Component", "Deployment"}, path)
            self.assertNotIn(document.get("kind"), {"Secret", "ConfigMap"}, path)


if __name__ == "__main__":
    unittest.main()
