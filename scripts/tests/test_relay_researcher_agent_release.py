#!/usr/bin/env python3
"""Agent-only updates of the researcher installation (owner decision 2026-11-17, runbook "Agent-only update").

An optional `agentRelease` in docs/relay-workers/researcher-binding-inputs.json pins a LATER Relay
release from which ONLY the infrastructure-agent image is taken; the worker runtime, the binding, the
template and definition.sourceCommit stay `release`'s. Two halves:

  * CI (always): the bundle tool's pin rule (relay-researcher-bundle.py check_pins) and the
    reproduce script's --agent-assets rule, on synthetic values.
  * Relay (only with RELAY_RESEARCHER_RELAY=<the pinned Relay checkout, `npm ci` done> and
    RELAY_RESEARCHER_ASSETS=<the assets `release` pins>, Node 22.21+ and helm on PATH; CI has none of
    them, because the Relay repository and its release assets are private). In a copy of this checkout,
    with a SYNTHETIC agent release (receipt, manifest.json and SHA256SUMS of a fake later version with
    another source commit and agent digest, beside decoy runtime files that nothing may read), it runs
    the real generator, Relay's prepare-pod-installation.ts and the bundle tool, and requires: without
    `agentRelease` the committed output byte for byte; with it, a definition that differs ONLY in
    agentImage, a byte-identical binding.json and template-preparation-input.json, the same binding
    digest, bundles that differ only in the agent image, and an import that changes only the agentImage
    pin. It then commits that state in the copy, runs the CI suites on it and reproduces it with
    --agent-assets. Each refusal the generator owes is run too.

  RELAY_RESEARCHER_RELAY=/path/to/relay RELAY_RESEARCHER_ASSETS=/operator/release-assets \\
    python3 -m unittest scripts/tests/test_relay_researcher_agent_release.py
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
INPUTS = "docs/relay-workers/researcher-binding-inputs.json"
I4 = "docs/relay-workers/researcher-i4"
PINS = "kubernetes/apps/relay-researcher/pins/pins.yaml"
CONFIG = "kubernetes/apps/relay-researcher/agent/config.json"
PROVENANCE = "docs/relay-workers/researcher-i5-provenance.md"
RELAY = os.environ.get("RELAY_RESEARCHER_RELAY")
ASSETS = os.environ.get("RELAY_RESEARCHER_ASSETS")
REPOSITORY = "registry.chifor.me/relay/infrastructure-agent"
# The synthetic later release: another version, source commit and agent digest.
FAKE_TAG, FAKE_RELEASE_ID = "v0.10.5", 999001
FAKE_SOURCE = hashlib.sha1(b"synthetic relay v0.10.5").hexdigest()
FAKE_DIGEST = hashlib.sha256(b"synthetic infrastructure-agent v0.10.5").hexdigest()
FAKE_RECEIPT = "worker-image-infrastructure-agent-999001.json"
# A whole 64- or 40-hex value, or an abbreviated one (prefix…suffix), as the provenance record quotes them.
HASH = re.compile(r"(?<![0-9a-f])(?:([0-9a-f]{64}|[0-9a-f]{40})|([0-9a-f]{6,})…([0-9a-f]{4,}))(?![0-9a-f])")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def substitute_hashes(text: str, mapping: dict[str, str]) -> str:
    """Every quoted hash that `mapping` names, whole or abbreviated, replaced in the same form."""
    def one(match):
        if match.group(1):
            return mapping.get(match.group(1), match.group(1))
        prefix, suffix = match.group(2), match.group(3)
        hits = {b for a, b in mapping.items() if a.startswith(prefix) and a.endswith(suffix)}
        if len(hits) != 1:
            return match.group(0)
        (b,) = hits
        return f"{b[:len(prefix)]}…{b[-len(suffix):]}"
    return HASH.sub(one, text)


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def committed_inputs() -> dict:
    return json.loads((ROOT / INPUTS).read_text(encoding="utf-8"))


def agent_release(**overrides) -> dict:
    """A synthetic `agentRelease` (its asset sha256s are filled by write_agent_release)."""
    return {"tag": FAKE_TAG, "giteaReleaseId": FAKE_RELEASE_ID, "sourceCommit": FAKE_SOURCE, "assets": {},
            "agentReceipt": FAKE_RECEIPT, "agentImage": f"{REPOSITORY}@sha256:{FAKE_DIGEST}", **overrides}


class PinRule(unittest.TestCase):
    """relay-researcher-bundle.py check_pins: installationId and sourceCommit always exactly; agentImage
    exactly, unless the bundle's is the one the reviewed inputs pin (agentRelease's, else release's)."""

    def setUp(self):
        self.bundle = module("relay_researcher_bundle", ROOT / "scripts/relay-researcher-bundle.py")
        self.inputs = committed_inputs()
        self.release = self.inputs["release"]
        self.pins = {"installationId": "b380ff7f-0716-478d-8b01-3d39f30e750b",
                     "agentImage": self.release["agentImage"], "sourceCommit": self.release["sourceCommit"]}
        self.later = agent_release()["agentImage"]

    def definition(self, **overrides) -> dict:
        d = {"identity": {"installationId": self.pins["installationId"]}, "agentImage": self.pins["agentImage"],
             "sourceCommit": self.pins["sourceCommit"]}
        d.update(overrides)
        return d

    def check(self, pins: dict, definition: dict, agent: dict | None) -> str | None:
        """None when accepted, else the refusal, with the inputs carrying `agent` as agentRelease (or none)."""
        inputs = copy.deepcopy(self.inputs)
        inputs.pop("agentRelease", None)
        if agent is not None:
            inputs["agentRelease"] = agent
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inputs.json"
            path.write_text(json.dumps(inputs), encoding="utf-8")
            with mock.patch.object(self.bundle, "INPUTS", path):
                try:
                    self.bundle.check_pins(pins, definition)
                except self.bundle.Mismatch as error:
                    return str(error)
        return None

    def test_without_an_agent_release_every_pin_is_exact(self):
        self.assertIsNone(self.check(self.pins, self.definition(), None))
        for field, value in (("agentImage", self.later), ("sourceCommit", FAKE_SOURCE)):
            with self.subTest(field):
                self.assertIn("pins.yaml", self.check(self.pins, self.definition(**{field: value}), None))
        other = self.definition(identity={"installationId": "00000000-0000-4000-8000-000000000000"})
        self.assertIn("pins.yaml differs", self.check(self.pins, other, None))

    def test_an_agent_release_may_change_only_the_agent_image(self):
        agent = agent_release()
        # Before import: the pins still name release's agent; the bundle carries the reviewed later one.
        self.assertIsNone(self.check(self.pins, self.definition(agentImage=self.later), agent))
        # After import: the pins name the later agent.
        imported = {**self.pins, "agentImage": self.later}
        self.assertIsNone(self.check(imported, self.definition(agentImage=self.later), agent))
        # A stale bundle (release's agent) no longer matches the imported pins or the inputs.
        self.assertIn("agentImage differs", self.check(imported, self.definition(), agent))
        # An agent image nobody reviewed.
        unreviewed = f"{REPOSITORY}@sha256:{'7' * 64}"
        self.assertIn("agentImage differs", self.check(self.pins, self.definition(agentImage=unreviewed), agent))
        # The source commit stays the runtime's: the agent release's is refused, with or without its image.
        for image in (self.pins["agentImage"], self.later):
            with self.subTest(image=image):
                self.assertIn("pins.yaml differs", self.check(
                    self.pins, self.definition(agentImage=image, sourceCommit=FAKE_SOURCE), agent))

    def test_removing_the_agent_release_reverts_to_release_agent(self):
        imported = {**self.pins, "agentImage": self.later}
        self.assertIsNone(self.check(imported, self.definition(), None))

    def test_malformed_inputs_are_a_refusal_not_a_traceback(self):
        for agent in ({}, {"agentImage": "registry.chifor.me/relay/infrastructure-agent:v0.10.5"}, "v0.10.5", None):
            with self.subTest(agent=agent):
                inputs = copy.deepcopy(self.inputs)
                inputs["agentRelease"] = agent
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "inputs.json"
                    path.write_text(json.dumps(inputs), encoding="utf-8")
                    with mock.patch.object(self.bundle, "INPUTS", path):
                        with self.assertRaisesRegex(self.bundle.Mismatch, "pins no digest-only agentImage in agentRelease"):
                            self.bundle.check_pins(self.pins, self.definition(agentImage=self.later))

    def test_placeholder_pins_are_not_compared(self):
        placeholders = {k: f"operator-fills-{k}" for k in self.pins}
        self.assertIsNone(self.check(placeholders, self.definition(agentImage=self.later), None))


