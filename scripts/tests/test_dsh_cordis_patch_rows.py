#!/usr/bin/env python3
"""Wiring gates for deployment-owned overrides in the dsh cordis.patch.yml.

WHY THIS EXISTS. A row override in cordis.patch.yml is applied by id, and the
include plugin SKIPS an override whose id is absent or whose `name` does not
match the shipped row -- with one stderr line, no failure. So an override that
drifts from the shipped package name is a silent no-op: the pod boots, and the
thing the override was meant to switch off comes back. This pins the shape of
the overrides that exist for exactly that reason, next to the rows themselves.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PATCH = ROOT / "kubernetes" / "apps" / "apps" / "dsh" / "cordis.patch.yml"


class _TolerantLoader(yaml.SafeLoader):
    """cordis.patch.yml carries `!!js` tags this test has no need to evaluate."""


_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def _rows():
    return [r for r in yaml.load(PATCH.read_text(encoding="utf-8"), Loader=_TolerantLoader) if isinstance(r, dict)]


class InternalTestingNoticeOff(unittest.TestCase):
    """The welcome-notice package is switched off by the documented row form."""

    def test_row_is_disabled_with_the_name_assertion(self):
        rows = [r for r in _rows() if r.get("id") == "ui-settings-models"]
        self.assertEqual(len(rows), 1, "expected exactly one override of ui-settings-models")
        row = rows[0]
        self.assertIs(row.get("disabled"), True)
        # The assertion that protects against disabling the wrong row if a
        # release moves the id: the include plugin skips a name mismatch.
        self.assertEqual(row.get("name"), "@deepseek-ai/dsh-client-ui-settings-models")
        # An override assigns only the keys it lists; the shipped row has no
        # config, and adding one here would replace, not merge.
        self.assertNotIn("config", row)


if __name__ == "__main__":
    unittest.main()
