#!/usr/bin/env python3
"""Source gates for the Relay hub upgrade of the first researcher worker.

docs/runbooks/relay-researcher-hub-upgrade.md. The hub components are staged and inert; these tests pin
their exact content and refuse an inclusion that breaks the runbook's order or the owner's decisions
(relay#277 comment 122379). H7's candidate component has a generated half (run/), which
scripts/tests/test_relay_candidate_run.py checks against scripts/relay-candidate-run.py. scripts/tests/test_relay_v060_activation_gate.py refuses each
component in the live overlay until its step; scripts/tests/test-relay-researcher-hub-render.sh
renders them onto the live Deployment.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
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

    def test_candidate_shape(self):
        # The reviewed half patches the Deployment; the generated run/ half (a nested Component) carries
        # the run-bound ConfigMap, Secret name and pin. No standalone resource in either.
        kust = doc(COMPONENTS / "relay-worker-candidate/kustomization.yaml")
        self.assertEqual({k: v for k, v in kust.items() if k not in ("apiVersion", "kind")},
                         {"components": ["run"], "patches": [{"path": "deployment.yaml"}]})
        self.assertEqual(kust["kind"], "Component")
        run = doc(COMPONENTS / "relay-worker-candidate/run/kustomization.yaml")
        self.assertEqual(run["kind"], "Component")
        self.assertEqual(set(run), {"apiVersion", "kind", "configMapGenerator", "patches"})
        self.assertEqual(run["patches"], [{"path": "deployment.yaml"}])
        [generator] = run["configMapGenerator"]
        self.assertEqual(generator["files"], ["document.json"])
        self.assertEqual(generator["namespace"], "relay")
        self.assertIs(generator["options"]["immutable"], True)
        self.assertIs(generator["options"]["disableNameSuffixHash"], True)

    def test_candidate_patch(self):
        """The reviewed half, exactly: the document as a root-owned 0444 subPath file, and the session
        key copied by UID 1000 into a Memory emptyDir as 0400, because no Secret volume can give a
        non-root reader a file without group or other bits (the component's header)."""
        patch = doc(COMPONENTS / "relay-worker-candidate/deployment.yaml")
        self.assertEqual((patch["kind"], patch["metadata"]), ("Deployment", {"name": "relay", "namespace": "relay"}))
        spec = patch["spec"]["template"]["spec"]
        self.assertEqual(set(spec), {"initContainers", "containers", "volumes"})
        relay = next(
            d for d in yaml.safe_load_all((RELAY / "relay.yaml").read_text(encoding="utf-8"))
            if d and d["kind"] == "Deployment")
        node_image = next(c["image"] for c in relay["spec"]["template"]["spec"]["containers"]
                          if c["name"] == "backup-holder")
        self.assertEqual(spec["initContainers"], [{
            "name": "candidate-session-key",
            "image": node_image,
            "command": ["install", "-m", "0400", "/run/relay-worker-candidate-key-source/session-key.pem",
                        "/run/relay-worker-candidate-key/session-key.pem"],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "5m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
            "volumeMounts": [
                {"name": "worker-candidate-key-source", "mountPath": "/run/relay-worker-candidate-key-source",
                 "readOnly": True},
                {"name": "worker-candidate-key", "mountPath": "/run/relay-worker-candidate-key"}],
        }])
        self.assertEqual(spec["containers"], [{
            "name": "relay",
            "env": [{"name": "RELAY_WORKER_CANDIDATE_FILE", "value": "/run/relay-worker-candidate/document.json"},
                    {"name": "RELAY_WORKER_CANDIDATE_SESSION_KEY_FILE",
                     "value": "/run/relay-worker-candidate-key/session-key.pem"}],
            "volumeMounts": [
                {"name": "worker-candidate-document", "mountPath": "/run/relay-worker-candidate/document.json",
                 "subPath": "document.json", "readOnly": True},
                {"name": "worker-candidate-key", "mountPath": "/run/relay-worker-candidate-key", "readOnly": True}],
        }])
        self.assertEqual(spec["volumes"], [
            {"name": "worker-candidate-document",
             "configMap": {"defaultMode": 0o444, "items": [{"key": "document.json", "path": "document.json"}]}},
            {"name": "worker-candidate-key-source",
             "secret": {"defaultMode": 0o440, "items": [{"key": "session-key.pem", "path": "session-key.pem"}]}},
            {"name": "worker-candidate-key", "emptyDir": {"medium": "Memory", "sizeLimit": "64Ki"}},
        ])
        # The run-bound half sets only the pin and the two names, never a fixed field above.
        run = doc(COMPONENTS / "relay-worker-candidate/run/deployment.yaml")
        rspec = run["spec"]["template"]["spec"]
        self.assertEqual(set(rspec), {"containers", "volumes"})
        [container] = rspec["containers"]
        self.assertEqual([e["name"] for e in container["env"]], ["RELAY_WORKER_CANDIDATE_SHA256"])
        self.assertEqual(set(container), {"name", "env"})
        self.assertEqual([(v["name"], set(v), set(v.get("configMap", v.get("secret")))) for v in rspec["volumes"]],
                         [("worker-candidate-document", {"name", "configMap"}, {"name"}),
                          ("worker-candidate-key-source", {"name", "secret"}, {"secretName"})])

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
    keep the generation) include relay-restore-fencing without installations.

    The control plane does not need the profile registry: H6 comes long before H4b, whose entries
    carry the genuine release approval issued only after qualification. That approval is not visible
    here; Relay's own registry schema refuses an entry without one. H6 still follows H3 and H4a, so
    the control plane requires the installations (and through them the recovery generation) and the
    budget client explicitly, no longer through the registry."""
    out = []
    if "relay-infrastructure-installations" in comps and "relay-restore-fencing" not in comps:
        out.append("installations without the recovery generation (step H3)")
    if "relay-worker-profiles" in comps and not {"relay-infrastructure-installations",
                                                 "relay-worker-budget-client"} <= comps:
        out.append("profiles without installations and the budget client (steps H3, H4a before H4b)")
    if "relay-agent-control-plane" in comps and not {"relay-router-connection", "relay-infrastructure-installations",
                                                     "relay-worker-budget-client"} <= comps:
        out.append("control plane without the Router connection, installations and the budget client "
                   "(step H6 follows H3 and H4a)")
    if "relay-agent-control-plane" in comps and comps & {"relay-gitea-authority", "relay-notifications"}:
        out.append("control plane with Gitea authority or notifications (owner decision G2)")
    # H6's monitoring: Relay refuses to start with a control metrics file and no control plane
    # ("Control metrics require the agent control plane", server/main.ts), and the worker alerts read
    # the control collector's series (docs/runbooks/relay-worker-monitoring.md).
    if "relay-control-monitoring" in comps and "relay-agent-control-plane" not in comps:
        out.append("control monitoring without the control plane (Relay refuses to start)")
    if "relay-worker-monitoring" in comps and "relay-control-monitoring" not in comps:
        out.append("worker monitoring without the control collector")
    # H7 needs H6 (the budget authority starts on the candidate profile source with the Router
    # connection) and so everything H6 needs; Relay's design removes the candidate authority before the
    # canary, and H4b's registry comes after the canary, so the two never share a rollout.
    if "relay-worker-candidate" in comps and not {"relay-router-connection", "relay-agent-control-plane",
                                                  "relay-infrastructure-installations", "relay-restore-fencing",
                                                  "relay-worker-budget-client"} <= comps:
        out.append("candidate files without H6 (step H7 follows H6)")
    if "relay-worker-candidate" in comps and "relay-worker-profiles" in comps:
        out.append("candidate files with the profile registry (H7 is removed before the canary, H4b follows it)")
    return out


def sets_environments(path: Path) -> list[str]:
    """Every parsed scalar of a YAML file that names RELAY_ENVIRONMENTS (an env entry's name or an
    inline patch string). Comments are not parsed values, so they never match."""
    hits: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(k)
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str) and "RELAY_ENVIRONMENTS" in value:
            hits.append(value)

    for d in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        walk(d)
    return hits


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
        h3 = {"relay-restore-fencing", "relay-infrastructure-installations"}
        h4a = h3 | {"relay-worker-budget-client"}                       # the budget client, no profiles
        h6 = h4a | {"relay-router-connection", "relay-agent-control-plane",
                    "relay-control-monitoring", "relay-worker-monitoring"}  # still no profiles
        # H7's candidate files for the nineteen scenarios; the canary and qualification files are their
        # own components, not staged yet. The candidate files go before the canary, and H4b adds the
        # registry after the release approval.
        h7 = h6 | {"relay-worker-candidate"}
        h4b = h6 | {"relay-worker-profiles"}
        ok = [
            set(),
            {"relay-restore-fencing"},                                  # a restore, or the H3 rollback
            h3,
            h4a,
            h6,
            h7,
            h4b,
        ]
        for comps in ok:
            self.assertEqual(order_violations(comps), [], comps)
        bad = [
            {"relay-infrastructure-installations"},
            {"relay-restore-fencing", "relay-worker-profiles", "relay-worker-budget-client"},
            {"relay-restore-fencing", "relay-infrastructure-installations", "relay-worker-profiles"},
            {"relay-restore-fencing", "relay-infrastructure-installations", "relay-agent-control-plane"},
            {"relay-agent-control-plane", "relay-router-connection"},   # H6 with nothing before it
            *(h6 - {missing} for missing in ("relay-restore-fencing", "relay-infrastructure-installations",
                                             "relay-worker-budget-client", "relay-router-connection")),
            h6 - {"relay-agent-control-plane"},                        # the collector without the control plane
            h6 - {"relay-control-monitoring"},                         # worker alerts without the collector
            h4a | {"relay-control-monitoring", "relay-worker-monitoring"},
            h4b - {"relay-worker-budget-client"},
            h4b | {"relay-gitea-authority"},
            h4b | {"relay-notifications"},
            {"relay-worker-candidate"},
            h4a | {"relay-worker-candidate"},                          # H7 before H6
            *(h7 - {missing} for missing in ("relay-router-connection", "relay-agent-control-plane",
                                             "relay-infrastructure-installations", "relay-restore-fencing",
                                             "relay-worker-budget-client")),
            h7 | {"relay-worker-profiles"},
        ]
        for comps in bad:
            self.assertTrue(order_violations(comps), comps)

    def test_placeholder_registry_is_never_included(self):
        if "relay-worker-profiles" in relay_components():
            self.assertNotIn(b"OPERATOR-FILLS", (COMPONENTS / "relay-worker-profiles/profiles.json").read_bytes())

    def test_placeholder_candidate_is_never_included(self):
        if "relay-worker-candidate" in relay_components():
            for name in ("document.json", "deployment.yaml", "kustomization.yaml"):
                self.assertNotIn(b"OPERATOR-FILLS", (COMPONENTS / "relay-worker-candidate/run" / name).read_bytes())

    def test_no_environments_anywhere_in_the_live_relay(self):
        # Owner decision G2: environments stay off with the control plane. Check the overlay AND every
        # component it includes, so a component cannot smuggle the flag in. Parsed values only (env
        # entries, inline patch strings), never comments: the control-plane component explains the
        # flag in its header and must stay includable.
        for root in [RELAY, *included_component_dirs()]:
            for path in root.rglob("*.yaml"):
                self.assertEqual(sets_environments(path), [], path)
        self.assertEqual(sets_environments(COMPONENTS / "relay-agent-control-plane/deployment.yaml"), [])
        self.assertEqual(sets_environments(COMPONENTS / "relay-agent-control-plane/kustomization.yaml"), [])

    def test_environments_detector(self):
        with tempfile.TemporaryDirectory() as d:
            cases = {
                "comment.yaml": "# RELAY_ENVIRONMENTS stays unset\nkind: Component\n",
                "env.yaml": "spec:\n  containers:\n  - name: relay\n    env:\n    - {name: RELAY_ENVIRONMENTS, value: '1'}\n",
                "inline.yaml": "patches:\n- patch: |-\n    - op: add\n      path: /x\n      value: {name: RELAY_ENVIRONMENTS, value: '1'}\n",
            }
            found = {}
            for name, body in cases.items():
                (Path(d) / name).write_text(body, encoding="utf-8")
                found[name] = bool(sets_environments(Path(d) / name))
        self.assertEqual(found, {"comment.yaml": False, "env.yaml": True, "inline.yaml": True})


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
