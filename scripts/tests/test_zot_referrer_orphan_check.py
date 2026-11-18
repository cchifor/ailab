"""Tests for scripts/zot-referrer-orphan-check.py: the deleteReferrers safety pre-check (no store needed)."""
import importlib.util
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "zot-referrer-orphan-check.py"
_spec = importlib.util.spec_from_file_location("zot_referrer_orphan_check", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def run(entries, docs):
    return mod.check_repo(entries, lambda d: docs.get(d))


class OrphanCheckTest(unittest.TestCase):
    def test_store_shape_seen_live_is_safe(self):
        # buildx shape seen on 2026-11-18: the index AND its platform children are all top-level entries.
        entries = [{"digest": "idx", "annotations": {mod.TAG: "sha-1"}}, {"digest": "amd64"}, {"digest": "att-live"},
                   {"digest": "att-orphan"}]
        docs = {"idx": {"manifests": [{"digest": "amd64"}]}, "att-live": {"subject": {"digest": "amd64"}},
                "att-orphan": {"subject": {"digest": "gone"}}}
        res = run(entries, docs)
        self.assertEqual((res["nested_not_top"], res["orphans"], res["kept_referrers"], res["tagged_orphans"]),
                         (0, 1, 1, 0))

    def test_live_child_missing_from_top_level_is_flagged(self):
        # zot's top-level-only lookup would delete att-live although its subject is a child of a kept index.
        entries = [{"digest": "idx", "annotations": {mod.TAG: "sha-1"}}, {"digest": "att-live"}]
        docs = {"idx": {"manifests": [{"digest": "amd64"}]}, "att-live": {"subject": {"digest": "amd64"}}}
        res = run(entries, docs)
        self.assertEqual(res["nested_not_top"], 1)
        self.assertEqual(res["orphans"], 1)

    def test_tagged_orphan_is_flagged(self):
        entries = [{"digest": "att", "annotations": {mod.TAG: "latest"}}]
        res = run(entries, {"att": {"subject": {"digest": "gone"}}})
        self.assertEqual(res["tagged_orphans"], 1)

    def test_nested_indexes_are_walked(self):
        entries = [{"digest": "outer"}, {"digest": "inner"}]
        docs = {"outer": {"manifests": [{"digest": "inner"}]}, "inner": {"manifests": [{"digest": "leaf"}]}}
        self.assertEqual(run(entries, docs)["nested_not_top"], 1)  # leaf is referenced but not top-level


if __name__ == "__main__":
    unittest.main()