class ReproduceArguments(unittest.TestCase):
    """relay-researcher-reproduce.py: --agent-assets exactly when the inputs at the ref pin an agentRelease."""

    def reproduce(self, inputs: dict, agent_assets: Path | None) -> str:
        script = module("relay_researcher_reproduce", ROOT / "scripts/relay-researcher-reproduce.py")

        def export(ref, destination):
            (destination / INPUTS).parent.mkdir(parents=True)
            (destination / INPUTS).write_text(json.dumps(inputs), encoding="utf-8")
            return "0" * 40

        with tempfile.TemporaryDirectory() as tmp:
            relay = Path(tmp) / "relay"
            (relay / "node_modules/.bin").mkdir(parents=True)
            (relay / "node_modules/.bin/tsx").write_text("", encoding="utf-8")
            # Stop after argument validation regardless of whether TMPDIR is inside a git worktree.
            with mock.patch.object(script, "export", export), \
                    mock.patch.object(script, "run", side_effect=script.Difference("fixture checkout")), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    script.reproduce(relay, Path(tmp), "HEAD", agent_assets)
                except script.Difference as error:
                    return str(error)
        self.fail("reproduce did not stop on the fake checkout")

    def test_agent_assets_follow_the_inputs(self):
        inputs = committed_inputs()
        inputs.pop("agentRelease", None)
        self.assertIn("--agent-assets is refused", self.reproduce(inputs, Path("/nonexistent")))
        self.assertNotIn("agent", self.reproduce(inputs, None))
        inputs["agentRelease"] = agent_release()
        self.assertIn("pins an agentRelease: --agent-assets is required", self.reproduce(inputs, None))
        self.assertNotIn("agent", self.reproduce(inputs, Path("/nonexistent")))


