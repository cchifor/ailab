#!/usr/bin/env python3
"""Text-preserving agent pin updates and offline refusal checks; optional real-release reproduction.

Set RELAY_RESEARCHER_RELAY, RELAY_RESEARCHER_ASSETS and RELAY_RESEARCHER_AGENT_ASSETS
to run the complete tool on a copy with the currently pinned release (Node 22 and helm on PATH).
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/relay-researcher-agent-pin.py"
spec = importlib.util.spec_from_file_location("agent_pin", ROOT / SCRIPT)
pin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin)
RELAY = os.environ.get("RELAY_RESEARCHER_RELAY")
ASSETS = os.environ.get("RELAY_RESEARCHER_ASSETS")
AGENT_ASSETS = os.environ.get("RELAY_RESEARCHER_AGENT_ASSETS")


class AgentPin(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / pin.INPUTS).read_bytes().decode("utf-8")
        self.inputs = json.loads(self.text)
        self.agent = copy.deepcopy(self.inputs["agentRelease"])
        self.agent.update(tag="v0.10.7", giteaReleaseId=999007, sourceCommit="7" * 40,
                          agentReceipt="worker-image-infrastructure-agent-999007.json",
                          agentImage=self.inputs["release"]["agentImage"].split("@")[0] + "@sha256:" + "7" * 64)
        self.agent["assets"] = {"SHA256SUMS": "1" * 64, "manifest.json": "2" * 64,
                                self.agent["agentReceipt"]: "3" * 64}
        self.receipt = {"kind": "relay-worker-image", "version": 1, "component": "infrastructure-agent",
                        "platform": "linux/amd64", "sourceCommit": self.agent["sourceCommit"],
                        "imageConfigDigest": "sha256:" + "4" * 64, "runtime": None,
                        "publication": {"image": self.agent["agentImage"], "manifestSha256": "7" * 64}}
        self.manifest = {"version": self.agent["tag"], "sourceCommit": self.agent["sourceCommit"]}
        self.tmp = tempfile.TemporaryDirectory(prefix=".agent-pin-test-", dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.assets = Path(self.tmp.name)

    def write_assets(self, *, receipt=None, manifest=None, sums=None):
        for name, value in ((self.agent["agentReceipt"], self.receipt if receipt is None else receipt),
                            ("manifest.json", self.manifest if manifest is None else manifest)):
            (self.assets / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
        if sums is None:
            sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                           for p in (self.assets / "manifest.json", self.assets / self.agent["agentReceipt"]))
        (self.assets / "SHA256SUMS").write_text(sums, encoding="utf-8")

    def read_release(self, **kwargs):
        return pin.read_agent_release(self.assets, kwargs.get("tag", self.agent["tag"]),
                                      kwargs.get("release_id", self.agent["giteaReleaseId"]), self.inputs["release"])

    def test_rewrite_preserves_every_other_byte_and_internal_whitespace(self):
        # Hand formatting, CRLF, braces in a string and alternate indentation must survive.
        for text in (self.text, self.text.replace("\n", "\r\n"),
                     self.text.replace('    "tag":', '\t"tag" :').replace("Inputs of", "Inputs { } of")):
            with self.subTest(format=text[:30]):
                rewritten = pin.rewrite_inputs(text, self.agent)
                expected = text
                old = self.inputs["agentRelease"]
                # Independent expected edit: all scalar/key replacements restricted to the known block.
                start = text.index('  "agentRelease": {')
                end = text.index('\n  "derived":', start)
                block = text[start:end]
                for field in ("tag", "giteaReleaseId", "sourceCommit", "agentReceipt", "agentImage"):
                    block = block.replace(json.dumps(old[field]), json.dumps(self.agent[field]))
                for asset, digest in old["assets"].items():
                    new_name = self.agent["agentReceipt"] if asset == old["agentReceipt"] else asset
                    block = block.replace(digest, self.agent["assets"][new_name])
                expected = expected[:start] + block + expected[end:]
                expected = expected.replace(
                    f"Relay {old['tag']} release (Gitea release {old['giteaReleaseId']}, source {old['sourceCommit'][:8]})",
                    f"Relay {self.agent['tag']} release (Gitea release {self.agent['giteaReleaseId']}, "
                    f"source {self.agent['sourceCommit'][:8]})")
                self.assertEqual(rewritten.encode(), expected.encode())
                parsed = json.loads(rewritten)
                self.assertEqual(parsed["agentRelease"], self.agent)
                self.assertEqual({k: v for k, v in parsed.items() if k not in ("agentRelease", "_comment")},
                                 {k: v for k, v in self.inputs.items() if k not in ("agentRelease", "_comment")})

    def test_rewrite_is_idempotent(self):
        self.assertEqual(pin.rewrite_inputs(self.text, self.inputs["agentRelease"]), self.text)
        updated = pin.rewrite_inputs(self.text, self.agent)
        self.assertEqual(pin.rewrite_inputs(updated, self.agent), updated)

    def test_rewrite_refuses_ambiguous_or_unrecognized_inputs(self):
        cases = [self.text.replace('  "agentRelease": {', '  "agentRelease": {}, "agentRelease": {'),
                 self.text.replace('"agentRelease": {', '"agentRelease": {"runtimeImage": "unexpected",'),
                 self.text.replace('"SHA256SUMS":', '"other":'),
                 self.text.replace("`agentRelease` pins", "`agentRelease` used to pin")]
        for text in cases:
            with self.subTest(text=text[:30]), self.assertRaises(pin.Refusal):
                pin.rewrite_inputs(text, self.agent)

    def test_release_hashes_all_three_assets(self):
        self.write_assets()
        agent, config = self.read_release()
        self.assertEqual(config, self.receipt["imageConfigDigest"])
        self.assertEqual(agent, {**self.agent, "assets": {
            name: hashlib.sha256((self.assets / name).read_bytes()).hexdigest() for name in self.agent["assets"]}})
        # Real release checksums can list only other release artifacts.
        self.write_assets(sums=f"{'5' * 64}  unrelated-connector\n")
        self.assertEqual(self.read_release()[0]["agentImage"], self.agent["agentImage"])

    def test_malformed_receipts_and_manifests_are_refused(self):
        cases = [
            ("kind", lambda r, m: r.update(kind="other")),
            ("version", lambda r, m: r.update(version=True)),
            ("component", lambda r, m: r.update(component="worker-runtime")),
            ("runtime", lambda r, m: r.update(runtime={"runtimeDigest": "1" * 64})),
            ("platform", lambda r, m: r.update(platform="linux/arm64")),
            ("source", lambda r, m: r.update(sourceCommit="8" * 40)),
            ("invalid source", lambda r, m: (r.update(sourceCommit="bad"), m.update(sourceCommit="bad"))),
            ("tag mismatch", lambda r, m: m.update(version="v0.10.8")),
            ("missing v", lambda r, m: m.update(version="0.10.7")),
            ("publication", lambda r, m: r.update(publication=[])),
            ("tag image", lambda r, m: r["publication"].update(image="registry.chifor.me/relay/infrastructure-agent:v0.10.7")),
            ("tag plus digest", lambda r, m: r["publication"].update(image=self.agent["agentImage"].replace("@", ":v0.10.7@"))),
            ("repository", lambda r, m: r["publication"].update(image=self.agent["agentImage"].replace("infrastructure-agent", "worker-runtime"))),
            ("digest join", lambda r, m: r["publication"].update(manifestSha256="9" * 64)),
            ("missing config", lambda r, m: r.pop("imageConfigDigest")),
            ("config", lambda r, m: r.update(imageConfigDigest="4" * 64)),
        ]
        for name, mutate in cases:
            with self.subTest(name):
                receipt, manifest = copy.deepcopy(self.receipt), copy.deepcopy(self.manifest)
                mutate(receipt, manifest)
                self.write_assets(receipt=receipt, manifest=manifest)
                with self.assertRaises(pin.Refusal):
                    self.read_release()
        for name in ("manifest.json", self.agent["agentReceipt"]):
            for malformed in ("[]", "{", '{"version": 1, "version": 2}'):
                with self.subTest(name=name, malformed=malformed):
                    self.write_assets()
                    (self.assets / name).write_text(malformed, encoding="utf-8")
                    with self.assertRaises((pin.Refusal, ValueError)):
                        self.read_release()

    def test_bad_tags_ids_checksums_and_ambiguous_assets_are_refused(self):
        self.write_assets()
        for tag in ("v0.10.3", "v0.10.2", "v0.10.7-rc.1", "v00.10.7", "0.10.7"):
            with self.subTest(tag=tag), self.assertRaises(pin.Refusal):
                self.read_release(tag=tag)
        for release_id in (0, -1, True, 2**53):
            with self.subTest(release_id=release_id), self.assertRaises(pin.Refusal):
                self.read_release(release_id=release_id)
        for sums in ("", "bad\n", f"{'0' * 64}  manifest.json\n",
                     f"{'0' * 64}  other\n{'0' * 64}  other\n"):
            self.write_assets(sums=sums)
            with self.subTest(sums=sums), self.assertRaises(pin.Refusal):
                self.read_release()
        self.write_assets()
        duplicate = self.assets / "worker-image-infrastructure-agent-888.json"
        duplicate.write_bytes((self.assets / self.agent["agentReceipt"]).read_bytes())
        with self.assertRaisesRegex(pin.Refusal, "exactly one"):
            self.read_release()
        duplicate.unlink()
        (self.assets / self.agent["agentReceipt"]).unlink()
        with self.assertRaisesRegex(pin.Refusal, "exactly one"):
            self.read_release()

    def test_i4_and_import_invariants_refuse_unrelated_changes(self):
        before = pin.snapshot(ROOT / pin.I4, ["."])
        summary = json.loads(before["summary.json"])
        agent = self.inputs["agentRelease"]
        config = summary["agentRelease"]["imageConfigDigest"]
        pin.check_i4(before, before, agent, config)
        for name in before:
            after = dict(before)
            data = json.loads(after[name])
            data["unexpected"] = True
            after[name] = json.dumps(data).encode()
            with self.subTest(name=name), self.assertRaises(pin.Refusal):
                pin.check_i4(before, after, agent, config)
        installation = pin.snapshot(ROOT, [pin.TREE])
        pin.check_import(installation, installation, agent["agentImage"])
        for name in (pin.PINS, f"{pin.TREE}/agent/config.json"):
            with self.subTest(name=name), self.assertRaises(pin.Refusal):
                pin.check_import(installation, {**installation, name: installation[name] + b"\n"}, agent["agentImage"])
        with self.assertRaises(pin.Refusal):
            pin.check_import(installation, {**installation, "unexpected": b""}, agent["agentImage"])


@unittest.skipUnless(RELAY and ASSETS and AGENT_ASSETS,
                     "needs RELAY_RESEARCHER_RELAY, RELAY_RESEARCHER_ASSETS and RELAY_RESEARCHER_AGENT_ASSETS")
class CurrentRelease(unittest.TestCase):
    def test_current_pin_leaves_every_tracked_file_byte_identical(self):
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                               check=True, capture_output=True, text=True).stdout.split("\0")
        with tempfile.TemporaryDirectory(prefix=".agent-pin-e2e-", dir=ROOT) as tmp:
            tree = Path(tmp) / "tree"
            # Include the tool before its first commit too; baseline every tracked file.
            for name in set(filter(None, [*files, SCRIPT])):
                source, target = ROOT / name, tree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
            before = {name: (tree / name).read_bytes() for name in filter(None, files)}
            modes = {name: (tree / name).stat().st_mode for name in before}
            agent = json.loads(before[pin.INPUTS])["agentRelease"]
            result = subprocess.run([
                sys.executable, str(tree / SCRIPT), "--relay", RELAY, "--assets", ASSETS,
                "--agent-assets", AGENT_ASSETS, "--tag", agent["tag"],
                "--gitea-release-id", str(agent["giteaReleaseId"])],
                cwd=tree, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("all files byte-identical", result.stdout)
            self.assertIn("imageConfigDigest:", result.stdout)
            self.assertIn("Exact current paragraph, receipt entry and rows", result.stdout)
            for name, body in before.items():
                self.assertEqual((tree / name).read_bytes(), body, name)
                self.assertEqual((tree / name).stat().st_mode, modes[name], name)
            self.assertFalse(list(tree.glob(".relay-agent-pin-*")))

    def test_new_agent_changes_exactly_the_four_mechanical_files(self):
        with tempfile.TemporaryDirectory(prefix=".agent-pin-update-", dir=ROOT) as tmp:
            work, original = Path(tmp), pin.snapshot(ROOT, pin.STAGED)
            tree, assets = work / "tree", work / "assets"
            assets.mkdir()
            for name, body in original.items():
                target = tree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            inputs = json.loads(original[pin.INPUTS])
            old = inputs["agentRelease"]
            tag = f"v{pin.version(old['tag'])[0] + 1}.0.0"
            manifest = json.loads((Path(AGENT_ASSETS) / "manifest.json").read_bytes())
            receipt = json.loads((Path(AGENT_ASSETS) / old["agentReceipt"]).read_bytes())
            manifest.update(version=tag, sourceCommit="7" * 40)
            receipt.update(sourceCommit="7" * 40)
            image = old["agentImage"].split("@")[0] + "@sha256:" + "7" * 64
            receipt["publication"].update(image=image, manifestSha256="7" * 64)
            receipt_name = "worker-image-infrastructure-agent-999007.json"
            for name, value in (("manifest.json", manifest), (receipt_name, receipt)):
                (assets / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
            (assets / "SHA256SUMS").write_text("".join(
                f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n"
                for name in ("manifest.json", receipt_name)), encoding="utf-8")
            with mock.patch.object(pin, "ROOT", tree), contextlib.redirect_stdout(io.StringIO()):
                pin.pin(Path(RELAY), Path(ASSETS), assets, tag, 999007)
            after = pin.snapshot(tree, pin.STAGED)
            self.assertEqual(set(after), set(original))
            self.assertEqual({name for name in after if after[name] != original[name]},
                             {pin.INPUTS, f"{pin.I4}/definition.json", f"{pin.I4}/summary.json", pin.PINS})
            self.assertEqual(after[pin.PINS], original[pin.PINS].replace(old["agentImage"].encode(), image.encode()))
            self.assertEqual(after[f"{pin.I4}/definition.json"],
                             original[f"{pin.I4}/definition.json"].replace(old["agentImage"].encode(), image.encode()))
            updated = json.loads(after[pin.INPUTS])["agentRelease"]
            self.assertEqual((updated["tag"], updated["giteaReleaseId"], updated["sourceCommit"],
                              updated["agentReceipt"], updated["agentImage"]),
                             (tag, 999007, "7" * 40, receipt_name, image))
            self.assertFalse(list(tree.glob(".relay-agent-pin-*")))

    def test_failed_verification_never_installs_staged_edits(self):
        # A real generator run reaches the invariant check with changed inputs; its refusal
        # must leave the target tree intact, including the comment and provenance.
        with tempfile.TemporaryDirectory(prefix=".agent-pin-refusal-", dir=ROOT) as tmp:
            tree = Path(tmp)
            original = pin.snapshot(ROOT, pin.STAGED)
            for name, body in original.items():
                target = tree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            agent = json.loads(original[pin.INPUTS])["agentRelease"]
            with mock.patch.object(pin, "ROOT", tree), mock.patch.object(
                    pin, "check_i4", side_effect=pin.Refusal("injected invariant mismatch")), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(pin.Refusal, "injected invariant mismatch"):
                    pin.pin(Path(RELAY), Path(ASSETS), Path(AGENT_ASSETS), agent["tag"], agent["giteaReleaseId"] + 1)
            self.assertEqual(pin.snapshot(tree, pin.STAGED), original)
            self.assertFalse(list(tree.glob(".relay-agent-pin-*")))


if __name__ == "__main__":
    unittest.main()
