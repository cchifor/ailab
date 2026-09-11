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


class AliasPairs(unittest.TestCase):
    def setUp(self):
        self.entries = _model_list()
        self.by_name = {}
        for e in self.entries:
            self.by_name.setdefault(e["model_name"], []).append(e)

    def test_every_alias_matches_its_original_and_the_original_is_hidden(self):
        aliases = [e for e in self.entries if (e.get("model_info") or {}).get("alias_of")]
        self.assertGreaterEqual(len(aliases), 1, "expected at least the qwen3.8-27b-fp8 alias pair")
        for alias in aliases:
            info = alias["model_info"]
            original_name = info["alias_of"]
            with self.subTest(alias=alias["model_name"], original=original_name):
                originals = self.by_name.get(original_name)
                self.assertIsNotNone(originals, f"alias_of names {original_name}, which is not in model_list")
                self.assertEqual(len(originals), 1, "an alias must point at exactly one deployment")
                original = originals[0]
                self.assertEqual(
                    alias["litellm_params"],
                    original["litellm_params"],
                    "alias and original are one deployment; their litellm_params must be identical",
                )
                self.assertIs(
                    (original.get("model_info") or {}).get("hidden"),
                    True,
                    "the original must be hidden, or both names show up in the pickers",
                )
                self.assertIsNot(info.get("hidden"), True, "the alias is the advertised name; it must not be hidden")
                # Everything in model_info except the two alias-mechanics keys must agree too:
                # max_input_tokens and supports_vision shape requests and the consumers' rows.
                strip = lambda d: {k: v for k, v in (d or {}).items() if k not in ("hidden", "alias_of")}
                self.assertEqual(strip(info), strip(original.get("model_info")), "model_info must agree apart from hidden/alias_of")

    def test_alias_names_are_not_themselves_aliased(self):
        for e in self.entries:
            target = (e.get("model_info") or {}).get("alias_of")
            if target:
                self.assertIsNone(
                    (self.by_name[target][0].get("model_info") or {}).get("alias_of"),
                    "no alias chains: point every alias at the deployment's original name",
                )


if __name__ == "__main__":
    unittest.main()
