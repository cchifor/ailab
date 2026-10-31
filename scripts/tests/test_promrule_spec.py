#!/usr/bin/env python3
"""The shared Prometheus-rule tooling: scripts/promrule-spec.py (the PrometheusRule spec extractor
scripts/rules-lint.sh hands to promtool) and scripts/rules-lint.sh itself, which CI runs over every
*-rules.yaml in kubernetes/apps/infrastructure/monitoring.

These tests lived in test_broker_refresh_intervals.py, which went with AgentForge on 2026-10-31
(plans/2026-10-31-retire-agentforge-plan.md). They were never AgentForge-specific: they guard every
rules file that remains, including the column-0-comment truncation regression. Moved verbatim.
Stdlib-only, like every module in scripts/tests.

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
MONITORING_DIR = REPO / "kubernetes/apps/infrastructure/monitoring"
RULES_LINT = REPO / "scripts/rules-lint.sh"
RULES_LINT_WORKFLOW = REPO / ".gitea/workflows/rules-lint.yaml"
SPEC_EXTRACTOR = REPO / "scripts/promrule-spec.py"


def _load_extractor():
    """scripts/promrule-spec.py, by path (hyphenated filename), lazily so each test fails alone."""
    spec = importlib.util.spec_from_file_location("promrule_spec", SPEC_EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PromruleSpecExtractorFailsClosed(unittest.TestCase):
    RULE = (
        "# leading comment\n"
        "apiVersion: monitoring.coreos.com/v1\n"
        "kind: PrometheusRule\n"
        "metadata:\n  name: x\n  namespace: monitoring\n"
        "spec:\n  groups:\n    - name: g\n      rules:\n        - alert: A\n          expr: up == 0\n"
    )

    def test_extracts_the_dedented_spec_of_a_single_prometheusrule(self) -> None:
        mod = _load_extractor()
        spec = mod.extract_spec(self.RULE, "fixture")
        self.assertTrue(spec.startswith("groups:\n"), spec)
        self.assertIn("\n    rules:\n      - alert: A\n        expr: up == 0\n", spec)

    def test_rejects_multi_document_other_kinds_and_a_missing_spec(self) -> None:
        mod = _load_extractor()
        cases = {
            "two documents": self.RULE + "---\n" + self.RULE,
            "not a PrometheusRule": self.RULE.replace("kind: PrometheusRule", "kind: ConfigMap"),
            "no spec": self.RULE.split("spec:\n")[0],
            "spec without groups": self.RULE.split("spec:\n")[0] + "spec:\n  rules: []\n",
        }
        for label, text in cases.items():
            with self.subTest(case=label), self.assertRaises(mod.SpecError):
                mod.extract_spec(text, label)

    # A column-0 comment INSIDE spec (this repo's rule files are comment-heavy) and a stray non-key
    # line at column 0. The first used to read as "next top-level key" and silently truncated the
    # extracted spec — promtool then blessed a file whose later rules it never saw. Comments must
    # be transparent; anything else at column 0 that is not a key is a SpecError, never a guess.
    COMMENTED = (
        "apiVersion: monitoring.coreos.com/v1\n"
        "kind: PrometheusRule\n"
        "metadata:\n  name: x\n  namespace: monitoring\n"
        "spec:\n  groups:\n    - name: g\n      rules:\n        - alert: A\n          expr: up == 0\n"
        "# a column-0 comment inside spec\n"
        "        - alert: B\n          expr: up == 1\n"
        "        # an indented one too\n"
        "        - record: c\n          expr: up\n"
    )

    def test_a_column_0_comment_inside_spec_does_not_truncate_the_rules(self) -> None:
        mod = _load_extractor()
        spec = mod.extract_spec(self.COMMENTED, "fixture")
        self.assertIn("- alert: B\n", spec)
        self.assertIn("- record: c\n", spec)
        self.assertEqual(mod.rule_count(spec), 3)
        self.assertEqual(mod.rule_count(self.COMMENTED), mod.rule_count(spec))

    def test_a_non_key_line_at_column_0_inside_spec_fails_closed(self) -> None:
        mod = _load_extractor()
        with self.assertRaises(mod.SpecError):
            mod.extract_spec(self.RULE + "- alert: stray\n  expr: up\n", "stray")

    def test_the_repo_rules_files_keep_every_rule_through_extraction(self) -> None:
        mod = _load_extractor()
        for path in sorted(MONITORING_DIR.glob("*-rules.yaml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertEqual(mod.rule_count(mod.extract_spec(text, path.name)), mod.rule_count(text))

    def test_cli_writes_one_file_per_input_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "a-rules.yaml"
            src.write_text(self.RULE, encoding="utf-8")
            out = pathlib.Path(tmp) / "out"
            proc = subprocess.run(
                [sys.executable, str(SPEC_EXTRACTOR), "--out", str(out), str(src)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((out / "a-rules.yaml").read_text(encoding="utf-8").startswith("groups:"))
            # The per-file and total rule counts rules-lint.sh reconciles against promtool's own
            # "N rules found" — a truncated extraction can no longer pass unnoticed.
            self.assertIn("(1 rules)", proc.stdout)
            self.assertIn("promrule-spec: 1 rules across 1 files", proc.stdout)
            bad = pathlib.Path(tmp) / "b-rules.yaml"
            bad.write_text("kind: ConfigMap\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SPEC_EXTRACTOR), "--out", str(out), str(src), str(bad)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0, "a non-PrometheusRule input must fail closed")
            self.assertIn("b-rules.yaml", proc.stderr)


class RulesLintScriptRunsPromtoolOverEveryRulesFile(unittest.TestCase):
    def test_script_exists_and_is_shell_clean(self) -> None:
        self.assertTrue(RULES_LINT.is_file(), f"{RULES_LINT} missing")
        proc = subprocess.run(["bash", "-n", str(RULES_LINT)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_script_pins_promtool_by_digest_and_globs_the_rules_files(self) -> None:
        text = RULES_LINT.read_text(encoding="utf-8")
        self.assertRegex(text, r"quay\.io/prometheus/prometheus:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}")
        self.assertIn("check rules", text)
        self.assertIn("promrule-spec.py", text)
        self.assertIn("*-rules.yaml", text)
        self.assertIn("set -euo pipefail", text)
        # Comment lines are the script explaining its own idiom ("no `|| true`"); only code counts.
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("|| true", code)
        # The extracted-vs-promtool rule count reconciliation (see PromruleSpecExtractorFailsClosed).
        self.assertIn("rules found", code)
        self.assertIn("rules across", code)

    def test_ci_runs_the_script_fail_closed_on_every_push(self) -> None:
        """Until manifest-lint.sh (C-P0-05) is on main the gate has its own workflow, in the idiom
        of broker-inventory.yaml: one job, no needs/if/continue-on-error, a timeout, push+PR."""
        self.assertTrue(RULES_LINT_WORKFLOW.is_file(), f"{RULES_LINT_WORKFLOW} missing")
        text = RULES_LINT_WORKFLOW.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        self.assertIn("bash scripts/rules-lint.sh", code)
        self.assertIn("timeout-minutes:", code)
        self.assertRegex(code, r"(?m)^on:\n(?:  .*\n)*  push:")
        self.assertRegex(code, r"(?m)^  pull_request:")
        self.assertEqual(len(re.findall(r"(?m)^  [A-Za-z0-9_-]+:\s*$", code.split("jobs:", 1)[1])), 1, "one job")
        for forbidden in ("needs:", "if:", "continue-on-error", "|| true", "matrix:"):
            self.assertNotIn(forbidden, code, forbidden)


if __name__ == "__main__":
    unittest.main()
