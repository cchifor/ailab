#!/usr/bin/env python3
"""Unit tests for scripts/gen-agent-team.py.

The generator edits YAML by line, which is fast and readable and has exactly one
recurring hazard: an edit that matches at the wrong indentation. Both bugs found
in review were that shape --

  * `--set` APPENDED beside an existing key instead of replacing it, producing
    `maxRounds: 64` followed by `maxRounds: 8`: two values for one bound, which
    YAML resolves last-wins and no reader notices.
  * the replacement then matched ANY depth inside the row, so a key nested under
    `agentOptions` (which has its own `provider` and `model`) could be rewritten
    instead of the direct config key -- and with both present, whichever came
    first in the file won, which is position luck rather than intent.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts" / "gen-agent-team.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_agent_team", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _yaml(text):
    import yaml
    return yaml.safe_load(text)


NESTED = """- id: tool-subagent
  name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: spawn
    toolName: subagent
    agentOptions:
      provider: litellm
      model: qwen
- id: other
  name: y
"""

# THE NESTED MAPPING COMES FIRST HERE, DELIBERATELY. With the direct key first
# (as in NESTED above), a depth-blind scan still hits the right line by position
# and the test passes against the bug -- which is exactly the position luck the
# review described. Only this ordering exposes it: a depth-blind matcher rewrites
# agentOptions.provider and leaves config.provider untouched. Verified to fail
# against the pre-fix generator.
NESTED_FIRST = """- id: tool-subagent
  name: '@deepseek-ai/dsh-tool-subagent'
  config:
    agentOptions:
      provider: litellm
      model: qwen
    provider: spawn
    toolName: subagent
"""


class SetKeys(unittest.TestCase):
    def setUp(self):
        self.gen = _load()

    def test_replaces_the_direct_key_not_the_nested_one(self):
        out, hit = self.gen.set_keys(NESTED, {"tool-subagent": [("provider", "REPLACED")]})
        self.assertEqual(hit, {"tool-subagent"})
        cfg = _yaml(out)[0]["config"]
        self.assertEqual(cfg["provider"], "REPLACED")
        # The whole point: agentOptions.provider must be untouched.
        self.assertEqual(cfg["agentOptions"]["provider"], "litellm")

    def test_nested_key_earlier_in_the_file_is_not_mistaken_for_the_direct_one(self):
        """The case a depth-blind matcher actually gets wrong."""
        out, _ = self.gen.set_keys(NESTED_FIRST, {"tool-subagent": [("provider", "REPLACED")]})
        cfg = _yaml(out)["config"] if isinstance(_yaml(out), dict) else _yaml(out)[0]["config"]
        self.assertEqual(cfg["agentOptions"]["provider"], "litellm",
                         "rewrote the NESTED provider -- matcher is depth-blind")
        self.assertEqual(cfg["provider"], "REPLACED",
                         "left the direct provider unchanged")

    def test_replaces_rather_than_duplicating(self):
        out, _ = self.gen.set_keys(NESTED, {"tool-subagent": [("toolName", "renamed")]})
        self.assertEqual(out.count("toolName:"), 1, "a second key would resolve last-wins silently")
        self.assertEqual(_yaml(out)[0]["config"]["toolName"], "renamed")

    def test_appends_a_key_that_is_absent(self):
        out, _ = self.gen.set_keys(NESTED, {"tool-subagent": [("maxDepth", "2")]})
        self.assertEqual(_yaml(out)[0]["config"]["maxDepth"], 2)

    def test_leaves_other_rows_alone(self):
        out, _ = self.gen.set_keys(NESTED, {"tool-subagent": [("provider", "X")]})
        self.assertEqual(_yaml(out)[1], {"id": "other", "name": "y"})


class DropAndEnable(unittest.TestCase):
    def setUp(self):
        self.gen = _load()

    def test_drop_removes_the_row_entirely(self):
        out, hit = self.gen.drop_rows(NESTED, {"tool-subagent"})
        self.assertEqual(hit, {"tool-subagent"})
        self.assertEqual([r["id"] for r in _yaml(out)], ["other"])

    def test_enable_removes_the_comment_above_the_flag(self):
        """A cleared flag with its 'Dormant until ...' note still standing is a
        file that contradicts itself, and regeneration would reintroduce it."""
        src = (
            "- id: a\n  name: x\n"
            "  # Dormant until WP-2/WP-5/WP-6 land -- see this file's header.\n"
            "  disabled: true\n"
            "- id: b\n  name: y\n"
        )
        out, hit = self.gen.enable_rows(src, {"a"})
        self.assertEqual(hit, {"a"})
        self.assertNotIn("Dormant until", out, "stale comment left above a now-active row")
        rows = {r["id"]: r for r in _yaml(out)}
        self.assertNotIn("disabled", rows["a"])

    def test_enable_clears_only_the_named_rows_flag(self):
        src = "- id: a\n  name: x\n  disabled: true\n- id: b\n  name: y\n  disabled: true\n"
        out, hit = self.gen.enable_rows(src, {"a"})
        self.assertEqual(hit, {"a"})
        rows = {r["id"]: r for r in _yaml(out)}
        self.assertNotIn("disabled", rows["a"])
        self.assertTrue(rows["b"]["disabled"])


if __name__ == "__main__":
    unittest.main()
