"""The estate provisioning Job's MATRIX, evaluated by a real shell.

    python3 -m unittest discover -s scripts/tests -p "test_estate_provision_matrix.py"

A literal backslash-n once slipped into the double-quoted MATRIX (codex review of the edge P3 change,
2026-10-27): the shell keeps it as a token of its own, the Job then looks for a seed named after it,
and seeding stops before the entries that follow. This runs the assignment the way the Job's shell
does and checks every token, instead of reading the YAML.
"""
import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
JOB = ROOT / "kubernetes/apps/infrastructure/security/openbao/estate-provision-job.yaml"
TOKEN = re.compile(r"^[a-z0-9][a-z0-9-]*:[a-z0-9_]+$")


def matrix_assignment() -> str:
    docs = [d for d in yaml.safe_load_all(JOB.read_text(encoding="utf-8")) if d]
    script = [d for d in docs if d.get("kind") == "ConfigMap"][0]["data"]["provision.sh"]
    m = re.search(r'^\s*(MATRIX="[^"]*")', script, re.M)
    if not m:
        raise AssertionError("no MATRIX=\"...\" assignment in provision.sh")
    return m.group(1)


class EstateProvisionMatrix(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.fail("bash is required to evaluate the MATRIX the way the Job's shell does")

    def tokens(self, assignment):
        out = subprocess.run([self.bash, "-c", assignment + '\nfor p in $MATRIX; do printf "%s\\n" "$p"; done'],
                             capture_output=True, text=True, check=True).stdout
        return [t for t in out.replace("\r", "").split("\n") if t]

    def test_every_token_is_one_seed_field(self):
        toks = self.tokens(matrix_assignment())
        self.assertGreater(len(toks), 10)
        self.assertEqual([t for t in toks if not TOKEN.match(t)], [])
        self.assertEqual(len(toks), len(set(toks)), "a seed field is listed twice")
        self.assertIn("unifi:api_token", toks)

    def test_a_literal_backslash_n_would_be_caught(self):
        toks = self.tokens('MATRIX="a:b c:d \\n      e:f"')
        self.assertIn("\\n", toks)
        self.assertFalse(all(TOKEN.match(t) for t in toks))


if __name__ == "__main__":
    unittest.main()
