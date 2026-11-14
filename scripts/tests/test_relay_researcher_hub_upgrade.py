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
import re
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


def order_violations(comps: set[str]) -> list[str]:
    """The runbook's order as a pure check over the overlay's component names.

    One way only: installations need the recovery generation, but the generation may stand alone,
    because every restore (relay-recovery-verification.md) and the H3 rollback (revert installations,
    keep the generation) include relay-restore-fencing without installations."""
    out = []
    if "relay-infrastructure-installations" in comps and "relay-restore-fencing" not in comps:
        out.append("installations without the recovery generation (step H3)")
    if "relay-worker-profiles" in comps and not {"relay-infrastructure-installations",
                                                 "relay-worker-budget-client"} <= comps:
        out.append("profiles without installations and the budget client (step H4)")
    if "relay-agent-control-plane" in comps and not {"relay-router-connection", "relay-worker-profiles"} <= comps:
        out.append("control plane without the Router connection and profiles (step H6)")
    if "relay-agent-control-plane" in comps and comps & {"relay-gitea-authority", "relay-notifications"}:
        out.append("control plane with Gitea authority or notifications (owner decision G2)")
    return out


def included_component_dirs() -> list[Path]:
    return [(RELAY / c).resolve() for c in doc(RELAY / "kustomization.yaml").get("components", [])]


class InclusionGate(unittest.TestCase):
    """The order of docs/runbooks/relay-researcher-hub-upgrade.md, enforced on the live overlay."""

    def test_live_overlay_follows_the_order(self):
        names = set(relay_components())
        # relay-notifications is included as components/relay-notifications/relay: match the path too.
        names |= {"relay-notifications" for d in included_component_dirs() if "relay-notifications" in d.parts}
        self.assertEqual(order_violations(names), [])

    def test_order_states(self):
        ok = [
            set(),
            {"relay-restore-fencing"},                                  # a restore, or the H3 rollback
            {"relay-restore-fencing", "relay-infrastructure-installations"},
            {"relay-restore-fencing", "relay-infrastructure-installations", "relay-worker-budget-client",
             "relay-worker-profiles"},
            {"relay-restore-fencing", "relay-infrastructure-installations", "relay-worker-budget-client",
             "relay-worker-profiles", "relay-router-connection", "relay-agent-control-plane",
             "relay-control-monitoring", "relay-worker-monitoring"},
        ]
        for comps in ok:
            self.assertEqual(order_violations(comps), [], comps)
        bad = [
            {"relay-infrastructure-installations"},
            {"relay-restore-fencing", "relay-worker-profiles", "relay-worker-budget-client"},
            {"relay-restore-fencing", "relay-infrastructure-installations", "relay-worker-profiles"},
            {"relay-restore-fencing", "relay-infrastructure-installations", "relay-agent-control-plane"},
            ok[-1] | {"relay-gitea-authority"},
            ok[-1] | {"relay-notifications"},
        ]
        for comps in bad:
            self.assertTrue(order_violations(comps), comps)

    def test_placeholder_registry_is_never_included(self):
        if "relay-worker-profiles" in relay_components():
            self.assertNotIn(b"OPERATOR-FILLS", (COMPONENTS / "relay-worker-profiles/profiles.json").read_bytes())

    def test_no_environments_anywhere_in_the_live_relay(self):
        # Owner decision G2: environments stay off with the control plane. Scan the overlay AND every
        # component it includes, so a component cannot smuggle the flag in.
        for root in [RELAY, *included_component_dirs()]:
            for path in root.rglob("*.yaml"):
                self.assertNotIn("RELAY_ENVIRONMENTS", path.read_text(encoding="utf-8"), path)
        self.assertNotIn("RELAY_ENVIRONMENTS",
                         (COMPONENTS / "relay-agent-control-plane/deployment.yaml").read_text(encoding="utf-8"))


def code_blocks(text: str) -> list[str]:
    # Fenced blocks, also the ones indented inside list items.
    return re.findall(r"^[ \t]*```[a-z]*\n(.*?)^[ \t]*```", text, flags=re.M | re.S)


class Runbook(unittest.TestCase):
    def test_h3_initializes_and_never_fences_the_live_database(self):
        # Owner decision G1: the first generation comes from recovery:initialize; the fence is for
        # restores only. Relay's fence is `npm run recovery:fence` (package.json), compiled as
        # dist/scripts/fence-recovery.js (scripts/fence-recovery.ts): no command block of this
        # runbook may run either.
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("recovery:initialize", text)
        self.assertIn('kind: "initial"', text)
        blocks = code_blocks(text)
        self.assertTrue(any("recovery:initialize" in b for b in blocks), "H3 must run the initializer")
        for block in blocks:
            self.assertNotIn("recovery:fence", block)
            self.assertNotIn("fence-recovery", block)

    def test_generation_configmap_is_excluded_from_velero(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        [create] = [b for b in code_blocks(text) if "create configmap relay-recovery-generation" in b]
        self.assertIn("velero.io/exclude-from-backup=true", create)
        self.assertIn('.metadata.labels["velero.io/exclude-from-backup"] == "true"', create)


if __name__ == "__main__":
    unittest.main()
