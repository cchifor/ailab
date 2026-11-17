#!/usr/bin/env python3
"""CI half of the researcher installation's reproducibility (runbook steps I4 and I5).

scripts/relay-researcher-reproduce.py re-runs the I4 generator and the I5 import from the pinned
Relay release and requires byte-identical results. It needs a checkout of the private Relay
repository and its private release assets, which ailab's CI holds no credential for, so the operator
runs it (`just relay-researcher-reproduce`). This suite checks, in every runbook state, what CI can
check without them: the committed I4 output, the I5 pins and agent config, the provenance record
(docs/relay-workers/researcher-i5-provenance.md, once I5 commits it) and the pinned inputs all agree
with each other. Before I5 the pins and the release-derived config are placeholders, together.
"""
from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "kubernetes/apps/relay-researcher"
INPUTS = ROOT / "docs/relay-workers/researcher-binding-inputs.json"
OUTPUT = ROOT / "docs/relay-workers/researcher-i4"
PROVENANCE = ROOT / "docs/relay-workers/researcher-i5-provenance.md"
PLACEHOLDER = "OPERATOR-FILLS"
# The agent config leaves the I5 import fills; before I5 each is a placeholder.
IMPORTED_CONFIG_LEAVES = (("identity", "installationId"), ("identity", "installationPublicKey"),
                          ("policy", "installation"), ("policy", "podTiers"), ("policy", "allowedRuntimeDigests"),
                          ("policy", "allowedTemplates"))
