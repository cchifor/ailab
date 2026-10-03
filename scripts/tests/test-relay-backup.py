#!/usr/bin/env python3
"""Exercise the production publisher with pg_restore COPY format and adversarial artifacts.

Run: RELAY_NODE_BIN=/path/to/node python3 scripts/tests/test-relay-backup.py
Requires PyYAML and Node 22. test-relay-rollout-backup.py also exercises real
pg_dump/restore and DB roles under the deployment's pinned container images.
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
ROLLOUT_SCRIPT = next(d for d in DOCS if d["kind"] == "ConfigMap")["data"]["rollout-publish.mjs"]
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


class RolloutPublisherTest(PublisherTest):
    """The same publisher invariants plus init-container failure/retry semantics."""

    def setUp(self):
        super().setUp()
        self.gate = self.root / "rollout-publish.mjs"
        self.gate.write_text(ROLLOUT_SCRIPT)
        self.receipt = self.root / "receipt.json"

    def publish(self, uid):
        return subprocess.run(
            [NODE, str(self.gate)],
            env={**os.environ, "DUMP_ROOT": str(self.dumps), "ARTIFACT_ROOT": str(self.artifacts),
                 "POD_UID": uid, "BACKUP_PUBLISHER": str(self.script), "BACKUP_RECEIPT": str(self.receipt)},
            capture_output=True, text=True,
        )

    def test_failed_artifact_publication_preserves_snapshot_for_same_pod_retry(self):
        self.prepare("retry")
        artifact = self.artifacts / self.old_sha / "index.mjs"
        original = artifact.read_bytes()
        artifact.unlink()
        failed = self.publish("retry")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.receipt.exists())
        saved = self.dumps / ".rollout-source-retry"
        self.assertEqual((saved / "relay.dump").read_bytes(), b"archive fixture")
        self.assertTrue((saved / "plugin-states.sql").is_file())
        artifact.write_bytes(original)
        recovered = self.publish("retry")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(saved.exists())
        self.assertEqual(len(self.completed()), 1)
        proof = json.loads(self.receipt.read_text())
        self.assertEqual(proof["podUid"], "retry")
        self.assertEqual(proof["dumpSha256"], hashlib.sha256(b"archive fixture").hexdigest())

    def test_retry_after_publication_reuses_verified_generation_without_pruning_again(self):
        self.prepare("idempotent")
        self.assertEqual(self.publish("idempotent").returncode, 0)
        proof = self.receipt.read_text()
        shutil.rmtree(self.artifacts)
        self.receipt.unlink()
        result = self.publish("idempotent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipt.read_text(), proof)
        self.assertEqual(len(self.completed()), 1)

    def test_corrupt_completed_generation_blocks_retry_and_clears_old_receipt(self):
        self.prepare("tamper")
        self.assertEqual(self.publish("tamper").returncode, 0)
        generation, = self.completed()
        (generation / "relay.dump").write_bytes(b"corrupted archive")
        failed = self.publish("tamper")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.receipt.exists())
        self.assertTrue(generation.exists())

    def test_receipt_from_another_pod_cannot_satisfy_the_backup_gate(self):
        self.prepare("other-pod")
        self.assertEqual(self.publish("other-pod").returncode, 0)
        # A suffix match would accidentally accept other-pod when current UID is pod.
        failed = self.publish("pod")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.receipt.exists())


class RolloutWiringTest(unittest.TestCase):
    def test_backup_gates_migration_without_exposing_credentials_to_runtime(self):
        docs = list(yaml.safe_load_all((REPO / "kubernetes/apps/apps/relay/relay.yaml").read_text()))
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["backup-database", "backup-artifacts", "migrate"])
        database, artifacts, migrate = pod["initContainers"]
        self.assertEqual(database["envFrom"], [{"secretRef": {"name": "relay-backup"}}])
        self.assertNotIn("envFrom", artifacts)
        self.assertTrue(next(v for v in artifacts["volumeMounts"] if v["name"] == "data")["readOnly"])
        self.assertIn("test -s /tmp/relay-pre-migration-backup.json", migrate["command"][-1])
        runtime = next(c for c in pod["containers"] if c["name"] == "relay")
        self.assertEqual(runtime["envFrom"], [{"secretRef": {"name": "relay-secrets"}}])
        self.assertNotIn("dumps", [v["name"] for v in runtime["volumeMounts"]])
        self.assertEqual(runtime["image"], migrate["image"])


if __name__ == "__main__":
    unittest.main()
