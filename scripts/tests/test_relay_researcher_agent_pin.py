#!/usr/bin/env python3
"""Text-preserving agent pin updates and offline refusal checks; optional real-release reproduction.

Set RELAY_RESEARCHER_RELAY, RELAY_RESEARCHER_ASSETS and RELAY_RESEARCHER_AGENT_ASSETS
to run the complete tool on a copy with the currently pinned release (Node 22.21+ and helm on PATH).
"""
from __future__ import annotations

import contextlib
import copy
import errno
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
        # One patch above the committed pin, so the fixture is never a downgrade after a real pin.
        major, minor, patch = map(int, self.agent["tag"][1:].split("."))
        self.agent.update(tag=f"v{major}.{minor}.{patch + 1}", giteaReleaseId=999007, sourceCommit="7" * 40,
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
        # The committed config and the bundle tool's leadership overlay it carries.
        self.config = (ROOT / pin.CONFIG).read_bytes()
        self.leadership = pin.leadership_overlay(ROOT)

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
        agent, config, checked = self.read_release()
        self.assertEqual(config, self.receipt["imageConfigDigest"])
        self.assertEqual(checked, ["manifest.json", self.agent["agentReceipt"]])
        self.assertEqual(agent, {**self.agent, "assets": {
            name: hashlib.sha256((self.assets / name).read_bytes()).hexdigest() for name in self.agent["assets"]}})
        # Real release checksums can list only other release artifacts.
        self.write_assets(sums=f"{'5' * 64}  unrelated-connector\n")
        self.assertEqual(self.read_release()[0]["agentImage"], self.agent["agentImage"])
        self.assertEqual(self.read_release()[2], [])

    def cli_args(self):
        return ["--relay", str(self.assets), "--assets", str(self.assets),
                "--agent-assets", str(self.assets), "--tag", self.agent["tag"],
                "--gitea-release-id", str(self.agent["giteaReleaseId"])]

    def test_step_two_reports_checksum_coverage_including_none(self):
        for sums, expected in ((None, f"manifest.json, {self.agent['agentReceipt']}"),
                               (f"{'5' * 64}  unrelated-connector\n", "none")):
            self.write_assets(sums=sums)
            output = io.StringIO()
            with self.subTest(coverage=expected), \
                    mock.patch.object(pin, "run", return_value="v22.23.5"), \
                    mock.patch.object(Path, "is_file", return_value=True), \
                    mock.patch.object(pin, "snapshot", return_value={pin.INPUTS: self.text.encode()}), \
                    mock.patch.object(pin, "rewrite_inputs", side_effect=pin.Refusal("stop after step 2")), \
                    contextlib.redirect_stdout(output), self.assertRaisesRegex(pin.Refusal, "stop after step 2"):
                pin.pin(self.assets, self.assets, self.assets, self.agent["tag"], self.agent["giteaReleaseId"])
            self.assertIn("2/6 Hash the three agent assets", output.getvalue())
            self.assertIn(f"Cross-checked against SHA256SUMS: {expected}\n", output.getvalue())

    def test_downgrade_requires_cli_override_and_warns(self):
        # A release newer than the runtime but older than the current agent pin, derived from the
        # committed inputs so the test survives every agent-only update.
        pinned = self.inputs["agentRelease"]["tag"]
        major, minor, patch = pin.version(pinned)
        older = f"v{major}.{minor}.{patch - 1}"
        newer = f"v{major}.{minor}.{patch + 1}"
        self.assertGreater(pin.version(older), pin.version(self.inputs["release"]["tag"]))
        self.agent["tag"] = self.manifest["version"] = older
        self.write_assets()
        for allow in (False, True):
            stderr = io.StringIO()
            with self.subTest(allow=allow), \
                    mock.patch.object(pin, "run", return_value="v22.23.5"), \
                    mock.patch.object(Path, "is_file", return_value=True), \
                    mock.patch.object(pin, "snapshot", return_value={pin.INPUTS: self.text.encode()}), \
                    mock.patch.object(pin, "rewrite_inputs", side_effect=pin.Refusal("reached rewrite")) as rewrite, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                self.assertEqual(pin.main(self.cli_args() + (["--allow-downgrade"] if allow else [])), 1)
            if allow:
                rewrite.assert_called_once()
                self.assertIn(f"WARNING: --allow-downgrade permits agent downgrade from {pinned} to {older}",
                              stderr.getvalue())
                self.assertIn("reached rewrite", stderr.getvalue())
            else:
                rewrite.assert_not_called()
                self.assertIn(f"older than pinned agentRelease.tag {pinned}", stderr.getvalue())
        for tag in (pinned, newer):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                pin.check_agent_tag(tag, self.inputs["agentRelease"], allow_downgrade=False)
            self.assertEqual(stderr.getvalue(), "")
        self.agent["tag"] = self.manifest["version"] = self.inputs["release"]["tag"]
        self.write_assets()
        with mock.patch.object(pin, "run", return_value="v22.23.5"), \
                mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(pin, "snapshot", return_value={pin.INPUTS: self.text.encode()}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(pin.main(self.cli_args() + ["--allow-downgrade"]), 1)
        self.assertIn("agent tag must be newer than the runtime release", stderr.getvalue())

    def test_node_minimum_matches_generator(self):
        for node in ("v22.21.0", "v22.23.5", "v23.0.0", "v24.0.0"):
            with self.subTest(node=node):
                pin.check_node(node)
        for node in ("v20.30.0", "v22.20.9", "v24.0.0-rc.1", "bad"):
            with self.subTest(node=node), self.assertRaisesRegex(pin.Refusal, "Node 22.21"):
                pin.check_node(node)

    def test_missing_or_malformed_inputs_have_context(self):
        for key in ("release", "installation", "agentRelease"):
            for value in ("missing", None, []):
                inputs = copy.deepcopy(self.inputs)
                if value == "missing":
                    del inputs[key]
                else:
                    inputs[key] = value
                with self.subTest(key=key, value=value), \
                        mock.patch.object(pin, "run", return_value="v22.23.5"), \
                        mock.patch.object(Path, "is_file", return_value=True), \
                        mock.patch.object(pin, "snapshot", return_value={pin.INPUTS: json.dumps(inputs).encode()}), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(pin.main(self.cli_args()), 1)
                self.assertRegex(stderr.getvalue(), rf"inputs(?: is missing |\.){key}")
        for value in ("missing", None, []):
            inputs = copy.deepcopy(self.inputs)
            if value == "missing":
                del inputs["_comment"]
            else:
                inputs["_comment"] = value
            with self.subTest(comment=value), self.assertRaisesRegex(pin.Refusal, r"inputs.*_comment"):
                pin.rewrite_inputs(json.dumps(inputs), self.agent)
        before = pin.snapshot(ROOT / pin.I4, ["."])
        for value in ("missing", None, []):
            summary = json.loads(before["summary.json"])
            if value == "missing":
                del summary["files"]
            else:
                summary["files"] = value
            malformed = {**before, "summary.json": json.dumps(summary).encode()}
            with self.subTest(files=value), self.assertRaisesRegex(pin.Refusal, r"I4 summary.json.*files"):
                pin.check_i4(malformed, before, self.inputs["agentRelease"],
                             summary["agentRelease"]["imageConfigDigest"])

    def test_programming_errors_are_not_reported_as_input_refusals(self):
        for error in (KeyError("bug"), TypeError("bug")):
            with self.subTest(error=error), mock.patch.object(pin, "pin", side_effect=error), \
                    self.assertRaises(type(error)):
                pin.main(self.cli_args())

    def test_import_accepts_scalar_quoting_and_existing_indentation(self):
        image = self.agent["agentImage"]
        for indent in ("  ", "    "):
            before = (f"# retained comment\napiVersion: v1\ndata:\n{indent}agentImage: 'old'\n"
                      f"{indent}sourceCommit: unchanged\n").encode()
            for scalar in (json.dumps(image), f"'{image}'", image):
                with self.subTest(indent=indent, scalar=scalar):
                    pin.check_import({pin.PINS: before, pin.CONFIG: self.config},
                                     {pin.PINS: before.replace(b"'old'", scalar.encode()), pin.CONFIG: self.config},
                                     image, self.leadership)
            for changed in (before.replace(b"unchanged", b"changed"),
                            before.replace(b"retained comment", b"edited comment"),
                            before + f"{indent}agentImage: 'old'\n".encode(), b"data: [bad]\n"):
                with self.subTest(changed=changed), self.assertRaises(pin.Refusal):
                    pin.check_import({pin.PINS: before, pin.CONFIG: self.config},
                                     {pin.PINS: changed, pin.CONFIG: self.config}, "old", self.leadership)

    def test_import_requires_pyyaml_with_context(self):
        with mock.patch.dict(sys.modules, {"yaml": None}), self.assertRaisesRegex(pin.Refusal, "PyYAML is required"):
            pin.check_import({}, {}, self.agent["agentImage"], self.leadership)

    def test_malformed_receipts_and_manifests_are_refused(self):
        cases = [
            ("kind", lambda r, m: r.update(kind="other")),
            ("version", lambda r, m: r.update(version=True)),
            ("component", lambda r, m: r.update(component="worker-runtime")),
            ("runtime", lambda r, m: r.update(runtime={"runtimeDigest": "1" * 64})),
            ("platform", lambda r, m: r.update(platform="linux/arm64")),
            ("source", lambda r, m: r.update(sourceCommit="8" * 40)),
            ("invalid source", lambda r, m: (r.update(sourceCommit="bad"), m.update(sourceCommit="bad"))),
            ("tag mismatch", lambda r, m: m.update(version=self.agent["tag"] + "0")),
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
        pin.check_import(installation, installation, agent["agentImage"], self.leadership)
        for name in (pin.PINS, f"{pin.TREE}/agent/config.json"):
            with self.subTest(name=name), self.assertRaises(pin.Refusal):
                pin.check_import(installation, {**installation, name: installation[name] + b"\n"}, agent["agentImage"],
                                 self.leadership)
        with self.assertRaises(pin.Refusal):
            pin.check_import(installation, {**installation, "unexpected": b""}, agent["agentImage"], self.leadership)

    def test_import_guard_requires_the_leadership_overlay_and_nothing_else(self):
        # The config import leaves byte-identical must carry exactly the bundle tool's overlay; any
        # config change is refused, a change to the overlay's own timing included (that is the bundle
        # tool's import, landed before an agent-only update), and so is any other timing.
        bundle = importlib.util.spec_from_file_location("bundle_for_pin", ROOT / pin.BUNDLE_TOOL)
        module = importlib.util.module_from_spec(bundle)
        bundle.loader.exec_module(module)
        self.assertEqual(self.leadership, module.LEADERSHIP_OVERLAY)
        installation = pin.snapshot(ROOT, [pin.TREE])
        image = self.inputs["agentRelease"]["agentImage"]  # the committed pin: pins.yaml is unchanged here
        config = json.loads(installation[pin.CONFIG])
        self.assertEqual(config["leadership"], self.leadership)
        pin.check_import(installation, installation, image, self.leadership)

        def body(**changes):
            return (json.dumps({**config, **changes}, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # A valid timing that is not the overlay: the preparation's, unless the overlay is set back to it
        # (the installation runbook's timing-only rollback), then T1's 30/20/2.
        other = (module.PREPARED_LEADERSHIP if self.leadership != module.PREPARED_LEADERSHIP
                 else {"leaseDurationSeconds": 30, "renewDeadlineSeconds": 20, "retryPeriodSeconds": 2})
        committed, prepared = installation[pin.CONFIG], body(leadership=other)
        floats = body(leadership={k: float(v) for k, v in self.leadership.items()})
        cases = {  # config.json before and after the import
            "the overlay newly imported": (prepared, committed, "import changed"),
            "another timing imported": (committed, prepared, "import changed"),
            "another config field": (committed, body(version=2), "import changed"),
            "the preparation's timing kept": (prepared, prepared, "not the bundle tool's"),
            "the overlay as floats": (floats, floats, "not the bundle tool's"),
        }
        for name, (before, after, reason) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(pin.Refusal, reason):
                pin.check_import({**installation, pin.CONFIG: before}, {**installation, pin.CONFIG: after}, image,
                                 self.leadership)
        with self.subTest("a bundle tool without the overlay"), tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / pin.BUNDLE_TOOL).write_text("ROOT = None\n", encoding="utf-8")
            with self.assertRaisesRegex(pin.Refusal, "declares no LEADERSHIP_OVERLAY"):
                pin.leadership_overlay(Path(tmp))


class AtomicInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".agent-pin-install-", dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = {"one/inputs.json": b"original inputs", "two/definition.json": b"original definition",
                         "two/summary.json": b"original summary", "three/pins.yaml": b"original pins"}
        self.replacements = {name: b"replacement " + body for name, body in self.original.items()}
        for index, (name, body) in enumerate(self.original.items()):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(body)
            path.chmod(0o640 if index % 2 else 0o600)
        self.modes = {name: (self.root / name).stat().st_mode for name in self.original}

    def assert_files(self, expected):
        for name, body in expected.items():
            self.assertEqual((self.root / name).read_bytes(), body, name)
            self.assertEqual((self.root / name).stat().st_mode, self.modes[name], name)

    def leftovers(self):
        return sorted(self.root.rglob(".relay-agent-pin-*"))

    def test_success_preserves_modes_and_cleans_backups(self):
        pin.install(self.root, self.original, self.replacements)
        self.assert_files(self.replacements)
        self.assertEqual(self.leftovers(), [])

    def test_partial_staging_write_failure_never_touches_destinations(self):
        create = pin.tempfile.NamedTemporaryFile
        replacement_count = 0

        def fail_second_write(**kwargs):
            nonlocal replacement_count
            stream = create(**kwargs)
            if "replacement" in kwargs["prefix"]:
                replacement_count += 1
                if replacement_count == 2:
                    write = stream.write

                    def partial_write(body):
                        write(body[:3])
                        stream.flush()
                        raise OSError(errno.ENOSPC, "injected partial staging write")

                    stream.write = partial_write
            return stream

        with mock.patch.object(pin.tempfile, "NamedTemporaryFile", side_effect=fail_second_write), \
                mock.patch.object(pin.os, "replace") as replace, \
                self.assertRaisesRegex(pin.Refusal, "injected partial staging write"):
            pin.install(self.root, self.original, self.replacements)
        replace.assert_not_called()
        self.assert_files(self.original)
        self.assertEqual(self.leftovers(), [])

    def test_fsync_failure_never_touches_destinations(self):
        with mock.patch.object(pin.os, "fsync", side_effect=OSError(errno.ENOSPC, "injected fsync failure")), \
                mock.patch.object(pin.os, "replace") as replace, \
                self.assertRaisesRegex(pin.Refusal, "injected fsync failure"):
            pin.install(self.root, self.original, self.replacements)
        replace.assert_not_called()
        self.assert_files(self.original)
        self.assertEqual(self.leftovers(), [])

    def test_replace_failure_restores_all_previously_replaced_files(self):
        replace = pin.os.replace
        calls = []

        def fail_last(source, destination):
            calls.append(destination.relative_to(self.root).as_posix())
            if destination == self.root / "three/pins.yaml":
                raise PermissionError("injected destination permission failure")
            replace(source, destination)

        with mock.patch.object(pin.os, "replace", side_effect=fail_last), \
                self.assertRaisesRegex(pin.Refusal, "all replaced destinations restored"):
            pin.install(self.root, self.original, self.replacements)
        self.assertEqual(calls, [*self.original, "two/summary.json", "two/definition.json", "one/inputs.json"])
        self.assert_files(self.original)
        self.assertEqual(self.leftovers(), [])

    def test_restore_failure_retains_backup_and_continues_other_restorations(self):
        replace = pin.os.replace
        restored = []

        def fail_install_and_restore(source, destination):
            name = destination.relative_to(self.root).as_posix()
            if "backup" in source.name:
                restored.append(name)
                if name == "two/definition.json":
                    raise PermissionError("injected restore failure")
            elif name == "three/pins.yaml":
                raise OSError(errno.ENOSPC, "injected install failure")
            replace(source, destination)

        with mock.patch.object(pin.os, "replace", side_effect=fail_install_and_restore), \
                self.assertRaises(pin.Refusal) as caught:
            pin.install(self.root, self.original, self.replacements)
        self.assertEqual(restored, ["two/summary.json", "two/definition.json", "one/inputs.json"])
        self.assert_files({**self.original, "two/definition.json": self.replacements["two/definition.json"]})
        backup, = self.leftovers()
        self.assertEqual(backup.parent, self.root / "two")
        self.assertEqual(backup.read_bytes(), self.original["two/definition.json"])
        self.assertEqual(backup.stat().st_mode, self.modes["two/definition.json"])
        self.assertIn("injected install failure", str(caught.exception))
        self.assertIn("rollback could not restore two/definition.json: injected restore failure", str(caught.exception))
        self.assertIn(f"backup retained at {backup}", str(caught.exception))
        # The named backup remains usable after the failed transaction returns.
        os.replace(backup, self.root / "two/definition.json")
        self.assert_files(self.original)


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
        self.check_agent_update()

    def test_explicit_downgrade_changes_exactly_the_four_mechanical_files(self):
        self.check_agent_update(tag="v0.10.5", allow_downgrade=True)

    def check_agent_update(self, tag=None, allow_downgrade=False):
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
            tag = tag or f"v{pin.version(old['tag'])[0] + 1}.0.0"
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
            with mock.patch.object(pin, "ROOT", tree), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                pin.pin(Path(RELAY), Path(ASSETS), assets, tag, 999007, allow_downgrade=allow_downgrade)
            if allow_downgrade:
                self.assertIn("WARNING: --allow-downgrade permits agent downgrade", stderr.getvalue())
            else:
                self.assertEqual(stderr.getvalue(), "")
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

    def test_unimported_leadership_overlay_is_refused_and_installs_nothing(self):
        # A config without the bundle tool's overlay (as before the overlay landed) is not an agent-only
        # update: the bundle check refuses it, and the tree is left as it was.
        with tempfile.TemporaryDirectory(prefix=".agent-pin-timing-", dir=ROOT) as tmp:
            tree = Path(tmp)
            original = pin.snapshot(ROOT, pin.STAGED)
            for name, body in original.items():
                target = tree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            config = json.loads(original[pin.CONFIG])
            config["leadership"] = {"leaseDurationSeconds": 15, "renewDeadlineSeconds": 10, "retryPeriodSeconds": 2}
            (tree / pin.CONFIG).write_text(json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n",
                                           encoding="utf-8")
            staged = pin.snapshot(tree, pin.STAGED)
            agent = json.loads(original[pin.INPUTS])["agentRelease"]
            with mock.patch.object(pin, "ROOT", tree), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(pin.Refusal, "not LEADERSHIP_OVERLAY"):
                    pin.pin(Path(RELAY), Path(ASSETS), Path(AGENT_ASSETS), agent["tag"], agent["giteaReleaseId"])
            self.assertEqual(pin.snapshot(tree, pin.STAGED), staged)
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
