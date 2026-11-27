"""Exercise Claude acceptance automation with local synthetic evidence only."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/inference-canary-v0113"


class PiOwnerPhaseB(unittest.TestCase):
    def run_node_tests(self, names, timeout):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip Claude automation tests")
        result = subprocess.run(
            [node, "--test", *(str(RUNBOOK / name) for name in names)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_claude_driver_and_host_contracts(self):
        self.run_node_tests(
            ["owner-phase-b.test.cjs", "owner-phase-b-automation.test.cjs"], 60,
        )

    def test_offline_complete_claude_flow(self):
        # The real five-second observation barriers and twenty-second lease
        # heartbeat remain active. The fixture contains no real account access.
        self.run_node_tests(["owner-phase-b-automation-integration.test.cjs"], 330)


if __name__ == "__main__":
    unittest.main()
