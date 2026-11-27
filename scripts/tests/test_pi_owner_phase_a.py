"""Run the owner automation's offline Node contract tests; never skip missing Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a.test.cjs"
AUTOMATION_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-automation.test.cjs"


class PiOwnerPhaseA(unittest.TestCase):
    def test_offline_response_and_safety_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip owner acceptance safety tests")
        result = subprocess.run(
            [node, "--test", str(FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_noninteractive_host_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip automation host tests")
        result = subprocess.run(
            [node, "--test", str(AUTOMATION_FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
