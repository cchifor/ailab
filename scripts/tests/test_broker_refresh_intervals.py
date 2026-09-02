#!/usr/bin/env python3
"""Unit tests for C-P0-08: OAuth rotation propagation + the P0 engine alerts.

Three facts, pinned so that a later edit cannot quietly undo them:

  1. every `*-oauth` ExternalSecret in kubernetes/apps/infrastructure/agentforge-broker (one per
     seat, the same seat set scripts/gen-broker-inventory.py derives) refreshes within 5 minutes.
     A rotation happens BECAUSE the old credential is failing, so the ESO fetch interval is the
     first term of the "rotate -> broker serves the new token" latency; at the former 1h it was
     ~65 min end-to-end, at 5m it is ~6 min (5m fetch + 60s broker file reload).
  2. the shared broker ConfigMap carries AF_BROKER_OAUTH_RELOAD_INTERVAL_S explicitly, at 60s —
     the second term of that latency. Before this it was the IMPLICIT 300s code default
     (agentforge broker/config.py BrokerSettings.oauth_reload_interval_s), i.e. invisible in the
     manifest that documents every other AF_BROKER_* knob, and 5x slower than the -kids reload it
     sits beside.
  3. monitoring/agentforge-rules.yaml carries the three alerts that consume the P0 engine gauges
     and counters (ForgeDispatchStalled, ForgeUnexplainedEscalations, ForgeOperatorServiceFailing),
     and the file extracts as a single PrometheusRule spec that scripts/rules-lint.sh hands to
     `promtool check rules` (the real parser; this module only checks structure).

Nothing here talks to a cluster (BRIEFING: never run kubectl against ailab) and nothing here runs
docker: `promtool` itself is exercised by scripts/rules-lint.sh, which CI runs. Stdlib-only, like
every module in scripts/tests — the CI runner installs no dependency (no PyYAML), so the YAML is
read with scripts/gen-broker-inventory.py's own private block helpers (loaded by path, as
test_broker_servicemonitor.py does) rather than a second parser that could disagree with the one
that actually derives the seat inventory. Runs against the REAL repo tree, read-only.

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
BROKER_DIR = REPO / "kubernetes/apps/infrastructure/agentforge-broker"
CONFIGMAP = BROKER_DIR / "configmap.yaml"
MONITORING_DIR = REPO / "kubernetes/apps/infrastructure/monitoring"
RULES = MONITORING_DIR / "agentforge-rules.yaml"
RULES_LINT = REPO / "scripts/rules-lint.sh"
RULES_LINT_WORKFLOW = REPO / ".gitea/workflows/rules-lint.yaml"
SPEC_EXTRACTOR = REPO / "scripts/promrule-spec.py"

_MOD_PATH = REPO / "scripts" / "gen-broker-inventory.py"
_spec = importlib.util.spec_from_file_location("gen_broker_inventory", _MOD_PATH)
gbi = importlib.util.module_from_spec(_spec)
sys.modules["gen_broker_inventory"] = gbi
_spec.loader.exec_module(gbi)  # must NOT perform any I/O at import time

#: The ceiling the spec sets for the -oauth ExternalSecret fetch interval.
OAUTH_REFRESH_CEILING_S = 5 * 60
#: The knob (BrokerSettings.oauth_reload_interval_s, env prefix AF_BROKER_) and the value it
#: must carry: in step with AF_BROKER_KID_RELOAD_INTERVAL_S, and well under the 300s code default.
OAUTH_RELOAD_KNOB = "AF_BROKER_OAUTH_RELOAD_INTERVAL_S"
OAUTH_RELOAD_S = 60
#: alert name -> the engine series its expr must read.
P0_ALERTS = {
    "ForgeDispatchStalled": "forge_last_dispatch_timestamp",
    "ForgeUnexplainedEscalations": "forge_escalations_unexplained_total",
    "ForgeOperatorServiceFailing": "forge_operator_service_failures_total",
}

# The Go-duration subset ESO's refreshInterval is written in here (h/m/s, in that order). Anything
# else (a bare number, "5min", "") is a ValueError so a typo fails the test instead of parsing as 0.
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _seconds(duration: str) -> int:
    m = _DURATION.match(duration)
    if not duration or not m:
        raise ValueError(f"not an ESO duration: {duration!r}")
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _broker_files() -> list[pathlib.Path]:
    """The seat manifests (mirrors load_seats' own glob, minus the derived inventory file)."""
    return sorted(p for p in BROKER_DIR.glob("broker-*.yaml") if p != gbi.INVENTORY)


def _oauth_external_secrets() -> list[tuple[str, str, str | None]]:
    """(file, name, spec.refreshInterval) for every ExternalSecret named `*-oauth`."""
    out: list[tuple[str, str, str | None]] = []
    for path in _broker_files():
        for doc in gbi._docs(path.read_text(encoding="utf-8")):
            if gbi._kind(doc) != "ExternalSecret":
                continue
            name = gbi._name(doc)
            if not name or not name.endswith("-oauth"):
                continue
            interval = gbi._field(gbi._top_block(doc, "spec"), "refreshInterval", 2)
            out.append((path.name, name, interval))
    return out


def _load_extractor():
    """scripts/promrule-spec.py, by path (hyphenated filename), lazily so each test fails alone."""
    spec = importlib.util.spec_from_file_location("promrule_spec", SPEC_EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _alert_blocks(spec_text: str) -> dict[str, str]:
    """alert name -> the text of that rule (from its `- alert:` line to the next rule)."""
    starts = [
        (m.start(), m.group(1))
        for m in re.finditer(r"(?m)^[ ]*-[ ]*alert:[ \t]*(\S+)[ \t]*$", spec_text)
    ]
    blocks: dict[str, str] = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(spec_text)
        blocks[name] = spec_text[pos:end]
    return blocks


class OauthExternalSecretsRefreshWithinFiveMinutes(unittest.TestCase):
    def test_every_seat_has_one_oauth_externalsecret_refreshing_within_5m(self) -> None:
        found = _oauth_external_secrets()
        seats = gbi.load_seats()
        self.assertEqual(
            len(found),
            len(seats),
            f"expected one *-oauth ExternalSecret per seat ({len(seats)}), found {found}",
        )
        for file, name, interval in found:
            with self.subTest(externalsecret=name):
                self.assertIsNotNone(interval, f"{file}: {name} has no spec.refreshInterval")
                self.assertLessEqual(
                    _seconds(interval),
                    OAUTH_REFRESH_CEILING_S,
                    f"{file}: {name} refreshInterval {interval!r} exceeds 5m — a rotation happens "
                    "because the old credential is failing, so ESO must fetch the new one fast",
                )

    def test_duration_helper_is_strict(self) -> None:
        self.assertEqual(_seconds("5m"), 300)
        self.assertEqual(_seconds("1h"), 3600)
        self.assertEqual(_seconds("1h30m"), 5400)
        self.assertEqual(_seconds("90s"), 90)
        for bad in ("", "5", "5min", "m5", "1d"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _seconds(bad)


class ConfigMapPinsTheOauthReloadInterval(unittest.TestCase):
    def _data(self) -> str:
        docs = [d for d in gbi._docs(CONFIGMAP.read_text(encoding="utf-8")) if gbi._kind(d)]
        self.assertEqual([gbi._kind(d) for d in docs], ["ConfigMap"])
        return gbi._top_block(docs[0], "data")

    def test_oauth_reload_interval_is_explicit_and_60s(self) -> None:
        data = self._data()
        value = gbi._field(data, OAUTH_RELOAD_KNOB, 2)
        self.assertIsNotNone(
            value,
            f"{CONFIGMAP.name}: {OAUTH_RELOAD_KNOB} missing — the broker then falls back to the "
            "IMPLICIT 300s code default and the 5m ExternalSecret refresh buys nothing",
        )
        self.assertEqual(int(value), OAUTH_RELOAD_S, f"{OAUTH_RELOAD_KNOB}={value!r}")

    def test_oauth_reload_sits_beside_the_kid_reload_and_matches_it(self) -> None:
        data = self._data()
        kid = gbi._field(data, "AF_BROKER_KID_RELOAD_INTERVAL_S", 2)
        oauth = gbi._field(data, OAUTH_RELOAD_KNOB, 2)
        self.assertIsNotNone(kid)
        self.assertIsNotNone(oauth)
        self.assertEqual(int(oauth), int(kid), "the two mounted-file reloaders must run in step")
        self.assertLess(
            int(oauth),
            OAUTH_REFRESH_CEILING_S,
            "the file reload must be faster than the ESO fetch it follows, or it is the bottleneck",
        )


class AgentforgeRulesCarryTheP0EngineAlerts(unittest.TestCase):
    def _spec(self) -> str:
        mod = _load_extractor()
        return mod.extract_spec(RULES.read_text(encoding="utf-8"), RULES.name)

    def test_rules_file_extracts_as_one_prometheusrule_spec(self) -> None:
        spec = self._spec()
        self.assertRegex(spec, r"(?m)^groups:[ \t]*$")
        self.assertNotRegex(spec, r"(?m)^(apiVersion|kind|metadata):", "metadata leaked into the spec")

    def test_the_three_p0_alerts_exist_and_read_the_engine_series(self) -> None:
        blocks = _alert_blocks(self._spec())
        for alert, series in P0_ALERTS.items():
            with self.subTest(alert=alert):
                self.assertIn(alert, blocks, f"{RULES.name} has no `- alert: {alert}`")
                block = blocks[alert]
                self.assertRegex(block, r"(?m)^[ ]*expr:")
                self.assertIn(series, block, f"{alert} does not read {series}")
                self.assertRegex(block, r"severity:[ \t]*warning")
                self.assertRegex(block, r"(?m)^[ ]*summary:")
                self.assertRegex(block, r"(?m)^[ ]*description:")

    def test_dispatch_stalled_is_pinned_to_the_dispatcher_and_debounced(self) -> None:
        block = _alert_blocks(self._spec())["ForgeDispatchStalled"]
        # The gauge is a structural 0 until the first successful pass (and on every worker,
        # which builds the same registry): the `> 0` inner filter is what keeps `time() - 0`
        # from breaching forever, exactly as ForgeReconcileStalled does.
        self.assertIn('job="agentforge-dispatcher"', block)
        self.assertRegex(block, r"forge_last_dispatch_timestamp\{[^}]*\}[ ]*>[ ]*0")
        self.assertRegex(block, r">[ ]*600")
        self.assertRegex(block, r"(?m)^[ ]*for:[ \t]*5m")

    def test_no_alert_name_is_declared_twice(self) -> None:
        names = re.findall(r"(?m)^[ ]*-[ ]*alert:[ \t]*(\S+)", self._spec())
        self.assertEqual(len(names), len(set(names)), f"duplicate alert names in {names}")


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
