"""Keep reviewed canary definitions and their pinned installed bytes consistent."""
from pathlib import Path
import json
import runpy
import shutil
import tempfile
import unittest


class CanaryInputs(unittest.TestCase):
    def test_committed_inputs_match(self):
        script = Path(__file__).resolve().parents[2] / "docs/runbooks/inference-canary-v0113/validate.py"
        self.assertTrue(runpy.run_path(str(script))["validate"]())

    def test_archive_only_repin_is_refused(self):
        root = Path(__file__).resolve().parents[2] / "docs/runbooks/inference-canary-v0113"
        validate = runpy.run_path(str(root / "validate.py"))["validate"]
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary)
            for path in root.glob("*.json"):
                shutil.copyfile(path, copy / path.name)
            path = copy / "holder-inputs.json"
            inputs = json.loads(path.read_text())
            for archive in inputs["relay_holder_archives"]:
                if archive["component"] == "pi-linux-x64":
                    archive["manifestSha256"] = "b" * 64
            path.write_text(json.dumps(inputs))
            with self.assertRaisesRegex(ValueError, "Pi archive and profile runtime pins differ"):
                validate(copy)


if __name__ == "__main__":
    unittest.main()