def run(argv, cwd=None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True, text=True)


def write_json(path: Path, value) -> bytes:
    body = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    path.write_bytes(body)
    return body


def write_agent_release(directory: Path, release_assets: Path, release: dict, *, receipt=None, manifest=None,
                        sums=None, **overrides) -> dict:
    """Write a synthetic later release's assets into a new `directory` and return its `agentRelease`.
    `receipt` / `manifest` mutate the fixtures before they are pinned; `sums` replaces SHA256SUMS. Decoy
    runtime files (another runtime manifest and runtime receipt under release's names) sit beside them:
    nothing may read them."""
    directory.mkdir()
    agent = agent_release(**overrides)
    r = json.loads((release_assets / release["agentReceipt"]).read_text(encoding="utf-8"))
    tag = f"{REPOSITORY}:source-{FAKE_SOURCE}-{'0' * 32}"
    r.update(sourceCommit=FAKE_SOURCE, localTag=tag)
    r["publication"].update(image=f"{REPOSITORY}@sha256:{FAKE_DIGEST}", manifestSha256=FAKE_DIGEST, tag=tag)
    if receipt:
        receipt(r)
    receipt_body = write_json(directory / agent["agentReceipt"], r)
    m = json.loads((release_assets / "manifest.json").read_text(encoding="utf-8"))
    # The fake keeps the real release manifest's shape: its version is the tag itself, "v" included
    # (the worker-runtime manifest's drops it), and its sourceCommit the release's.
    assert (m["version"], m["sourceCommit"]) == (release["tag"], release["sourceCommit"]), m["version"]
    m.update(version=FAKE_TAG, sourceCommit=FAKE_SOURCE)
    if manifest:
        manifest(m)
    manifest_body = write_json(directory / "manifest.json", m)
    sums_text = sums if sums is not None else (
        f"{sha256(b'connector')}  relay-connector-linux-amd64\n{sha256(receipt_body)}  {agent['agentReceipt']}\n")
    (directory / "SHA256SUMS").write_text(sums_text, encoding="utf-8")
    for name in (release["runtimeManifest"], release["runtimeReceipt"]):
        decoy = json.loads((release_assets / name).read_text(encoding="utf-8"))
        decoy["sourceCommit"] = FAKE_SOURCE
        write_json(directory / name, decoy)
    if not agent["assets"]:
        agent["assets"] = {"SHA256SUMS": sha256(sums_text.encode("utf-8")), "manifest.json": sha256(manifest_body),
                           agent["agentReceipt"]: sha256(receipt_body)}
    return agent


