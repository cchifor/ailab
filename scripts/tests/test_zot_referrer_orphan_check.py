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

    def test_fails_closed_on_missing_data(self):
        empty = run([], {})
        self.assertFalse(mod.is_safe(empty))  # an empty or missing manifests list proves nothing
        unreadable = run([{"digest": "x"}], {})  # the blob could not be read
        self.assertEqual(unreadable["unreadable"], 1)
        self.assertFalse(mod.is_safe(unreadable))
        good = run([{"digest": "img"}], {"img": {"config": {}}})
        self.assertTrue(mod.is_safe(good))

    def test_main_exits_1_for_an_unreadable_or_empty_index(self):
        import io, json, tempfile, contextlib
        with tempfile.TemporaryDirectory() as root:
            os_repo = Path(root) / "strive" / "x"
            os_repo.mkdir(parents=True)
            (os_repo / "index.json").write_text("{}")  # no manifests at all
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = mod.main(["--root", root, "strive/x", "strive/missing"])
            self.assertEqual(rc, 1)
            lines = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([line["safe"] for line in lines], [False, False])

    def test_nested_indexes_are_walked(self):
        entries = [{"digest": "outer"}, {"digest": "inner"}]
        docs = {"outer": {"manifests": [{"digest": "inner"}]}, "inner": {"manifests": [{"digest": "leaf"}]}}
        self.assertEqual(run(entries, docs)["nested_not_top"], 1)  # leaf is referenced but not top-level


if __name__ == "__main__":
    unittest.main()
