"""Check the fixed active-drain core and its disabled-until-qualified host."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/inference-canary-v0113"


class PiOwnerActiveDrain(unittest.TestCase):
    def test_offline_active_drain_core_and_host_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip active-drain tests")
        result = subprocess.run(
            [node, "--test", str(RUNBOOK / "owner-active-drain.test.cjs"),
             str(RUNBOOK / "owner-active-drain-automation.test.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
