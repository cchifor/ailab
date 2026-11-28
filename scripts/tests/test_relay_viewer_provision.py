"""scripts/relay-viewer-provision.py: the seed edit touches one scope's line and nothing else."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("relay_viewer", ROOT / "scripts" / "relay-viewer-provision.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)

DOC = "\n".join([
    "# header comment",
    "stringData:",
    "    common.json: '{\"a\": \"x\"}'",
    "    dev-worker-4.json: '{\"gitea_pat\": \"it''s\"}'",
    "    dev-worker-5.json: \"{\\\"b\\\": 1}\"",
    "sops:",
    "    version: 3.9.4",
    "",
])


class EditSeed(unittest.TestCase):
    def test_sets_fields_in_one_scope_and_keeps_every_other_line(self):
        out = rv.edit_seed(DOC, "dev-worker-4.json", {"relay_viewer_user": "u", "relay_viewer_password": "p"})
        before, after = DOC.split("\n"), out.split("\n")
        self.assertEqual(len(before), len(after))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(changed, [3])
        _, seed, quote = rv.seed_line(out, "dev-worker-4.json")
        self.assertEqual(quote, "'")
        self.assertEqual(seed, {"gitea_pat": "it's", "relay_viewer_user": "u", "relay_viewer_password": "p"})

    def test_double_quoted_scope_round_trips(self):
        out = rv.edit_seed(DOC, "dev-worker-5.json", {"c": 2})
        _, seed, quote = rv.seed_line(out, "dev-worker-5.json")
        self.assertEqual((quote, seed), ('"', {"b": 1, "c": 2}))

    def test_a_missing_or_duplicate_scope_refuses(self):
        with self.assertRaises(SystemExit):
            rv.seed_line(DOC, "dev-worker-9.json")
        with self.assertRaises(SystemExit):
            rv.seed_line(DOC + "    common.json: '{}'\n", "common.json")

    def test_an_unquoted_scalar_refuses(self):
        with self.assertRaises(SystemExit):
            rv.seed_line("    x.json: {}\n", "x.json")


if __name__ == "__main__":
    unittest.main()
