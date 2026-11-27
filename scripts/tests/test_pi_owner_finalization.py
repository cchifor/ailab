"""Exercise fixed canary cleanup using local synthetic evidence only."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/inference-canary-v0113"


class PiOwnerFinalization(unittest.TestCase):
    def test_offline_finalization_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip finalization tests")
        result = subprocess.run(
            [node, "--test", str(RUNBOOK / "owner-finalization.test.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
