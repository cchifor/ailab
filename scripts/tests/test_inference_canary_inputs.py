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

    def test_missing_or_malformed_pi_node_version_is_refused(self):
        root = Path(__file__).resolve().parents[2] / "docs/runbooks/inference-canary-v0113"
        validate = runpy.run_path(str(root / "validate.py"))["validate"]
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary)
            for path in root.glob("*.json"):
                shutil.copyfile(path, copy / path.name)
            path = copy / "holder-inputs.json"
            inputs = json.loads(path.read_text())
            pi = next(a for a in inputs["relay_holder_archives"] if a["component"] == "pi-linux-x64")
            for pin in [None, "", "22.23", "v22.23.2", 22]:
                with self.subTest(pin=pin):
                    if pin is None:
                        pi.pop("nodeVersion", None)
                    else:
                        pi["nodeVersion"] = pin
                    path.write_text(json.dumps(inputs))
                    with self.assertRaisesRegex(ValueError, "explicit Node version pin"):
                        validate(copy)


if __name__ == "__main__":
    unittest.main()
