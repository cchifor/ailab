"""Run the owner automation's offline Node contract tests; never skip missing Node."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a.test.cjs"
AUTOMATION_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-automation.test.cjs"
SOCKET_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-socket.test.cjs"
RENEWAL_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-renewal.test.cjs"
INTEGRATION_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-automation-integration.test.cjs"


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
            [node, "--test", str(AUTOMATION_FIXTURE), str(SOCKET_FIXTURE), str(RENEWAL_FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_complete_noninteractive_flow(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip the complete automation flow")
        result = subprocess.run(
            [node, "--test", str(INTEGRATION_FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=390,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