@unittest.skipUnless(RELAY and ASSETS, "needs RELAY_RESEARCHER_RELAY and RELAY_RESEARCHER_ASSETS (the private "
                                       "Relay checkout and release assets; CI has neither)")
class SyntheticAgentRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.relay, cls.assets = Path(RELAY).resolve(), Path(ASSETS).resolve()
        cls.tsx = cls.relay / "node_modules/.bin/tsx"
        cls.tmp = Path(tempfile.mkdtemp(prefix="relay-agent-release-"))  # 0700, as prepare requires
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)  # also when the setup below fails
        cls.tree = cls.tmp / "tree"
        files = run(["git", "-C", ROOT, "ls-files", "-z"]).stdout.split("\0")
        for name in filter(None, files):
            if (ROOT / name).is_file():
                (cls.tree / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, cls.tree / name)
        # The runtime-only inputs: the committed ones without any agentRelease (the committed state may
        # already be a real agent-only update; the synthetic one then replaces it).
        text = (cls.tree / INPUTS).read_text(encoding="utf-8")
        cls.inputs = json.loads(text)
        cls.committed_agent = cls.inputs.pop("agentRelease", None)
        cls.runtime_inputs_text = text if cls.committed_agent is None else json.dumps(cls.inputs, indent=2) + "\n"
        cls.release = cls.inputs["release"]
        cls.fence = json.loads((ROOT / I4 / "definition.json").read_text(encoding="utf-8"))["identity"].get(
            "fenceObjectUid")
        cls.counter = 0

        cls.agent_dir = cls.tmp / "agent-assets"
        cls.agent = write_agent_release(cls.agent_dir, cls.assets, cls.release)
        cls.base = cls.generate(None)
        cls.updated = cls.generate(cls.agent, cls.agent_dir)
        for result in (cls.base, cls.updated):
            assert result[0].returncode == 0, result[0].stderr
        cls.base_bundle = cls.prepare(cls.base[1])
        cls.updated_bundle = cls.prepare(cls.updated[1])

    @classmethod
    def set_inputs(cls, agent: dict | None) -> None:
        """The copy's inputs: the runtime-only inputs, plus `agent` as agentRelease."""
        if agent is None:
            (cls.tree / INPUTS).write_text(cls.runtime_inputs_text, encoding="utf-8")
        else:
            write_json(cls.tree / INPUTS, {**cls.inputs, "agentRelease": agent})

    @classmethod
    def generate(cls, agent: dict | None, agent_assets: Path | None = None, *, inputs_agent=True):
        """Run the copy's generator; `agent` is the inputs' agentRelease (None: runtime-only inputs)."""
        cls.set_inputs(agent if inputs_agent else None)
        cls.counter += 1
        out = cls.tmp / f"i4-{cls.counter}"
        identity = cls.inputs["installation"]
        done = run([cls.tsx, cls.tree / "scripts/relay-researcher-binding.mts", "--relay", cls.relay,
                    "--assets", cls.assets, "--installation-id", identity["installationId"],
                    "--installation-public-key", identity["installationPublicKey"], "--out", out,
                    *(["--fence-object-uid", cls.fence] if cls.fence else []),
                    *(["--agent-assets", agent_assets] if agent_assets is not None else [])])
        return done, out

    @classmethod
    def prepare(cls, i4: Path) -> Path:
        """Relay's prepare-pod-installation.ts on the definition, as the runbook (and reproduce) run it."""
        cls.counter += 1
        work = cls.tmp / f"in-{cls.counter}"
        work.mkdir(mode=0o700)
        definition = work / "definition.json"
        shutil.copyfile(i4 / "definition.json", definition)
        definition.chmod(0o644)
        bundle = cls.tmp / f"bundle-{cls.counter}"
        done = run([cls.tsx, "scripts/prepare-pod-installation.ts", definition, sha256(definition.read_bytes()), bundle],
                   cwd=cls.relay)
        assert done.returncode == 0, done.stderr
        return bundle

    def bundle_tool(self, command: str, bundle: Path, chart: bool = True) -> subprocess.CompletedProcess:
        return run([sys.executable, self.tree / "scripts/relay-researcher-bundle.py", command, bundle,
                    *(["--chart", self.relay] if chart else [])])

    def read(self, directory: Path, name: str) -> bytes:
        return (directory / name).read_bytes()

    def test_1_without_an_agent_release_the_committed_output(self):
        out, committed = self.base[1], ROOT / I4
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(p.name for p in committed.iterdir()))
        self.assertNotIn("agentRelease", json.loads(self.read(out, "summary.json")))
        if self.committed_agent is None:
            for name in (p.name for p in out.iterdir()):
                self.assertEqual(self.read(out, name), self.read(committed, name), name)
            return
        # The committed state is a real agent-only update: the runtime-only output is it, but release's agent.
        for name in ("binding.json", "template-preparation-input.json"):
            self.assertEqual(self.read(out, name), self.read(committed, name), name)
        image = self.committed_agent["agentImage"]
        self.assertEqual(json.loads(self.read(committed, "definition.json"))["agentImage"], image)
        self.assertEqual(self.read(committed, "definition.json").replace(image.encode(), self.release["agentImage"].encode()),
                         self.read(out, "definition.json"))
        s0, s1 = (json.loads(self.read(d, "summary.json")) for d in (out, committed))
        strip = lambda s: {**{k: v for k, v in s.items() if k not in ("agentImage", "agentRelease")},
                           "files": {k: v for k, v in s["files"].items() if k != "definition.json"}}
        self.assertEqual(strip(s1), strip(s0))

    def test_2_agent_release_changes_only_the_agent_image(self):
        base, updated = self.base[1], self.updated[1]
        image = self.agent["agentImage"]
        for name in ("binding.json", "template-preparation-input.json"):
            self.assertEqual(self.read(updated, name), self.read(base, name), name)
        before, after = (json.loads(self.read(d, "definition.json")) for d in (base, updated))
        self.assertEqual((before["agentImage"], after["agentImage"]), (self.release["agentImage"], image))
        self.assertEqual(after["sourceCommit"], self.release["sourceCommit"])
        self.assertEqual({**after, "agentImage": before["agentImage"]}, before)
        # Byte for byte too: the image is the only change in the canonical file.
        self.assertEqual(self.read(updated, "definition.json").replace(image.encode(), self.release["agentImage"].encode()),
                         self.read(base, "definition.json"))
        s0, s1 = (json.loads(self.read(d, "summary.json")) for d in (base, updated))
        config = json.loads(self.read(self.agent_dir, FAKE_RECEIPT))["imageConfigDigest"]
        self.assertEqual(s1["agentRelease"], {"tag": FAKE_TAG, "giteaReleaseId": FAKE_RELEASE_ID,
                                              "sourceCommit": FAKE_SOURCE, "assets": self.agent["assets"],
                                              "agentReceipt": FAKE_RECEIPT, "imageConfigDigest": config})
        self.assertEqual(s1["agentImage"], image)
        self.assertEqual(s1["bindingDigest"], s0["bindingDigest"])
        self.assertEqual(s1["files"]["definition.json"], sha256(self.read(updated, "definition.json")))
        strip = lambda s: {**{k: v for k, v in s.items() if k not in ("agentImage", "agentRelease")},
                           "files": {k: v for k, v in s["files"].items() if k != "definition.json"}}
        self.assertEqual(strip(s1), strip(s0))

    def test_3_bundles_differ_only_in_the_agent_image(self):
        image, old = self.agent["agentImage"], self.release["agentImage"]
        for name in json.loads(self.read(self.base_bundle, "bundle-manifest.json"))["files"]:
            with self.subTest(name):
                self.assertEqual(self.read(self.updated_bundle, name).replace(image.encode(), old.encode()),
                                 self.read(self.base_bundle, name))
        values = json.loads(self.read(self.updated_bundle, "30-agent-values.json"))
        self.assertEqual(values["image"]["reference"], image)
        report = json.loads(self.read(self.updated_bundle, "report.json"))
        self.assertEqual((report["identity"]["agentImage"], report["identity"]["sourceCommit"]),
                         (image, self.release["sourceCommit"]))
        self.assertEqual(self.read(self.updated_bundle, "31-agent-config.json"),
                         self.read(self.base_bundle, "31-agent-config.json"))
        # Both carry the runtime preparation's own timing, which the bundle tool's overlay replaces.
        tool = module("relay_researcher_bundle", ROOT / "scripts/relay-researcher-bundle.py")
        for bundle in (self.base_bundle, self.updated_bundle):
            self.assertEqual(json.loads(self.read(bundle, "31-agent-config.json"))["leadership"],
                             tool.PREPARED_LEADERSHIP)
            self.assertEqual(json.loads(self.read(bundle, "30-agent-values.json"))["leadership"],
                             {"allowStandby": False, **tool.PREPARED_LEADERSHIP})

    def test_4_check_import_and_reproduce_the_agent_only_pin_pr(self):
        self.set_inputs(None)
        done = self.bundle_tool("check", self.base_bundle)
        self.assertEqual(done.returncode, 0, done.stderr)
        # The later agent is refused while the inputs do not pin it.
        done = self.bundle_tool("check", self.updated_bundle, chart=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("agentImage differs from the bundle's definition", done.stderr)

        self.set_inputs(self.agent)
        done = self.bundle_tool("check", self.updated_bundle)
        self.assertEqual(done.returncode, 0, done.stderr)
        pins, config = self.read(self.tree, PINS), self.read(self.tree, CONFIG)
        (previous,) = re.findall(r'^  agentImage: "([^"]+)"$', pins.decode(), re.M)  # release's, or a real update's
        done = self.bundle_tool("import", self.updated_bundle)
        self.assertEqual(done.returncode, 0, done.stderr)
        # pins.yaml differs only in agentImage; the agent config is byte-identical, with the overlay's timing.
        self.assertEqual(self.read(self.tree, CONFIG), config)
        tool = module("relay_researcher_bundle", self.tree / "scripts/relay-researcher-bundle.py")
        self.assertEqual(json.loads(config)["leadership"], tool.LEADERSHIP_OVERLAY)
        changed = [(a, b) for a, b in zip(pins.decode().splitlines(), self.read(self.tree, PINS).decode().splitlines())
                   if a != b]
        self.assertEqual(changed, [(f'  agentImage: "{previous}"', f'  agentImage: "{self.agent["agentImage"]}"')])
        self.assertEqual(len(pins.splitlines()), len(self.read(self.tree, PINS).splitlines()))
        # Once imported, a bundle with release's agent is stale.
        done = self.bundle_tool("check", self.base_bundle, chart=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("agentImage differs from the bundle's definition", done.stderr)
        done = self.bundle_tool("check", self.updated_bundle)
        self.assertEqual(done.returncode, 0, done.stderr)

        # The pin PR: the regenerated I4 output and the provenance record, then the CI suites on it.
        shutil.rmtree(self.tree / I4)
        shutil.copytree(self.updated[1], self.tree / I4)
        env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_RESEARCHER_")}
        # test_relay_candidate_run.py renders the chart from a Relay checkout at pins.yaml's sourceCommit:
        # the one this test generates with (release's commit, which the agent-only update keeps).
        env["RELAY_CHECKOUT"] = str(self.relay)
        suite = lambda name: run([sys.executable, "-m", "unittest", "discover", "-s", self.tree / "scripts/tests",
                                  "-p", name], env=env)
        # Without the provenance record's update, CI refuses the pin PR (the record states the old agent).
        done = suite("test_relay_researcher_reproduction.py")
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAIL: test_provenance_agrees_with_the_pins", done.stderr)
        original = (self.tree / PROVENANCE).read_text(encoding="utf-8")
        # The previous agent image (release's, or a committed real update's) becomes the synthetic one.
        mapping = {previous.rsplit(":", 1)[1]: FAKE_DIGEST}
        real = self.committed_agent
        if real is None:
            record = original.replace("\n| File / field |", (
                f"\nAgent-only update (`agentRelease`): Gitea release {FAKE_RELEASE_ID} ({FAKE_TAG}, source "
                f"`{FAKE_SOURCE}`), with these asset sha256s:\n\n"
                f"- `{FAKE_RECEIPT}`: `{self.agent['assets'][FAKE_RECEIPT]}`\n\n| File / field |"), 1)
        else:
            # The record already states a real agent release: restate it as the synthetic one.
            mapping[real["sourceCommit"]] = FAKE_SOURCE
            for name, digest in real["assets"].items():
                mapping[digest] = self.agent["assets"][FAKE_RECEIPT if name == real["agentReceipt"] else name]
            record = original.replace(f"`{real['agentReceipt']}`", f"`{FAKE_RECEIPT}`")
            record = re.sub(rf"\brelease {real['giteaReleaseId']}\b(\s*\(){re.escape(real['tag'])}",
                            rf"release {FAKE_RELEASE_ID}\g<1>{FAKE_TAG}", record)
            record = re.sub(rf"\brelease {real['giteaReleaseId']}\b", f"release {FAKE_RELEASE_ID}", record)
        record = substitute_hashes(record, mapping)
        self.assertNotEqual(record, original)
        (self.tree / PROVENANCE).write_text(record, encoding="utf-8")
        # Committed in the copy, the reproduction needs and accepts the agent release's assets.
        for argv in (["init", "-q"], ["add", "-A"],
                     ["-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "synthetic agent-only pin"]):
            done = run(["git", "-C", self.tree, *argv])
            self.assertEqual(done.returncode, 0, done.stderr)
        # Every test runs (none skips: the pins are imported, and the copy is a git checkout, which
        # test_relay_candidate_run.py's R0 state check needs), and passes.
        for name in ("test_relay_researcher_binding.py", "test_relay_researcher_reproduction.py",
                     "test_relay_researcher_installation.py", "test_relay_researcher_hub_upgrade.py",
                     "test_relay_candidate_run.py"):
            with self.subTest(name):
                done = suite(name)
                self.assertEqual((done.returncode, done.stderr.rstrip().splitlines()[-1]), (0, "OK"),
                                 done.stderr[-4000:])
        reproduce = [sys.executable, self.tree / "scripts/relay-researcher-reproduce.py", "--relay", self.relay,
                     "--assets", self.assets]
        done = run([*reproduce, "--agent-assets", self.agent_dir])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("reproduced: every committed output is byte-identical", done.stdout)
        self.assertIn("I5: import rewrote pins.yaml and agent/config.json: byte-identical", done.stdout)
        done = run(reproduce)
        self.assertEqual(done.returncode, 1)
        self.assertIn("--agent-assets is required", done.stderr)

    def test_5_refusals(self):
        release = self.release
        runtime_receipt = json.loads((self.assets / release["runtimeReceipt"]).read_text(encoding="utf-8"))

        def as_runtime(r):
            # The runtime receipt of release, moved to the agent release's commit: only its kind differs.
            r.clear()
            r.update(copy.deepcopy(runtime_receipt), sourceCommit=FAKE_SOURCE)
            r["publication"].update(image=f"{REPOSITORY}@sha256:{FAKE_DIGEST}", manifestSha256=FAKE_DIGEST)

        cases = {
            "wrong sha256": (dict(), lambda a: a["assets"].update({FAKE_RECEIPT: "0" * 64}),
                             f"release asset {FAKE_RECEIPT} has sha256"),
            "receipt of another commit": (dict(receipt=lambda r: r.update(sourceCommit=release["sourceCommit"])), None,
                                          f"{FAKE_RECEIPT} is not the infrastructure-agent publish receipt"),
            "runtime receipt as the agent's": (dict(receipt=as_runtime), None,
                                               f"{FAKE_RECEIPT} is not the infrastructure-agent publish receipt"),
            "receipt image is not agentImage": (dict(agentImage=f"{REPOSITORY}@sha256:{'7' * 64}"), None,
                                                "receipt image is not agentRelease.agentImage"),
            "receipt without a config digest": (dict(receipt=lambda r: r.pop("imageConfigDigest")), None,
                                                "receipt has no sha256 imageConfigDigest"),
            "receipt config digest not sha256": (dict(receipt=lambda r: r.update(imageConfigDigest="0" * 64)), None,
                                                 "receipt has no sha256 imageConfigDigest"),
            "manifest of another version": (dict(manifest=lambda m: m.update(version="v0.10.4")), None,
                                            "manifest.json is not the pinned agentRelease"),
            "manifest version without the v": (dict(manifest=lambda m: m.update(version=FAKE_TAG[1:])), None,
                                               "manifest.json is not the pinned agentRelease"),
            "manifest of another commit": (dict(manifest=lambda m: m.update(sourceCommit=release["sourceCommit"])), None,
                                           "manifest.json is not the pinned agentRelease"),
            "SHA256SUMS disagrees": (dict(sums=f"{'1' * 64}  {FAKE_RECEIPT}\n"), None,
                                     f"{FAKE_RECEIPT} differs from SHA256SUMS"),
            "not newer": (dict(), lambda a: a.update(tag=release["tag"]), "is not newer than release"),
            "older": (dict(), lambda a: a.update(tag="v0.10.2"), "is not newer than release"),
            "not a release tag": (dict(), lambda a: a.update(tag="v0.10.5-rc.1"), "is not a vMAJOR.MINOR.PATCH"),
            "another repository": (dict(), lambda a: a.update(agentImage=f"registry.chifor.me/relay/worker-runtime@sha256:{FAKE_DIGEST}"),
                                   "is not a digest-only registry.chifor.me/relay/infrastructure-agent image"),
            "a tag, not a digest": (dict(), lambda a: a.update(agentImage=f"{REPOSITORY}:v0.10.5"),
                                    "is not a digest-only"),
            "a runtime field": (dict(), lambda a: a.update(runtimeImage=release["runtimeImage"]),
                                "agentRelease must be an object with exactly"),
            "an asset path, not a name": (dict(), lambda a: a["assets"].update({"../SHA256SUMS": "0" * 64}),
                                          "is not a map of plain asset file name to sha256"),
            "manifest.json not pinned": (dict(), lambda a: a["assets"].pop("manifest.json"),
                                         "agentRelease.assets does not pin manifest.json"),
            "receipt not pinned": (dict(), lambda a: a["assets"].pop(FAKE_RECEIPT),
                                   f"agentRelease.assets does not pin {FAKE_RECEIPT}"),
        }
        for number, (name, (fixture, mutate, reason)) in enumerate(cases.items()):
            with self.subTest(name):
                directory = self.tmp / f"refusal-{number}"
                agent = write_agent_release(directory, self.assets, release, **fixture)
                if mutate:
                    mutate(agent)
                done, out = self.generate(agent, directory)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(reason, done.stderr)
                self.assertFalse(out.exists())
        with self.subTest("agentRelease without --agent-assets"):
            done, out = self.generate(self.agent, None)
            self.assertEqual(done.returncode, 1)
            self.assertIn("pins an agentRelease: --agent-assets <its asset directory> is required", done.stderr)
        with self.subTest("--agent-assets without agentRelease"):
            done, out = self.generate(self.agent, self.agent_dir, inputs_agent=False)
            self.assertEqual(done.returncode, 1)
            self.assertIn("--agent-assets is refused", done.stderr)
        with self.subTest("agent assets as the release's"):
            self.set_inputs(self.agent)
            done = run([self.tsx, self.tree / "scripts/relay-researcher-binding.mts", "--relay", self.relay,
                        "--assets", self.agent_dir, "--agent-assets", self.agent_dir,
                        "--installation-id", self.inputs["installation"]["installationId"],
                        "--installation-public-key", self.inputs["installation"]["installationPublicKey"],
                        "--out", self.tmp / "swapped"])
            self.assertEqual(done.returncode, 1)
            self.assertIn("has sha256", done.stderr)


if __name__ == "__main__":
    unittest.main()
