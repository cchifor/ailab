"""The live evidence reader must distinguish no effect, one effect and replay."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[2] / "docs/runbooks/inference-canary-v0113/observe.py"
SPEC = importlib.util.spec_from_file_location("canary_observer", SOURCE)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


@unittest.skipUnless(os.name == "posix", "Uses the production Linux file-permission contract")
class MarkerObserver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.run = str(uuid.uuid4())
        self.marker = ("relay-pi-qwen-" + self.run + "\n").encode()
        self.effect = self.root / ("acceptance-" + self.run + ".effects")
        self.denied = self.root / ("acceptance-" + self.run + ".denied")

    def read(self, phase):
        return observer.observe(self.root, "qwen", self.run, phase, os.getuid())

    def write(self, path, body):
        path.write_bytes(body)
        path.chmod(0o600)

    def test_absence_and_one_append_are_distinct_phases(self):
        self.assertTrue(self.read("baseline")["phaseChecksPass"])
        self.assertTrue(self.read("denied")["phaseChecksPass"])
        self.assertFalse(self.read("approved")["phaseChecksPass"])
        self.write(self.effect, self.marker)
        self.assertTrue(self.read("approved")["phaseChecksPass"])
        self.assertTrue(self.read("recovered")["phaseChecksPass"])
        self.assertFalse(self.read("baseline")["phaseChecksPass"])

    def test_replayed_append_fails_after_recovery(self):
        self.write(self.effect, self.marker * 2)
        result = self.read("recovered")
        self.assertFalse(result["phaseChecksPass"])
        self.assertEqual(result["files"]["effects"]["markerOccurrences"], 2)

    def test_denied_effect_is_always_failure(self):
        self.write(self.denied, self.marker)
        self.write(self.effect, self.marker)
        for phase in observer.PHASES:
            self.assertFalse(self.read(phase)["phaseChecksPass"])

    def test_unexpected_content_never_passes_or_leaks(self):
        self.write(self.effect, b"private-unexpected-content\n")
        result = self.read("approved")
        self.assertFalse(result["phaseChecksPass"])
        self.assertNotIn("private-unexpected-content", str(result))

    def test_symlink_hardlink_and_nonprivate_files_are_refused(self):
        target = self.root / "unrelated"
        self.write(target, self.marker)
        self.effect.symlink_to(target)
        self.assertEqual(self.read("approved")["files"]["effects"]["state"], "unsafe-or-unreadable")
        self.effect.unlink()
        os.link(target, self.effect)
        self.assertEqual(self.read("approved")["files"]["effects"]["state"], "unsafe-or-unreadable")
        self.effect.unlink()
        self.write(self.effect, self.marker)
        self.effect.chmod(0o644)
        self.assertFalse(self.read("approved")["phaseChecksPass"])

    def test_fifo_does_not_block_and_oversize_does_not_pass(self):
        os.mkfifo(self.effect, 0o600)
        self.assertFalse(self.read("approved")["phaseChecksPass"])
        self.effect.unlink()
        self.write(self.effect, b"x" * 4097)
        self.assertEqual(self.read("approved")["files"]["effects"]["state"], "oversized")

    def test_workspace_must_be_private(self):
        self.root.chmod(0o755)
        self.assertFalse(self.read("baseline")["phaseChecksPass"])


if __name__ == "__main__":
    unittest.main()
