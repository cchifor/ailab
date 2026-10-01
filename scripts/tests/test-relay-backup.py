#!/usr/bin/env python3
"""Exercise the production publisher with pg_restore COPY format and adversarial artifacts.

Run: RELAY_NODE_BIN=/path/to/node python3 scripts/tests/test-relay-backup.py
Requires PyYAML and Node 22. The separate live drill tests pg_dump/restore and DB roles.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

REPO = Path(__file__).resolve().parents[2]
DOCS = list(yaml.safe_load_all((REPO / "kubernetes/apps/apps/relay/backup.yaml").read_text()))
SCRIPT = next(d for d in DOCS if d["kind"] == "ConfigMap")["data"]["publish.mjs"]
NODE = os.environ.get("RELAY_NODE_BIN", "node")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def copy_field(value):
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-backup-test-")
        self.root = Path(self.temp.name)
        self.dumps = self.root / "dumps"
        self.artifacts = self.root / "artifacts"
        self.dumps.mkdir()
        self.artifacts.mkdir()
        self.script = self.root / "publish.mjs"
        self.script.write_text(SCRIPT)
        self.manifest = {"id": "example", "version": "2.0.0", "description": "tab\tline\npath\\quoted\""}
        self.previous = {"id": "example", "version": "1.0.0", "description": "old"}
        self.sha = self.artifact(self.manifest, "export default () => {};\n")
        self.old_sha = self.artifact(self.previous, "export default () => 1;\n")

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self, manifest, source):
        sha = hashlib.sha256((canonical(manifest) + "\n" + source).encode()).hexdigest()
        path = self.artifacts / sha
        path.mkdir()
        (path / "index.mjs").write_text(source)
        return sha

    def prepare(self, uid, empty=False):
        stage = self.dumps / (".staging-" + uid)
        stage.mkdir()
        (stage / "relay.dump").write_bytes(b"archive fixture")
        (stage / "relay.toc").write_text("toc fixture\n")
        row = "\t".join([
            self.sha, copy_field(json.dumps(self.manifest)),
            copy_field(json.dumps({"sha256": self.old_sha, "manifest": self.previous})),
        ])
        sql = "COPY public.plugin_states (sha256, manifest, previous) FROM stdin;\n"
        sql += "" if empty else row + "\n"
        sql += "\\.\n"
        (stage / "plugin-states.sql").write_text(sql)
        return stage

    def publish(self, uid):
        return subprocess.run(
            [NODE, str(self.script)],
            env={**os.environ, "DUMP_ROOT": str(self.dumps), "ARTIFACT_ROOT": str(self.artifacts), "POD_UID": uid},
            capture_output=True, text=True,
        )

    def completed(self):
        return [p for p in self.dumps.iterdir() if (p / "COMPLETE").is_file()]

    def test_snapshot_current_and_previous_files_survive_source_removal(self):
        self.prepare("good")
        result = self.publish("good")
        self.assertEqual(result.returncode, 0, result.stderr)
        generation, = self.completed()
        metadata = json.loads((generation / "recovery.json").read_text())
        self.assertEqual(set(metadata["artifactHashes"]), {self.sha, self.old_sha})
        self.assertEqual(metadata["pluginRows"], 1)
        shutil.rmtree(self.artifacts)
        for line in (generation / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(hashlib.sha256((generation / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(json.loads((generation / "plugins" / self.sha / "manifest.json").read_text()), self.manifest)

    def test_corrupt_or_missing_previous_artifact_never_publishes_or_prunes(self):
        self.prepare("good")
        self.assertEqual(self.publish("good").returncode, 0)
        (self.artifacts / self.old_sha / "index.mjs").write_text("corrupted")
        stage = self.prepare("corrupt")
        self.assertNotEqual(self.publish("corrupt").returncode, 0)
        self.assertFalse(stage.exists())
        (self.artifacts / self.old_sha / "index.mjs").unlink()
        stage = self.prepare("missing")
        self.assertNotEqual(self.publish("missing").returncode, 0)
        self.assertFalse(stage.exists())
        self.assertEqual(len(self.completed()), 1)

    def test_empty_registry_and_retention(self):
        for i in range(8):
            self.prepare("retention-" + str(i), empty=True)
            result = self.publish("retention-" + str(i))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.completed()), 7)
        for generation in self.completed():
            self.assertEqual(json.loads((generation / "recovery.json").read_text())["artifactHashes"], [])


if __name__ == "__main__":
    unittest.main()
