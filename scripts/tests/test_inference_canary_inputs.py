"""Keep reviewed canary definitions and their pinned installed bytes consistent."""
from pathlib import Path
import runpy
import unittest


class CanaryInputs(unittest.TestCase):
    def test_committed_inputs_match(self):
        script = Path(__file__).resolve().parents[2] / "docs/runbooks/inference-canary-v0113/validate.py"
        self.assertTrue(runpy.run_path(str(script))["validate"]())


if __name__ == "__main__":
    unittest.main()
