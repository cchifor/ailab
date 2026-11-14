#!/usr/bin/env python3
"""Source gates for the Relay hub upgrade of the first researcher worker.

docs/runbooks/relay-researcher-hub-upgrade.md. The three hub components are staged and inert; these
tests pin their exact content and refuse an inclusion that breaks the runbook's order or the owner's
decisions (relay#277 comment 122379). scripts/tests/test_relay_v060_activation_gate.py refuses each
component in the live overlay until its step; scripts/tests/test-relay-researcher-hub-render.sh
renders them onto the live Deployment.
"""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "kubernetes/components"
RELAY = ROOT / "kubernetes/apps/apps/relay"
RUNBOOK = ROOT / "docs/runbooks/relay-researcher-hub-upgrade.md"


def doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def relay_components() -> list[str]:
    return [c.rsplit("/", 1)[-1] for c in doc(RELAY / "kustomization.yaml").get("components", [])]


def patch_container(component: str) -> dict:
    patch = doc(COMPONENTS / component / "deployment.yaml")
    assert (patch["kind"], patch["metadata"]["name"], patch["metadata"]["namespace"]) == ("Deployment", "relay", "relay")
    (container,) = patch["spec"]["template"]["spec"]["containers"]
    assert container["name"] == "relay"
    return container


class Components(unittest.TestCase):
    """Each component sets exactly its own settings and nothing else."""

    def test_component_shapes(self):
        for name in ("relay-infrastructure-installations", "relay-worker-profiles", "relay-agent-control-plane"):
            kust = doc(COMPONENTS / name / "kustomization.yaml")
            self.assertEqual(kust["kind"], "Component", name)
            self.assertEqual(kust["patches"], [{"path": "deployment.yaml"}], name)
            self.assertEqual(kust.get("resources", []), [], name)

    def test_installations_sets_one_flag(self):
        self.assertEqual(patch_container("relay-infrastructure-installations"),
                         {"name": "relay", "env": [{"name": "RELAY_INFRASTRUCTURE_INSTALLATIONS", "value": "1"}]})

    def test_control_plane_sets_one_flag_and_no_environments(self):
        self.assertEqual(patch_container("relay-agent-control-plane"),
                         {"name": "relay", "env": [{"name": "RELAY_AGENT_CONTROL_PLANE", "value": "1"}]})

    def test_profiles_mount_and_pin(self):
        container = patch_container("relay-worker-profiles")
        env = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual(set(env), {"RELAY_WORKER_PROFILES_FILE", "RELAY_WORKER_PROFILES_SHA256"})
        self.assertEqual(env["RELAY_WORKER_PROFILES_FILE"], "/run/relay-worker-profiles/profiles.json")
        self.assertEqual(container["volumeMounts"], [{"name": "worker-profiles",
                                                      "mountPath": "/run/relay-worker-profiles/profiles.json",
                                                      "subPath": "profiles.json", "readOnly": True}])
        patch = doc(COMPONENTS / "relay-worker-profiles/deployment.yaml")
        self.assertEqual(patch["spec"]["template"]["spec"]["volumes"],
                         [{"name": "worker-profiles", "configMap": {"name": "relay-worker-profiles", "defaultMode": 0o444}}])
        kust = doc(COMPONENTS / "relay-worker-profiles/kustomization.yaml")
        self.assertEqual(kust["configMapGenerator"],
                         [{"name": "relay-worker-profiles", "namespace": "relay", "files": ["profiles.json"]}])
        body = (COMPONENTS / "relay-worker-profiles/profiles.json").read_bytes()
        placeholder = b"OPERATOR-FILLS" in body
        # Placeholder file and placeholder pin go together; a real file carries its own SHA-256.
        self.assertEqual(placeholder, env["RELAY_WORKER_PROFILES_SHA256"].startswith("OPERATOR-FILLS"))
        if not placeholder:
            self.assertEqual(env["RELAY_WORKER_PROFILES_SHA256"], hashlib.sha256(body).hexdigest())


class InclusionGate(unittest.TestCase):
    """The order of docs/runbooks/relay-researcher-hub-upgrade.md, enforced on the live overlay."""

    def test_installations_and_generation_together(self):
        comps = set(relay_components())
        if comps & {"relay-infrastructure-installations", "relay-restore-fencing"}:
            self.assertTrue({"relay-infrastructure-installations", "relay-restore-fencing"} <= comps,
                            "installations need the recovery generation and vice versa (step H3)")

    def test_profiles_with_budget_client_after_installations(self):
        comps = set(relay_components())
        if "relay-worker-profiles" in comps:
            self.assertIn("relay-infrastructure-installations", comps)
            self.assertIn("relay-worker-budget-client", comps)
            self.assertNotIn(b"OPERATOR-FILLS", (COMPONENTS / "relay-worker-profiles/profiles.json").read_bytes())

    def test_control_plane_last_and_without_environments_gitea_or_notifications(self):
        # Owner decision G2: the control plane with its full scope, at H6 with the Router connection,
        # and without the environments, Gitea authority and notification components.
        comps = relay_components()
        if "relay-agent-control-plane" not in comps:
            return
        self.assertTrue({"relay-router-connection", "relay-worker-profiles"} <= set(comps))
        all_comps = doc(RELAY / "kustomization.yaml").get("components", [])
        for c in all_comps:
            self.assertNotIn("relay-gitea-authority", c)
            self.assertNotIn("relay-notifications", c)
        for path in RELAY.rglob("*.yaml"):
            self.assertNotIn("RELAY_ENVIRONMENTS", path.read_text(encoding="utf-8"), path)


class Runbook(unittest.TestCase):
    def test_h3_initializes_and_never_fences_the_live_database(self):
        # Owner decision G1: the first generation comes from recovery:initialize; the fence is for
        # restores only, so no runbook command runs the fence script against the live hub.
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("recovery:initialize", text)
        self.assertIn('kind: "initial"', text)
        self.assertNotIn("fence-recovery.js", text)


if __name__ == "__main__":
    unittest.main()
