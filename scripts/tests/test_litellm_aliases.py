#!/usr/bin/env python3
"""Two names, one backend: every `alias_of` pair in litellm.yaml must stay identical.

WHY THIS EXISTS. A LiteLLM route is re-labelled by adding a second `model_name`
with the same `litellm_params` and hiding the old entry (runbook
model-registration.md section 5), because a rename strands every Open WebUI chat
pinned to the old id. That leaves two entries that MUST be one deployment. The
consumer generator's --check validates the derived spans, not that the two
blocks still agree, so a sampling tweak applied to one name and not the other
would ship silently and make "which name did you use" a behavioural question.
The new entry declares `model_info.alias_of: <old name>`; this test diffs the
pair and checks the old name is actually hidden, which is the point of the
alias.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import importlib.util
import pathlib
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
LITELLM = ROOT / "kubernetes" / "apps" / "apps" / "ai" / "litellm.yaml"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The generator's own block locator, so this test reads the same config.yaml the
# consumers are derived from (and the same one check-inline-hashes hashes).
glc = _load("gen_litellm_consumers_for_aliases", ROOT / "scripts" / "gen-litellm-consumers.py")


def _model_list():
    config = yaml.safe_load(glc.litellm_config_text(LITELLM.read_text(encoding="utf-8")))
    return config["model_list"]


def _alias_problems(entries):
    """Every way an `alias_of` pair in `entries` breaks the two-names-one-backend invariant (empty: none)."""
    by_name = {}
    for e in entries:
        by_name.setdefault(e["model_name"], []).append(e)
    problems = []
    strip = lambda d: {k: v for k, v in (d or {}).items() if k not in ("hidden", "alias_of")}
    for alias in (e for e in entries if (e.get("model_info") or {}).get("alias_of")):
        info, name = alias["model_info"], alias["model_name"]
        original_name = info["alias_of"]
        originals = by_name.get(original_name)
        if not originals:
            problems.append(f"{name}: alias_of names {original_name}, which is not in model_list")
            continue
        if len(originals) != 1:
            problems.append(f"{name}: an alias must point at exactly one deployment")
            continue
        original = originals[0]
        if alias["litellm_params"] != original["litellm_params"]:
            problems.append(f"{name}: alias and original are one deployment; their litellm_params must be identical")
        if (original.get("model_info") or {}).get("hidden") is not True:
            problems.append(f"{name}: the original must be hidden, or both names show up in the pickers")
        if info.get("hidden") is True:
            problems.append(f"{name}: the alias is the advertised name; it must not be hidden")
        # Everything in model_info except the two alias-mechanics keys must agree too:
        # max_input_tokens and supports_vision describe the one deployment both names reach.
        if strip(info) != strip(original.get("model_info")):
            problems.append(f"{name}: model_info must agree apart from hidden/alias_of")
        if (original.get("model_info") or {}).get("alias_of"):
            problems.append(f"{name}: no alias chains: point every alias at the deployment's original name")
    return problems


def _entry(name, temperature=1.0, **info):
    return {"model_name": name, "litellm_params": {"model": "openai/m", "api_base": "http://10.0.0.1/v1", "temperature": temperature},
            "model_info": {"max_input_tokens": 1000, **info}}


class AliasPairs(unittest.TestCase):
    def test_every_alias_matches_its_original_and_the_original_is_hidden(self):
        # Since 2026-11-05 the file may hold no pair at all (the qwen3.8-27b-fp8 pair, the only one, was retired);
        # the checker's own test below keeps this from passing vacuously.
        self.assertEqual(_alias_problems(_model_list()), [])

    def test_the_checker_finds_each_kind_of_broken_pair(self):
        good = [_entry("old", hidden=True), _entry("new", alias_of="old")]
        self.assertEqual(_alias_problems(good), [])
        cases = {
            "drifted params": [_entry("old", hidden=True), _entry("new", temperature=0.7, alias_of="old")],
            "original not hidden": [_entry("old"), _entry("new", alias_of="old")],
            "alias hidden": [_entry("old", hidden=True), _entry("new", hidden=True, alias_of="old")],
            "model_info drift": [_entry("old", hidden=True), {**_entry("new", alias_of="old"), "model_info": {"max_input_tokens": 2000, "alias_of": "old"}}],
            "missing original": [_entry("new", alias_of="gone")],
            "two originals": [_entry("old", hidden=True), _entry("old", hidden=True), _entry("new", alias_of="old")],
            "chain": [_entry("a", hidden=True), _entry("old", hidden=True, alias_of="a"), _entry("new", alias_of="old")],
        }
        for label, entries in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(_alias_problems(entries), [])


if __name__ == "__main__":
    unittest.main()