HEX = r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{40})(?![0-9a-f])"
ABBREVIATED = r"(?<![0-9a-f])([0-9a-f]{6,})…([0-9a-f]{4,})(?![0-9a-f])"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def releases(inputs: dict) -> list[dict]:
    """The pinned releases: `release`, then `agentRelease` when an agent-only update pins one."""
    return [inputs["release"], *([inputs["agentRelease"]] if "agentRelease" in inputs else [])]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class CommittedState(unittest.TestCase):
    def setUp(self):
        self.bundle = module("relay_researcher_bundle", ROOT / "scripts/relay-researcher-bundle.py")
        self.inputs = load(INPUTS)
        self.summary = load(OUTPUT / "summary.json")
        self.definition = load(OUTPUT / "definition.json")
        self.binding = self.definition["binding"]
        self.pins = self.bundle.pins()
        self.config_text = (TREE / "agent/config.json").read_text(encoding="utf-8")
        self.config = json.loads(self.config_text)
        # Step I5 has landed unless every pin is still the reviewed placeholder (the bundle script's predicate).
        self.imported = not self.bundle.pins_are_placeholders(self.pins)
        # During the qualification campaign: the S-A generator and its validated committed run.
        run = self.bundle.campaign_run()
        self.campaign = None if run is None else (self.bundle.candidate_generator(), run)

    def require_imported(self):
        if not self.imported:
            self.skipTest("the pins are placeholders: step I5 has not landed")

    def test_pins_and_config_are_placeholders_or_imported_together(self):
        # I5 commits both files from one import; half of it is never a reviewed state.
        if self.imported:
            self.assertTrue(self.bundle.pins_filled(self.pins), f"pins are neither placeholders nor valid: {self.pins}")
            self.assertNotIn(PLACEHOLDER, self.config_text, "pins are imported but agent/config.json is not")
        else:
            for section, leaf in IMPORTED_CONFIG_LEAVES:
                self.assertTrue(self.bundle.is_placeholder(self.config[section][leaf]),
                                f"pins are placeholders but agent/config.json {section}.{leaf} is filled")

    def test_imported_pins_are_the_i4_output(self):
        self.require_imported()
        release, approved = self.inputs["release"], self.inputs["installation"]
        # The agent image is the last pinned release's (agentRelease's after an agent-only update); the
        # source commit is always the runtime's.
        self.assertEqual(self.pins, {"installationId": approved["installationId"],
                                     "agentImage": releases(self.inputs)[-1]["agentImage"],
                                     "sourceCommit": release["sourceCommit"]})
        self.assertEqual((self.summary["installationId"], self.summary["agentImage"],
                          self.summary["release"]["sourceCommit"]),
                         (self.pins["installationId"], self.pins["agentImage"], self.pins["sourceCommit"]))
        # import writes each value JSON-quoted (relay-researcher-bundle.py write_pins).
        text = (TREE / "pins/pins.yaml").read_text(encoding="utf-8")
        for field, value in self.pins.items():
            self.assertEqual(len(re.findall(rf"^  {field}: {re.escape(json.dumps(value))}$", text, re.M)), 1, field)

    def expected_import(self) -> dict:
        """agent/config.json as the I5 import writes it for the committed I4 definition, built here from
        the definition, binding and summary (Relay's AgentConfig for this installation's inputs)."""
        d = self.definition
        identity = {k: d["identity"][k] for k in ("tenantId", "installationId", "clusterUid", "controlNamespace",
                                                  "installationPublicKey", "fenceObjectUid") if k in d["identity"]}
        identity["installationKeyFile"] = "/run/relay/installation/installation.pem"
        if "enrollmentTokenSecret" in d["references"]:
            identity["enrollmentTokenFile"] = "/run/relay/enrollment/token"
        return {
            "version": 1,
            "hub": {"origin": d["hub"]["origin"], "caFile": "/run/relay/hub-ca/ca.crt"},
            "kubernetes": {"origin": d["kubernetesOrigin"], "caFile": "/run/relay/kubernetes/ca.crt",
                           "tokenFile": "/run/relay/kubernetes/token"},
            "identity": identity,
            "leadership": {"leaseDurationSeconds": 15, "renewDeadlineSeconds": 10, "retryPeriodSeconds": 2},
            "policy": {
                "installation": d["installationPolicy"],
                "executionSlots": d["executionSlots"],
                "podTiers": [{k: p[k] for k in ("tier", "binding", "runtimeClassName")} for p in self.binding["placements"]],
                "allowedRuntimeDigests": [self.summary["runtimeDigest"]],
                "allowedTemplates": [{"templateId": t["definition"]["templateId"], "revision": t["definition"]["revision"],
                                      "digest": t["digest"]} for t in self.binding["templates"]],
                "bootstrapSecrets": False,
                "retainedStorageClass": None,
            },
        }

    def test_imported_config_is_the_i4_definition(self):
        """The committed config is exactly the I5 import of the committed definition, byte for byte in
        Relay canonicalJson; during the qualification campaign, exactly the S-A generator's
        candidate_config of it (policy.bootstrapSecrets false -> true and the run's candidateRuntime).
        Any other field, key or value fails here."""
        self.require_imported()
        expected = self.expected_import()
        self.assertEqual(self.config["policy"]["allowedTemplates"], [self.summary["template"]])
        if self.campaign is not None:
            gen, run = self.campaign
            expected = gen.candidate_config(expected, run)
            self.assertEqual(set(expected["candidateRuntime"]), set(gen.AGENT_PATHS) | {
                "bindingSha256", "trustSha256", "trustConfigMap", "pinConfigMap", "holderSecret"})
        self.assertEqual(self.config, expected)
        self.assertEqual(self.config_text, json.dumps(expected, sort_keys=True, separators=(",", ":"),
                                                      ensure_ascii=False) + "\n")

    def known_digests(self) -> set[str]:
        release, summary = self.inputs["release"], self.summary
        return {*release["assets"].values(), release["runtimeDigest"], release["sourceCommit"],
                release["runtimeImage"].rsplit(":", 1)[1], release["agentImage"].rsplit(":", 1)[1],
                summary["policyDigest"], summary["bindingDigest"], summary["template"]["digest"],
                *summary["files"].values(), *self.definition["installationPolicy"]["allowedImageDigests"],
                *(d for r in releases(self.inputs)[1:]
                  for d in (*r["assets"].values(), r["sourceCommit"], r["agentImage"].rsplit(":", 1)[1]))}

    def expected_by_field(self) -> dict:
        """Each committed field the provenance record must state: (its hashes, its other values). Every
        hash must appear (whole or abbreviated) and no other hash may; every other value must appear."""
        release, approved, policy = self.inputs["release"], self.inputs["installation"], self.config["policy"]
        (template,) = policy["allowedTemplates"]
        repository, agent_digest = releases(self.inputs)[-1]["agentImage"].split("@sha256:")
        ceiling = policy["installation"]["resourceCeiling"]
        return {
            "installationId": (set(), {approved["installationId"]}),
            "installationPublicKey": (set(), {approved["installationPublicKey"], approved["publicKeyFingerprint"]}),
            "agentImage": ({agent_digest}, {f"{repository}@sha256:"}),
            "sourceCommit": ({release["sourceCommit"]}, set()),
            "allowedRuntimeDigests": (set(policy["allowedRuntimeDigests"]), set()),
            "allowedImageDigests": (set(policy["installation"]["allowedImageDigests"]), set()),
            "allowedHarnessAdapters": (set(), set()),
            "allowedTemplates": ({template["digest"]}, {template["templateId"], f"rev {template['revision']}"}),
            "allowedRegistries": (set(), set()),
            # (tier, binding) pairs, in that order.
            "podTiers": (set(), {(tier["tier"], tier["binding"]) for tier in policy["podTiers"]}),
            # One value, so each quantity keeps its position (memory is not scratch).
            "resourceCeiling": (set(), {f"{ceiling['milliCpu']}m / {ceiling['memoryMiB']} MiB / "
                                        f"{ceiling['scratchMiB']} MiB scratch / {ceiling['maxProcesses']} processes"}),
            "maxLeaseSeconds": (set(), {f"{policy['installation']['maxLeaseSeconds']} s"}),
            "maxLifetimeSeconds": (set(), {f"{policy['installation']['maxLifetimeSeconds']} s"}),
            "allowedEgressDestinations": (set(), set()),
        }

    def expected_lists(self) -> dict:
        """List fields the record states as a JSON list: it must state exactly the committed list."""
        installation = self.config["policy"]["installation"]
        return {"allowedHarnessAdapters": installation["allowedHarnessAdapters"],
                "allowedRegistries": installation["allowedRegistries"],
                "allowedEgressDestinations": installation["allowedEgressDestinations"]}

    @staticmethod
    def find(value: str, text: str, start: int = 0) -> int:
        """Where `value` appears in `text` as a whole value, not as part of a longer one (rev 1 is not
        rev 10, registry.chifor.me is not staging.registry.chifor.me), or -1."""
        word = r"[\w.-]"
        before = rf"(?<!{word})" if re.match(word, value[0]) else ""
        after = rf"(?!{word})" if re.match(word, value[-1]) else ""
        match = re.compile(before + re.escape(value) + after).search(text, start)
        return match.start() if match else -1

    def states(self, value, text: str) -> bool:
        """A whole value, or a tuple of whole values stated in that order."""
        position = 0
        for part in (value if isinstance(value, tuple) else (value,)):
            position = self.find(part, text, position)
            if position < 0:
                return False
            position += len(part)
        return True

    @staticmethod
    def quoted_hashes(text: str) -> list[tuple[str, str]]:
        """Every hash in `text` as (prefix, suffix): whole ones twice over, abbreviated (prefix…suffix) ones as written."""
        return [(d, d) for d in re.findall(HEX, text)] + re.findall(ABBREVIATED, text)

    def assert_hashes(self, text: str, allowed: set[str], where: str, required: set[str] = frozenset()):
        """Every hash in `text` is one of `allowed`, and every one of `required` is quoted."""
        quoted = self.quoted_hashes(text)
        for prefix, suffix in quoted:
            self.assertTrue(any(a.startswith(prefix) and a.endswith(suffix) for a in allowed),
                            f"{where}: {prefix}…{suffix} is not {sorted(allowed)}")
        for digest in required:
            self.assertTrue(any(digest.startswith(p) and digest.endswith(s) for p, s in quoted),
                            f"{where}: does not state {digest}")

    def test_provenance_agrees_with_the_pins(self):
        self.require_imported()
        # Step I5 commits the record with the import; the pins never land without it.
        self.assertTrue(PROVENANCE.is_file(), f"{PROVENANCE.relative_to(ROOT)} is missing")
        text = PROVENANCE.read_text(encoding="utf-8")
        # The worker assets it lists are exactly the pinned files and hashes (whole or abbreviated), of
        # every pinned release (the agent release's too after an agent-only update).
        assets = {}
        for r in releases(self.inputs):
            for name, digest in r["assets"].items():
                assets.setdefault(name, set()).add(digest)
        listed = {}
        for name, rest in re.findall(r"^\s*[-*]\s*`?([\w.-]+\.json)`?\s*:\s*(.*)$", text, re.M):
            listed.setdefault(name, []).append(rest)
        self.assertEqual(set(listed), {n for n in assets if n.startswith("worker-")})
        # A name both releases pin (with different bytes) is listed once per release: every digest stated.
        for name, rests in listed.items():
            self.assert_hashes(" ".join(rests), assets[name], name, required=assets[name])
        # Each committed field has a table row that states its value: exactly its hashes, all of them,
        # and its other values. Rows are matched by the field's name with or without backticks.
        expected, seen = self.expected_by_field(), set()
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
            if len(cells) < 2 or set(cells[0]) <= set("-: "):
                continue
            named = [f for f in expected if re.search(rf"(?<![A-Za-z]){f}(?![A-Za-z])", cells[0])]
            if not named:
                self.assert_hashes(line, self.known_digests(), f"row {cells[0]!r}")
                continue
            seen.update(named)
            digests = set().union(*(expected[f][0] for f in named))
            self.assert_hashes(cells[1], digests, f"row {cells[0]!r}", required=digests)
            values = set().union(*(expected[f][1] for f in named))
            if {"maxLeaseSeconds", "maxLifetimeSeconds"} <= set(named):
                # Named together, the values follow the names' order (a lease equal to the lifetime is not the lifetime).
                (lease,), (lifetime,) = expected["maxLeaseSeconds"][1], expected["maxLifetimeSeconds"][1]
                self.assertLess(self.find("maxLeaseSeconds", cells[0]), self.find("maxLifetimeSeconds", cells[0]),
                                f"row {cells[0]!r}: name maxLeaseSeconds before maxLifetimeSeconds")
                values = (values - {lease, lifetime}) | {(lease, lifetime)}
            for value in values:
                if value != self.inputs["installation"]["publicKeyFingerprint"]:
                    self.assertTrue(self.states(value, cells[1]), f"row {cells[0]!r} does not state {value!r}")
            for field, listed in self.expected_lists().items():
                if field in named:
                    stated = [json.loads(v) for v in re.findall(r"`(\[[^`]*\])`", cells[1])]
                    self.assertEqual(stated, [listed], f"row {cells[0]!r} must state exactly {listed}")
        self.assertEqual(seen, set(expected), "the provenance record does not state every imported field")
        # The releases it names are the pinned ones (release, and agentRelease after an agent-only update):
        # every "release <id>" and, where the tag follows it ("release <id> (<tag>, ..."), that tag, each
        # named with its tag. Other version mentions (tools) are not a release.
        pinned = {(str(r["giteaReleaseId"]), r["tag"]) for r in releases(self.inputs)}
        self.assertEqual(set(re.findall(r"\brelease (\d+)\b", text)), {rid for rid, _ in pinned})
        tagged = re.findall(r"\brelease (\d+)\s*\((v\d+\.\d+\.\d+)(?![\w.])", text)
        self.assertTrue(tagged, "the record does not name the release with its tag")
        self.assertEqual(set(tagged), pinned)
        # Anything else it quotes is a pinned or derived hash.
        self.assert_hashes(text, self.known_digests(), "record")
        self.assertTrue(self.states(self.inputs["installation"]["publicKeyFingerprint"], text),
                        "the record does not state the approved key fingerprint")

    def test_the_reproduction_compares_what_the_runbook_commits(self):
        script = module("relay_researcher_reproduce", ROOT / "scripts/relay-researcher-reproduce.py")
        self.assertEqual(sorted(script.I4_FILES), sorted(p.name for p in OUTPUT.iterdir()))
        self.assertEqual(script.I4_OUTPUT, str(OUTPUT.relative_to(ROOT)))
        self.assertEqual(set(script.IMPORTED), {str((TREE / "pins/pins.yaml").relative_to(ROOT)),
                                                str((TREE / "agent/config.json").relative_to(ROOT))})
        self.assertEqual(script.INPUTS, str(INPUTS.relative_to(ROOT)))
        recipe = (ROOT / "justfile").read_text(encoding="utf-8")
        self.assertIn("scripts/relay-researcher-reproduce.py", recipe)
        # An agent-only update reproduces with the agent release's assets as well (fourth argument).
        self.assertIn('relay-researcher-reproduce relay assets ref="HEAD" agent_assets="":', recipe)
        self.assertIn('" --agent-assets " + agent_assets', recipe)


if __name__ == "__main__":
    unittest.main()
