"""Unit tests for scripts/promtest-refs.py, the fail-closed half of the promtool test gate.

WHY: `promtool test rules` only WARNS when a `rule_files:` entry matches no file. A fixture
naming a rules file that was never extracted therefore loads ZERO rules, and every
"expect no alerts" case in it passes vacuously — a gate that checks nothing. rules-lint.sh
refuses to invoke promtool unless this script resolves every reference, so the shapes that
must be REJECTED matter as much as the ones that must parse.
"""
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "scripts" / "promtest-refs.py"

_spec = importlib.util.spec_from_file_location("promtest_refs", SRC)
promtest_refs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(promtest_refs)


class ReferencesTest(unittest.TestCase):
    def test_normal_block(self):
        text = "rule_files:\n  - a-rules.yaml\n  - b-rules.yaml\n\nevaluation_interval: 1m\n"
        self.assertEqual(["a-rules.yaml", "b-rules.yaml"], promtest_refs.references(text))

    def test_block_at_end_of_file(self):
        """The shape the first version missed: its regex required a following top-level key,
        so a fixture ending on its rule_files list yielded zero references and passed."""
        self.assertEqual(["a-rules.yaml"], promtest_refs.references("rule_files:\n  - a-rules.yaml\n"))

    def test_block_stops_at_the_next_top_level_key(self):
        text = "rule_files:\n  - a-rules.yaml\ntests:\n  - interval: 1m\n"
        self.assertEqual(["a-rules.yaml"], promtest_refs.references(text))

    def test_inline_list_is_not_parsed(self):
        """Refused rather than guessed at: returning [] makes main() exit non-zero."""
        self.assertEqual([], promtest_refs.references("rule_files: [a-rules.yaml]\ntests: []\n"))

    def test_absent_block(self):
        self.assertEqual([], promtest_refs.references("tests: []\n"))

    def test_comments_and_blank_lines_are_ignored(self):
        text = "rule_files:\n  # a comment\n\n  - a-rules.yaml\n\ntests: []\n"
        self.assertEqual(["a-rules.yaml"], promtest_refs.references(text))


class ExitCodeTest(unittest.TestCase):
    def _run(self, body):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        f = pathlib.Path(d.name) / "fixture.yaml"
        f.write_text(body, encoding="utf-8")
        return promtest_refs.main(["promtest-refs.py", str(f)])

    def test_zero_on_a_parsable_list(self):
        self.assertEqual(0, self._run("rule_files:\n  - a-rules.yaml\ntests: []\n"))

    def test_one_when_nothing_is_parsed(self):
        self.assertEqual(1, self._run("tests: []\n"))

    def test_one_on_an_inline_list(self):
        self.assertEqual(1, self._run("rule_files: [a-rules.yaml]\ntests: []\n"))

    def test_two_on_wrong_arity(self):
        self.assertEqual(2, promtest_refs.main(["promtest-refs.py"]))


class RealFixtureTest(unittest.TestCase):
    def test_every_shipped_fixture_resolves_to_a_shipped_rules_file(self):
        """The check rules-lint.sh performs, asserted here too so a rename that breaks it is
        caught by the unit suite and not only by the CI gate."""
        rules_dir = ROOT / "kubernetes" / "apps" / "infrastructure" / "monitoring"
        fixtures = sorted(rules_dir.glob("*-rules.test.yaml"))
        self.assertTrue(fixtures, "expected at least one promtool fixture")
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                refs = promtest_refs.references(fixture.read_text(encoding="utf-8"))
                self.assertTrue(refs, f"{fixture.name} parsed no rule_files entries")
                for ref in refs:
                    self.assertTrue((rules_dir / ref).is_file(),
                                    f"{fixture.name} references missing {ref}")


if __name__ == "__main__":
    unittest.main()
