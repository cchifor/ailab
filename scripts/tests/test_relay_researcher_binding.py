#!/usr/bin/env python3
"""Source gates for the researcher installation's binding generator (runbook step I4).

scripts/relay-researcher-binding.mts rebuilds the PodRuntimeBinding and the complete I4 definition
from the pinned Relay release with that checkout's own code; it needs the release assets and a Relay
checkout, so CI cannot run it. These tests pin what CI can see without reimplementing a Relay digest:
the committed draft (docs/relay-workers/researcher-i4-draft/) is the reviewed template with exactly
its OPERATOR-FILLS leaves filled, its derived parts equal the template, the RuntimeClass manifest, the
internal TLS and journal manifests and the release pins, and the decisions it carries are the ones in
researcher-binding-inputs.json. A template, manifest or decision change without a regenerated draft
fails here. The draft's Relay digests (template, policy, binding) are computed only by Relay's code;
this suite checks that they are the ones the draft's own summary and definition carry.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs/relay-workers"
INPUTS = DOCS / "researcher-binding-inputs.json"
TEMPLATE = DOCS / "researcher-installation-definition.json"
DRAFT = DOCS / "researcher-i4-draft"
GENERATOR = ROOT / "scripts/relay-researcher-binding.mts"
RUNBOOK = ROOT / "docs/runbooks/relay-researcher-installation.md"
CONTAINMENT = ROOT / "kubernetes/apps/relay-researcher/installation/containment.yaml"
SLOT = "relay-researcher-1"
DRAFT_FILES = {"binding.json", "definition.json", "template-preparation-input.json", "summary.json"}
# Relay at the pinned sourceCommit (c0fe50f0), for the allocation arithmetic only:
# packages/infrastructure-agent/pod-renderer.ts MIN_HARNESS_MILLI_CPU / MIN_HARNESS_MEMORY_MIB /
# MIN_WORKSPACE_MIB / HARNESS_LOG_RESERVE_MIB; packages/worker-runtime/resources.ts RUNTIME_VOLUME_MIB,
# SUPERVISOR_MIN_PROCESS_MIB, SUPERVISOR_DISK_MIB; packages/worker-runtime/binding.ts supervisor bounds.
MIN_HARNESS_MILLI_CPU, MIN_HARNESS_MEMORY_MIB, MIN_WORKSPACE_MIB, HARNESS_LOG_RESERVE_MIB = 250, 256, 256, 32
HOLDER_STATE_MIB, SUPERVISOR_STATE_MIB, SUPERVISOR_MIN_PROCESS_MIB, SUPERVISOR_DISK_MIB = 128, 16, 64, 2
SUPERVISOR_BOUNDS = {"milliCpu": (50, 2000), "memoryMiB": (SUPERVISOR_STATE_MIB + SUPERVISOR_MIN_PROCESS_MIB, 2048),
                     "ephemeralMiB": (SUPERVISOR_DISK_MIB, 1024)}
# Relay ROLE_BASELINES.researcher resources (packages/infrastructure-protocol/baselines.ts).
RESEARCHER_BASELINE = {"milliCpu": 1000, "memoryMiB": 2048, "scratchMiB": 4096}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def bundle_module():
    spec = importlib.util.spec_from_file_location("relay_researcher_bundle", ROOT / "scripts/relay-researcher-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def template() -> dict:
    data = load(TEMPLATE)
    data.pop("_comment")
    return data


def placeholder_paths(value, path=""):
    if isinstance(value, str):
        return [path] if value.startswith("OPERATOR-FILLS") else []
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in placeholder_paths(v, f"{path}/{i}")]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in placeholder_paths(v, f"{path}/{k}")]
    return []


def quantity(text: str) -> int:
    match = re.fullmatch(r"(\d+)(m|Mi)", text)
    assert match, text
    return int(match.group(1))


class Draft(unittest.TestCase):
    def setUp(self):
        self.inputs = load(INPUTS)
        self.binding = load(DRAFT / "binding.json")
        self.definition = load(DRAFT / "definition.json")
        self.summary = load(DRAFT / "summary.json")
        self.manifest = self.binding["runtime"]["manifest"]

    def test_files_are_canonical_and_summarized(self):
        self.assertEqual({p.name for p in DRAFT.iterdir()}, DRAFT_FILES)
        for name in DRAFT_FILES:
            text = (DRAFT / name).read_text(encoding="utf-8")
            # Relay canonicalJson: sorted keys, no whitespace, one trailing newline.
            self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False) + "\n", name)
        for name, digest in self.summary["files"].items():
            self.assertEqual(hashlib.sha256((DRAFT / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(set(self.summary["files"]), DRAFT_FILES - {"summary.json"})

    def test_draft_identity_is_the_placeholder(self):
        draft = self.inputs["draft"]
        self.assertIs(self.summary["draft"], True)
        self.assertEqual(self.binding["installationId"], draft["installationId"])
        self.assertEqual(self.definition["identity"]["installationId"], draft["installationId"])
        self.assertEqual(self.definition["identity"]["installationPublicKey"], draft["installationPublicKey"])
        self.assertTrue(draft["installationPublicKey"].startswith("PLACEHOLDER"))
        # The generator refuses the placeholders outside --draft.
        self.assertIn("the draft placeholder identity is accepted only with --draft", GENERATOR.read_text(encoding="utf-8"))

    def test_definition_is_the_template_with_its_leaves_filled(self):
        bundle = bundle_module()
        try:
            bundle.compare_definition(template(), self.definition)
        except bundle.Mismatch as error:
            self.fail(f"the draft is stale; regenerate it with --draft (runbook step I4): {error}")
        generator = GENERATOR.read_text(encoding="utf-8")
        leaves = placeholder_paths(template())
        self.assertEqual(len(leaves), 7)
        for leaf in leaves:
            self.assertIn(f'"{leaf}"', generator, f"the generator does not fill {leaf}")
        self.assertEqual(self.definition["binding"], self.binding)
        release = self.inputs["release"]
        self.assertEqual(self.definition["sourceCommit"], release["sourceCommit"])
        self.assertEqual(self.definition["agentImage"], release["agentImage"])
        policy = self.definition["installationPolicy"]
        self.assertEqual(policy["allowedImageDigests"], [release["runtimeImage"].rsplit(":", 1)[1]])
        self.assertEqual(policy["allowedHarnessAdapters"], [self.manifest["adapterName"]])
        # The policy the binding's digest covers is the template's (compare_definition), and the
        # digest is the one Relay's preparation reported for this draft.
        self.assertEqual(self.binding["policyDigest"], self.summary["policyDigest"])
        self.assertRegex(self.binding["policyDigest"], r"^[0-9a-f]{64}$")

    def test_runtime_is_the_pinned_release(self):
        release = self.inputs["release"]
        self.assertEqual(self.binding["runtime"]["image"], release["runtimeImage"])
        self.assertEqual(self.manifest["sourceCommit"], release["sourceCommit"])
        self.assertEqual("v" + self.manifest["version"], release["tag"])
        self.assertEqual(self.summary["runtimeDigest"], release["runtimeDigest"])
        self.assertEqual(self.summary["release"]["assets"], release["assets"])
        self.assertEqual(self.summary["release"]["relayCheckoutHead"], release["sourceCommit"])
        self.assertNotIn("releaseApproval", self.binding)
        self.assertEqual(self.inputs["decisions"]["releaseApproval"], "absent")

    def test_g3_fields_in_both_stages(self):
        bundle = bundle_module()
        derived = self.inputs["derived"]
        self.assertEqual(self.binding["hostAliases"], bundle.internal_tls_addresses())
        self.assertEqual(derived["hostAliases"], bundle.internal_tls_addresses())
        self.assertEqual(derived["hubCaPath"], bundle.HUB_CA_PATH)
        self.assertEqual(derived["routerOrigin"], bundle.ROUTER_ORIGIN)
        for stage in ("enrollment", "qualification"):
            bundle.check_internal_tls(self.definition, stage)

    def test_placement(self):
        bundle = bundle_module()
        derived, decisions = self.inputs["derived"]["placement"], self.inputs["decisions"]["placement"]
        (placement,) = self.binding["placements"]
        self.assertEqual(placement, {**decisions, **derived})
        class_selector, class_tolerations = bundle.runtime_class_scheduling()
        # The template's two required keys plus every key RuntimeClass admission merges into the Pod.
        self.assertEqual(placement["nodeSelector"], {"kubernetes.io/hostname": "relay-worker-1",
                                                     "ailab.io/relay-worker-pool": "true", **class_selector})
        self.assertEqual(placement["tolerations"], class_tolerations)
        self.assertEqual(placement["runtimeClassName"], "relay-gvisor")
        text = (ROOT / "kubernetes/infra/agent-nodes/relay-workers.tf").read_text(encoding="utf-8")
        cpu, mem = re.search(r'overhead\s*=\s*\{\s*podFixed\s*=\s*\{\s*cpu\s*=\s*"(\d+)m",\s*memory\s*=\s*"(\d+)Mi"',
                             text).groups()
        self.assertEqual(placement["overhead"], {"milliCpu": int(cpu), "memoryMiB": int(mem)})
        for key, value in template()["controlPlacement"]["nodeSelector"].items():
            if key in placement["nodeSelector"]:
                self.assertNotEqual(placement["nodeSelector"][key], value, "control and worker placements overlap")
        self.assertIn(placement["tier"], ("restricted-pod", "sandboxed-pod"))

    def test_template_is_the_researcher_baseline_with_the_decided_pins(self):
        (sealed,) = self.binding["templates"]
        d, t = sealed["definition"], self.inputs["decisions"]["template"]
        self.assertEqual(self.summary["template"], {"templateId": d["templateId"], "revision": d["revision"],
                                                    "digest": sealed["digest"]})
        self.assertEqual((d["templateId"], d["revision"], d["name"], d["role"]["skills"], d["role"]["toolsets"],
                          d["harness"]["modelPolicy"]),
                         (t["templateId"], t["revision"], t["name"], t["skills"], t["toolsets"], t["modelPolicy"]))
        self.assertEqual(d["role"]["role"], "researcher")
        self.assertEqual(d["runtime"]["image"], self.binding["runtime"]["image"])
        artifacts = {a["installPath"]: a for a in self.manifest["artifacts"]}
        bridge = [a for a in self.manifest["artifacts"] if a["role"] == "bridge"]
        self.assertEqual(d["harness"], {"adapter": self.manifest["adapterName"], "adapterDigest": bridge[0]["sha256"],
                                        "executableDigest": artifacts[self.manifest["harness"]["executable"]]["sha256"],
                                        "modelPolicy": t["modelPolicy"]})
        self.assertEqual(d["network"]["egressDestinations"], self.inputs["derived"]["templateEgressDestinations"])
        self.assertEqual({k: d["resources"][k] for k in RESEARCHER_BASELINE}, RESEARCHER_BASELINE)
        policy = self.definition["installationPolicy"]
        for key, value in d["resources"].items():
            self.assertLessEqual(value, policy["resourceCeiling"][key], key)
        self.assertLessEqual(d["lifetime"]["leaseSeconds"], policy["maxLeaseSeconds"])
        self.assertLessEqual(d["lifetime"]["maxLifetimeSeconds"], policy["maxLifetimeSeconds"])
        # The independent rerun input of Relay scripts/prepare-worker-templates.ts seals the same pins.
        (selection,) = load(DRAFT / "template-preparation-input.json")["selections"]
        self.assertEqual(selection["role"], "researcher")
        self.assertEqual(selection["pins"]["harness"], d["harness"])
        self.assertEqual(selection["pins"]["image"], d["runtime"]["image"])

    def test_supervisor_fits_the_template_the_ceiling_and_the_slot_quota(self):
        supervisor = self.binding["supervisor"]
        self.assertEqual(supervisor, self.inputs["decisions"]["supervisor"])
        for key, (low, high) in SUPERVISOR_BOUNDS.items():
            self.assertTrue(low <= supervisor[key] <= high, key)
        (placement,) = self.binding["placements"]
        ceiling = self.definition["installationPolicy"]["resourceCeiling"]
        quota = next(d for d in yaml.safe_load_all(CONTAINMENT.read_text(encoding="utf-8"))
                     if d and d["kind"] == "ResourceQuota" and d["metadata"]["namespace"] == SLOT)["spec"]["hard"]
        for name, intent in (("researcher template", RESEARCHER_BASELINE), ("G4 ceiling", ceiling)):
            with self.subTest(name):
                # Relay pod-renderer.ts: the supervisor is carved out of the intent, never added to it.
                self.assertGreaterEqual(intent["milliCpu"] - supervisor["milliCpu"], MIN_HARNESS_MILLI_CPU)
                self.assertGreaterEqual(intent["memoryMiB"] - supervisor["memoryMiB"], MIN_HARNESS_MEMORY_MIB)
                workspace = intent["scratchMiB"] - supervisor["ephemeralMiB"] - HOLDER_STATE_MIB - HARNESS_LOG_RESERVE_MIB
                self.assertGreaterEqual(workspace, MIN_WORKSPACE_MIB)
                # The scheduler and the quota see the intent plus the RuntimeClass overhead.
                self.assertLessEqual(intent["milliCpu"] + placement["overhead"]["milliCpu"], quantity(quota["limits.cpu"]))
                self.assertLessEqual(intent["memoryMiB"] + placement["overhead"]["memoryMiB"],
                                     quantity(quota["limits.memory"]))
                self.assertLessEqual(intent["scratchMiB"], quantity(quota["limits.ephemeral-storage"]))

    def test_runbook_i4_uses_the_generator(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        i4 = text[text.index("**I4."):text.index("**I5.")]
        self.assertIn("scripts/relay-researcher-binding.mts", i4)
        self.assertIn("Binding inputs that are decisions", text)
        for decision in ("supervisor", "releaseApproval", "modelPolicy", "templateId", "tier"):
            self.assertIn(decision, text[text.index("Binding inputs that are decisions"):])


if __name__ == "__main__":
    unittest.main()
