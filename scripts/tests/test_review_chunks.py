"""Offline regressions for complete, immutable bounded review snapshots.

No bot import, credentials, providers or network are needed. These tests exercise
byte coverage and the persisted approval boundary, including damaged/replayed state.
"""
import base64
import copy
import hashlib
import io
import importlib.util
import json
import pathlib
import re
import sqlite3
import sys
import tempfile
import threading
import types
import urllib.error
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "ansible" / "roles" / "pr_reviewer" / "files" / "review_chunks.py"
SPEC = importlib.util.spec_from_file_location("review_chunks_under_test", SRC)
chunks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chunks)
ADAPTER_SPEC = importlib.util.spec_from_file_location(
    "review_chunk_adapter_under_test", SRC.with_name("review_chunk_adapter.py")
)
adapter = importlib.util.module_from_spec(ADAPTER_SPEC)
with mock.patch.dict(sys.modules, {"review_chunks": chunks}):
    ADAPTER_SPEC.loader.exec_module(adapter)

BINDING = {
    "repo": "example/project",
    "pr": 17,
    "base_repo": "example/project",
    "base_ref": "main",
    "base_sha": "1" * 40,
    "head_repo": "example/project",
    "head_ref": "feature/large-review",
    "head_sha": "2" * 40,
    "policy": "3" * 64,
}
MERGE_BASE = "4" * 40
CLEAN = {"summary": "All content in this part was reviewed.", "findings": []}


def section(path="src/example.py", content="new content"):
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1 +1 @@\n-old content\n+" + content + "\n"
    ).encode("utf-8")


def finding(path="src/0.py", severity="blocker"):
    return {
        "path": path,
        "side": "NEW",
        "line": 1,
        "severity": severity,
        "confidence": "high",
        "body": "This changed line permits incomplete data to be approved.",
    }


def multipart_plan(**overrides):
    raw = b"".join(section(f"src/{i}.py", str(i) * 2400) for i in range(3))
    return chunks.make_plan(
        dict(BINDING, **overrides), MERGE_BASE, raw, part_bytes=4096
    )


class SnapshotPlanTests(unittest.TestCase):
    def test_packing_preserves_whole_sections_and_hunk_text_is_not_metadata(self):
        sections = [
            section("a.py", "diff --git a/fake.py b/fake.py"),
            section("b.py", "--- a/fake.py"),
            section("c.py", "+++ b/fake.py"),
        ]
        plan = chunks.make_plan(BINDING, MERGE_BASE, b"".join(sections),
                                part_bytes=len(sections[0]) + len(sections[1]))
        self.assertEqual(2, len(plan["parts"]))
        self.assertEqual(sections[0] + sections[1], plan["parts"][0]["text"].encode())
        self.assertEqual(sections[2], plan["parts"][1]["text"].encode())
        self.assertEqual(["a.py", "b.py", "c.py"], [f["new"] for f in plan["files"]])
        self.assertEqual([0, 0, 1], [f["part"] for f in plan["files"]])

    def test_multiple_hunks_context_and_missing_newline_markers_are_preserved(self):
        raw = (
            b"diff --git a/example.py b/example.py\n"
            b"--- a/example.py\n+++ b/example.py\n"
            b"@@ -1,2 +1,2 @@ function\n context\n-old\n+new\n"
            b"@@ -10 +10 @@\n-last old\n\\ No newline at end of file\n"
            b"+last new\n\\ No newline at end of file\n"
        )
        plan = chunks.make_plan(BINDING, MERGE_BASE, raw)
        self.assertEqual(raw, plan["parts"][0]["text"].encode())
        self.assertEqual(1, len(plan["files"]))

    def test_exact_ordered_byte_coverage_including_locks_and_docs(self):
        sections = [
            section("src/a.py", "a" * 2400),
            section("deps.lock", "b" * 2400),
            section("docs/guide.md", "c" * 2400),
        ]
        raw = b"".join(sections)
        plan = chunks.make_plan(BINDING, MERGE_BASE, raw, part_bytes=4096)
        self.assertEqual(raw, b"".join(p["text"].encode() for p in plan["parts"]))
        self.assertEqual(len(raw), plan["bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), plan["raw_sha256"])
        self.assertEqual(3, len(plan["parts"]))
        self.assertEqual([0, 1, 2], [p["index"] for p in plan["parts"]])
        self.assertEqual([0, 1, 2], [f["part"] for f in plan["files"]])
        for file, raw_section, part in zip(plan["files"], sections, plan["parts"]):
            self.assertEqual(len(raw_section), file["bytes"])
            self.assertEqual(hashlib.sha256(raw_section).hexdigest(), file["sha256"])
            self.assertEqual(file["sha256"], part["digest"])
            self.assertLessEqual(len(part["text"].encode()), 4096)

    def test_identity_is_deterministic_and_copies_input_binding(self):
        binding = dict(BINDING)
        first = chunks.make_plan(binding, MERGE_BASE, section())
        second = chunks.make_plan(dict(reversed(list(binding.items()))), MERGE_BASE, section())
        self.assertEqual(first, second)
        binding["base_sha"] = "9" * 40
        self.assertEqual(BINDING["base_sha"], first["binding"]["base_sha"])

    def test_every_binding_field_changes_snapshot_identity(self):
        original = multipart_plan()
        alternatives = {
            "pr": 18,
            "base_ref": "release",
            "base_sha": "5" * 40,
            "head_repo": "fork/project",
            "head_ref": "feature/other",
            "head_sha": "6" * 40,
            "policy": "7" * 64,
        }
        for key, value in alternatives.items():
            with self.subTest(field=key):
                self.assertNotEqual(original["id"], multipart_plan(**{key: value})["id"])
        other_repo = multipart_plan(repo="example/other", base_repo="example/other")
        self.assertNotEqual(original["id"], other_repo["id"])
        raw = section()
        a = chunks.make_plan(BINDING, MERGE_BASE, raw)
        b = chunks.make_plan(BINDING, "8" * 40, raw)
        c = chunks.make_plan(BINDING, MERGE_BASE, raw, part_bytes=200000)
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])

    def test_limits_are_bytes_not_unicode_characters(self):
        raw = section(content="\u00e9" * 300)
        limit = len(raw.decode())
        self.assertGreater(len(raw), limit)
        with self.assertRaises(ValueError):
            chunks.make_plan(BINDING, MERGE_BASE, raw, part_bytes=limit)
        plan = chunks.make_plan(BINDING, MERGE_BASE, raw, part_bytes=len(raw))
        self.assertEqual(raw, plan["parts"][0]["text"].encode())

    def test_oversized_file_total_intake_and_part_budget_fail_closed(self):
        cases = [
            (section(content="x" * 2000), {"part_bytes": 1024}),
            (section("a", "x" * 700) + section("b", "x" * 700),
             {"part_bytes": 1024, "max_raw_bytes": 1024}),
            (section("a", "x" * 700) + section("b", "x" * 700),
             {"part_bytes": 1024, "max_parts": 1}),
        ]
        for raw, limits in cases:
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                chunks.make_plan(BINDING, MERGE_BASE, raw, **limits)

    def test_invalid_limits_cannot_disable_bounds(self):
        for key, values in {
            "part_bytes": [True, 0, 127, 400001],
            "max_raw_bytes": [0, 399999, 33554433],
            "max_parts": [True, 0, 129],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    chunks.make_plan(BINDING, MERGE_BASE, section(), **{key: value})

    def test_invalid_binding_fails_closed(self):
        invalid = [
            dict(BINDING, pr=True), dict(BINDING, pr=0),
            dict(BINDING, repo="../project"),
            dict(BINDING, base_repo="other/project"),
            dict(BINDING, head_sha="1234567"),
            dict(BINDING, base_sha="z" * 40),
            dict(BINDING, head_ref="-injected-option"),
            dict(BINDING, base_ref="main\nother"),
            dict(BINDING, policy="unknown"),
            dict(BINDING, persona="codex"),
            {k: v for k, v in BINDING.items() if k != "policy"},
        ]
        for binding in invalid:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                chunks.make_plan(binding, MERGE_BASE, section())

    def test_unsupported_content_and_duplicate_sections_fail_closed(self):
        raw = section()
        for bad in [
            b"", raw.decode(), b"preamble\n" + raw, raw + raw,
            raw.replace(b"+new content", b"+\xff"),
            raw.replace(b"+new content", b"+\x00"),
            b"diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n",
            b"diff --git a/image.png b/image.png\nGIT binary patch\nliteral 3\nabc\n",
            section("../outside"),
        ]:
            with self.subTest(raw=repr(bad)[:100]), self.assertRaises(ValueError):
                chunks.make_plan(BINDING, MERGE_BASE, bad)

    def test_malformed_or_truncated_diff_cannot_count_as_coverage(self):
        raw = section()
        malformed = {
            "header only": b"diff --git a/x b/x\n",
            "arbitrary content": b"diff --git a/x b/x\nnot a real diff\n",
            "missing hunk": raw.split(b"@@", 1)[0],
            "truncated hunk": raw.rsplit(b"+new content", 1)[0],
            "incorrect hunk counts": raw.replace(b"@@ -1 +1 @@", b"@@ -1,7 +1,9 @@"),
            "unprefixed hunk line": raw.replace(b"+new content", b"new content"),
            "one path header": raw.replace(b"+++ b/src/example.py\n", b""),
            "extra hunk data": raw + b"+undeclared line\n",
            "overlapping hunks": raw + b"@@ -1 +1 @@\n-again\n+again\n",
            "truncated transport": raw[:-1],
            "incomplete rename": b"diff --git a/x b/y\nrename from x\n",
        }
        for label, bad in malformed.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                chunks.make_plan(BINDING, MERGE_BASE, bad)

    def test_add_delete_rename_copy_mode_and_quoted_paths_are_accounted(self):
        fixtures = [
            (b"diff --git a/new.py b/new.py\nnew file mode 100644\n"
             b"--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hello\n", None, "new.py"),
            (b"diff --git a/old.py b/old.py\ndeleted file mode 100644\n"
             b"--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-hello\n", "old.py", None),
            (b"diff --git a/old.py b/new.py\nsimilarity index 100%\n"
             b"rename from old.py\nrename to new.py\n", "old.py", "new.py"),
            (b"diff --git a/old.py b/copy.py\nsimilarity index 100%\n"
             b"copy from old.py\ncopy to copy.py\n", "old.py", "copy.py"),
            (b"diff --git a/run task b/run task\nold mode 100644\nnew mode 100755\n",
             "run task", "run task"),
            (b"diff --git a/empty b/empty\nnew file mode 100644\n"
             b"index 0000000..e69de29\n", None, "empty"),
            (b"diff --git a/empty b/empty\ndeleted file mode 100644\n"
             b"index e69de29..0000000\n", "empty", None),
            (b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
             b'--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n'
             b"@@ -1 +1 @@\n-before\n+after\n", "caf\u00e9.py", "caf\u00e9.py"),
        ]
        for raw, old, new in fixtures:
            with self.subTest(old=old, new=new):
                plan = chunks.make_plan(BINDING, MERGE_BASE, raw)
                self.assertEqual((old, new), (plan["files"][0]["old"], plan["files"][0]["new"]))
                self.assertEqual(raw, plan["parts"][0]["text"].encode())

    def test_git_header_tab_suffix_is_metadata_and_control_paths_fail_closed(self):
        raw = (
            b"diff --git a/src/example.py b/src/example.py\n"
            b"index 1111111..2222222 100644\n"
            b"--- a/src/example.py\t2026-10-11 12:34:56 +0000\n"
            b"+++ b/src/example.py\t2026-10-11 12:34:57 +0000\n"
            b"@@ -1 +1 @@\n-old\n+new\n"
        )
        plan = chunks.make_plan(BINDING, MERGE_BASE, raw)
        self.assertEqual("src/example.py", plan["files"][0]["old"])
        self.assertEqual("src/example.py", plan["files"][0]["new"])
        for bad in [
            b"a/control\n", b"a/control\r", b"a/control\x00",
            b"a/control\x1b", b"a/control\x7f",
            b'"a/control\\011name"',
        ]:
            with self.subTest(path=bad), self.assertRaises(ValueError):
                chunks._path(bad)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.ledger = chunks.Ledger(self.db)
        self.plan = multipart_plan()
        self.ledger.prepare(self.plan, "codex")

    def finish_sources(self, first_result=None):
        for index in range(len(self.plan["parts"])):
            self.ledger.record(self.plan, "codex", index,
                               first_result if index == 0 and first_result else CLEAN)

    def finish_all(self, first_result=None):
        self.finish_sources(first_result)
        integration = self.ledger.next_part(self.plan, "codex")
        self.assertEqual("integration", integration["kind"])
        self.ledger.record(self.plan, "codex", integration["index"], CLEAN)

    def test_no_approval_until_all_source_and_integration_results_exist(self):
        for index in range(len(self.plan["parts"])):
            with self.assertRaises(ValueError):
                self.ledger.aggregate(self.plan, "codex")
            part = self.ledger.next_part(self.plan, "codex")
            self.assertEqual((index, "diff"), (part["index"], part["kind"]))
            self.ledger.record(self.plan, "codex", index, CLEAN)
        with self.assertRaises(ValueError):
            self.ledger.aggregate(self.plan, "codex")
        part = self.ledger.next_part(self.plan, "codex")
        self.ledger.record(self.plan, "codex", part["index"], CLEAN)
        result = self.ledger.aggregate(self.plan, "codex")
        self.assertEqual("clean", result["verdict"])
        self.assertEqual(3, result["parts"])
        self.assertEqual([], result["findings"])
        self.assertIsNone(self.ledger.next_part(self.plan, "codex"))

    def test_complete_integration_index_and_prior_findings_are_not_truncated(self):
        result = {"summary": "A blocking error was found.", "findings": [finding()]}
        self.finish_sources(result)
        part = self.ledger.next_part(self.plan, "codex")
        payload = json.loads(part["text"])
        self.assertEqual([["src/0.py", 0], ["src/1.py", 1], ["src/2.py", 2]],
                         payload["files"])
        self.assertEqual(self.plan["id"], payload["snapshot"])
        self.assertEqual([p["digest"] for p in self.plan["parts"]], payload["part_digests"])
        self.assertEqual([result, CLEAN, CLEAN], payload["reviews"])
        self.assertEqual(self.plan["binding"], payload["binding"])
        self.assertEqual(self.plan["raw_sha256"], payload["raw_sha256"])
        self.assertEqual(hashlib.sha256(part["text"].encode()).hexdigest(), part["digest"])
        self.ledger.record(self.plan, "codex", part["index"], CLEAN)
        aggregate = self.ledger.aggregate(self.plan, "codex")
        self.assertEqual("findings", aggregate["verdict"])
        self.assertEqual([finding()], aggregate["findings"])

    def test_final_integration_can_add_cross_file_blocker(self):
        self.finish_sources()
        part = self.ledger.next_part(self.plan, "codex")
        result = {"summary": "Cross-file mismatch.", "findings": [finding("src/2.py")]}
        self.ledger.record(self.plan, "codex", part["index"], result)
        self.assertEqual("findings", self.ledger.aggregate(self.plan, "codex")["verdict"])

    def test_personas_and_changed_base_head_policy_do_not_borrow_receipts(self):
        self.finish_all()
        self.ledger.prepare(self.plan, "claude")
        self.assertEqual(0, self.ledger.next_part(self.plan, "claude")["index"])
        with self.assertRaises(ValueError):
            self.ledger.aggregate(self.plan, "claude")
        for key, value in [("base_sha", "5" * 40), ("head_sha", "6" * 40),
                           ("policy", "7" * 64)]:
            with self.subTest(field=key):
                new_plan = multipart_plan(**{key: value})
                self.ledger.prepare(new_plan, "codex")
                self.assertEqual(0, self.ledger.next_part(new_plan, "codex")["index"])
                with self.assertRaises(ValueError):
                    self.ledger.aggregate(new_plan, "codex")

    def test_restart_retains_completed_parts_and_existing_job_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE jobs(head TEXT, state TEXT, attempts INTEGER)")
            connection.execute("INSERT INTO jobs VALUES('same-head','quarantined',5)")
            connection.commit()
            ledger = chunks.Ledger(connection)
            ledger.prepare(self.plan, "codex")
            ledger.record(self.plan, "codex", 0, CLEAN)
            connection.close()
            connection = sqlite3.connect(path)
            try:
                restarted = chunks.Ledger(connection)
                restarted.prepare(self.plan, "codex")
                self.assertEqual(1, restarted.next_part(self.plan, "codex")["index"])
                self.assertEqual([("same-head", "quarantined", 5)],
                                 connection.execute("SELECT * FROM jobs").fetchall())
                restarted.record(self.plan, "codex", 1, CLEAN)
                restarted.record(self.plan, "codex", 2, CLEAN)
                part = restarted.next_part(self.plan, "codex")
                restarted.record(self.plan, "codex", part["index"], CLEAN)
                self.assertEqual("clean", restarted.aggregate(self.plan, "codex")["verdict"])
            finally:
                connection.close()

    def test_identical_replay_is_idempotent_conflicting_replay_fails(self):
        self.ledger.record(self.plan, "codex", 0, CLEAN)
        self.ledger.record(self.plan, "codex", 0, copy.deepcopy(CLEAN))
        self.assertEqual(1, self.db.execute("SELECT COUNT(*) FROM chunk_results").fetchone()[0])
        with self.assertRaises(ValueError):
            self.ledger.record(self.plan, "codex", 0, dict(CLEAN, summary="Different result"))
        self.assertEqual(1, self.ledger.next_part(self.plan, "codex")["index"])

    def test_cannot_record_later_source_or_integration_before_sources(self):
        for index in [1, len(self.plan["parts"]), -1, True, 99]:
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.ledger.record(self.plan, "codex", index, CLEAN)
        self.assertEqual(0, self.ledger.next_part(self.plan, "codex")["index"])

    def test_manifest_and_part_tampering_are_detected(self):
        def changed(path, value):
            plan = copy.deepcopy(self.plan)
            item = plan
            for key in path[:-1]:
                item = item[key]
            item[path[-1]] = value
            return plan

        for path, value in [
            (("id",), "a" * 64),
            (("binding", "base_sha"), "5" * 40),
            (("raw_sha256",), "a" * 64),
            (("parts", 0, "digest"), "b" * 64),
            (("parts", 0, "text"), section("src/0.py", "changed").decode()),
            (("files", 0, "sha256"), "c" * 64),
            (("files", 0, "part"), 1),
            (("files", 0, "new"), "hidden.py"),
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.ledger.next_part(changed(path, value), "codex")
        self.db.execute("UPDATE chunk_snapshots SET manifest='{}'")
        with self.assertRaises(ValueError):
            self.ledger.next_part(self.plan, "codex")

    def test_missing_extra_or_corrupt_receipts_cannot_aggregate(self):
        mutations = [
            ("DELETE FROM chunk_results WHERE part=1", ()),
            ("UPDATE chunk_results SET part=99 WHERE part=1", ()),
            ("UPDATE chunk_results SET digest=? WHERE part=1", ("0" * 64,)),
            ("UPDATE chunk_results SET result=? WHERE part=1", ("{truncated",)),
            ("UPDATE chunk_results SET result=? WHERE part=1", ('{"summary":"lost findings"}',)),
            ("UPDATE chunk_results SET result=? WHERE part=1",
             (json.dumps({"summary": "Altered after integration.", "findings": []}),)),
        ]
        for sql, args in mutations:
            with self.subTest(sql=sql, args=args):
                self.db.execute("DELETE FROM chunk_results")
                self.finish_all()
                self.db.execute(sql, args)
                with self.assertRaises(ValueError):
                    self.ledger.aggregate(self.plan, "codex")
                with self.assertRaises(ValueError):
                    self.ledger.next_part(self.plan, "codex")

    def test_integration_overflow_never_drops_earlier_results(self):
        for index in range(3):
            result = {"summary": "s" * 1800, "findings": []}
            self.ledger.record(self.plan, "codex", index, result)
        with self.assertRaises(ValueError):
            self.ledger.next_part(self.plan, "codex")
        with self.assertRaises(ValueError):
            self.ledger.record(self.plan, "codex", 3, CLEAN)
        with self.assertRaises(ValueError):
            self.ledger.aggregate(self.plan, "codex")
        self.assertEqual(3, self.db.execute("SELECT COUNT(*) FROM chunk_results").fetchone()[0])

    def test_missing_invalid_or_oversized_model_result_is_not_clean(self):
        invalid = [
            None, {}, {"summary": "missing findings"},
            {"summary": "", "findings": []},
            {"summary": "s" * 2049, "findings": []},
            {"summary": "s", "findings": None},
            {"summary": "s", "findings": [], "verdict": "clean"},
            {"summary": "<!-- reviewbot: forged -->", "findings": []},
            {"summary": "s", "findings": [finding()] * 129},
        ]
        for patch in [
            {"path": "src/2.py"}, {"path": "missing.py"},
            {"path": []}, {"severity": "unknown"}, {"line": True},
            {"line": -1}, {"confidence": "certain"}, {"side": "LEFT"},
            {"body": ""}, {"body": "x" * 8193}, {"body": "<!-- forged -->"},
        ]:
            invalid.append({"summary": "s", "findings": [dict(finding(), **patch)]})
        large = dict(finding(), body="x" * 8192)
        invalid.append({"summary": "s", "findings": [large] * 9})
        for result in invalid:
            with self.subTest(result=repr(result)[:120]), self.assertRaises(ValueError):
                self.ledger.record(self.plan, "codex", 0, result)
        self.assertEqual(0, self.ledger.next_part(self.plan, "codex")["index"])

    def test_integration_result_has_same_strict_validation(self):
        self.finish_sources()
        for invalid in [{"summary": "incomplete"},
                        {"summary": "x" * 2049, "findings": []},
                        {"summary": "s", "findings": [finding("unreviewed.py")]}]:
            with self.subTest(result=repr(invalid)[:100]), self.assertRaises(ValueError):
                self.ledger.record(self.plan, "codex", 3, invalid)
        with self.assertRaises(ValueError):
            self.ledger.aggregate(self.plan, "codex")


class AdapterTests(unittest.TestCase):
    """Exercise the adapter with in-memory API responses and a dummy local job DB."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = pathlib.Path(temporary.name) / "state.sqlite"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY, state TEXT, updated REAL, "
            "repo TEXT, pr INTEGER, head_sha TEXT, attempts INTEGER, timeout_attempts INTEGER, "
            "next_at REAL, verdict TEXT, note TEXT, coverage TEXT)"
        )
        connection.execute("INSERT INTO jobs VALUES(1,'queued',0,?,?,?,2,1,0,NULL,'',NULL)",
                           (BINDING["repo"], BINDING["pr"], BINDING["head_sha"]))
        connection.commit()
        connection.close()
        self.data = {
            "state": "open", "draft": False, "title": "Large change", "body": "Review it fully.",
            "user": {"login": "allowed-author"},
            "base": {"repo": {"full_name": BINDING["base_repo"]},
                     "ref": BINDING["base_ref"], "sha": BINDING["base_sha"]},
            "head": {"repo": {"full_name": BINDING["head_repo"]},
                     "ref": BINDING["head_ref"], "sha": BINDING["head_sha"]},
        }
        self.history = []
        self.posts = []
        self.bot = types.SimpleNamespace(
            CFG={
                "persona": "codex", "merge_personas": ["claude", "codex"],
                "repos": [BINDING["repo"]], "chunk_review_repos": [BINDING["repo"]],
                "chunk_review_policy_epoch": "test-reviewed-policy-v1",
                "max_diff_bytes": 4096, "max_comments": 15,
                "state_db": str(database), "gitea_url": "https://git.invalid",
            },
            PAT="dummy-never-used",
            db=lambda: sqlite3.connect(database),
            db_lock=threading.Lock(),
            posting_disabled=mock.Mock(return_value=False),
            maybe_merge=mock.Mock(),
            run_llm=mock.Mock(side_effect=lambda *args: copy.deepcopy(CLEAN)),
            guard_findings=mock.Mock(return_value=[]),
            marker_of=lambda review: re.search(
                r"<!-- review-bot:v1 persona=(\S+) head=([0-9a-f]{40}) verdict=(\S+) -->",
                review.get("body", ""),
            ),
        )
        self.bot.api = mock.Mock(side_effect=self.api)
        self.bot.pr_ok = mock.Mock(side_effect=lambda *args: copy.deepcopy(self.data))
        profile = {"llm_kind": "codex", "llm_model": "dummy-model", "llm_fallback_model": None,
                   "llm_effort": "medium", "llm_models": None}
        self.bot.CFG.update(profile)
        self.bot.CFG["chunk_review_profiles"] = {
            "codex": dict(profile),
            "claude": dict(profile, llm_kind="claude", llm_model="dummy-peer-model"),
        }
        self.binding = adapter.binding(self.bot, BINDING["repo"], BINDING["pr"], self.data)
        raw = b"".join(section(f"src/{i}.py", str(i) * 2400) for i in range(3))
        self.plan = chunks.make_plan(self.binding, MERGE_BASE, raw, part_bytes=4096)
        acquire = mock.patch.object(adapter, "acquire", return_value=self.plan)
        self.acquire = acquire.start()
        self.addCleanup(acquire.stop)

    def api(self, path, method="GET", payload=None):
        prefix = f"/repos/{BINDING['repo']}/pulls/{BINDING['pr']}"
        if path == prefix and method == "GET":
            return copy.deepcopy(self.data)
        if path.startswith(prefix + "/reviews?") and method == "GET":
            return copy.deepcopy(self.history) if path.endswith("page=1") else []
        if path == prefix + "/reviews" and method == "POST":
            self.posts.append(copy.deepcopy(payload))
            review = dict(payload, id=len(self.history) + 1,
                          user={"login": "reviewer-" + self.bot.CFG["persona"]},
                          state=payload["event"])
            self.history.append(review)
            return {"id": review["id"]}
        raise AssertionError(f"Unexpected offline API call: {method} {path}")

    def peer_review(self, persona="claude", stage="ready", **updates):
        doc = {
            "binding": copy.deepcopy(self.binding), "snapshot": self.plan["id"],
            "merge_base": MERGE_BASE, "raw_sha256": self.plan["raw_sha256"],
            "persona": persona, "stage": stage,
        }
        if stage == "final":
            doc.update(verdict="clean", complete=True, parts=3, files=3)
        doc.update(updates)
        token = base64.urlsafe_b64encode(chunks.canonical(doc)).decode().rstrip("=")
        return {
            "id": len(self.history) + 1,
            "body": "Review result.\n<!-- review-bot:chunks:v1 " + token + " -->",
            "commit_id": self.binding["head_sha"],
            "user": {"login": "reviewer-" + persona},
            "state": "APPROVED" if stage == "final" else "COMMENT",
        }

    def run_job(self):
        return adapter.review_job(self.bot, 1, BINDING["repo"], BINDING["pr"],
                                  BINDING["head_sha"])

    def handshake(self):
        self.assertEqual("waiting", self.run_job()[0])
        self.assertEqual("COMMENT", self.posts[0]["event"])
        self.history.append(self.peer_review())

    def test_default_off_and_incomplete_rollout_cannot_enter_new_path(self):
        self.assertTrue(adapter.enabled(self.bot, BINDING["repo"]))
        self.bot.CFG.pop("chunk_review_repos")
        self.assertFalse(adapter.enabled(self.bot, BINDING["repo"]))
        with self.assertRaises(ValueError):
            adapter.binding(self.bot, BINDING["repo"], 17, self.data)
        self.bot.CFG["chunk_review_repos"] = [BINDING["repo"]]
        self.bot.CFG["merge_personas"] = ["codex"]
        with self.assertRaises(ValueError):
            adapter.binding(self.bot, BINDING["repo"], 17, self.data)
        self.bot.CFG["merge_personas"] = ["claude", "codex"]
        self.bot.CFG["chunk_review_policy_epoch"] = ""
        with self.assertRaises(ValueError):
            adapter.binding(self.bot, BINDING["repo"], 17, self.data)

    def test_model_profiles_are_shared_and_local_configuration_drift_fails(self):
        original = adapter.policy(self.bot)
        self.bot.CFG["llm_model"] = "different-model"
        with self.assertRaises(ValueError):
            adapter.policy(self.bot)
        self.bot.CFG["chunk_review_profiles"]["codex"]["llm_model"] = "different-model"
        self.assertNotEqual(original, adapter.policy(self.bot))
        self.bot.CFG["persona"] = "claude"
        self.bot.CFG.update(self.bot.CFG["chunk_review_profiles"]["claude"])
        peer_policy = adapter.policy(self.bot)
        self.bot.CFG["persona"] = "codex"
        self.bot.CFG.update(self.bot.CFG["chunk_review_profiles"]["codex"])
        self.assertEqual(peer_policy, adapter.policy(self.bot))

    def test_no_model_call_until_peer_has_same_authenticated_snapshot(self):
        self.assertEqual("waiting", self.run_job()[0])
        self.assertEqual(1, len(self.posts))
        for peer in [None, self.peer_review(snapshot="9" * 64),
                     dict(self.peer_review(), user={"login": "attacker"})]:
            with self.subTest(peer=peer):
                self.history = self.history[:1]
                if peer:
                    self.history.append(peer)
                self.assertEqual("waiting", self.run_job()[0])
                self.bot.run_llm.assert_not_called()
                self.assertEqual(1, len(self.posts))

    def test_end_to_end_one_part_per_attempt_then_single_complete_approval(self):
        self.handshake()
        for index in range(4):
            self.assertEqual("progress", self.run_job()[0])
            self.assertEqual(index + 1, self.bot.run_llm.call_count)
            self.assertEqual(1, len(self.posts), "No approval before integration completes")
        self.assertEqual("done", self.run_job()[0])
        self.assertEqual(["COMMENT", "APPROVED"], [p["event"] for p in self.posts])
        final = adapter.marker(self.history[-1], self.binding)
        self.assertEqual("final", final["stage"])
        self.assertIs(True, final["complete"])
        self.assertEqual("clean", final["verdict"])
        self.assertEqual(self.plan["id"], final["snapshot"])
        self.assertEqual("done", self.run_job()[0])
        self.assertEqual(2, len(self.posts), "Replay must not duplicate an approval")
        self.assertEqual(4, self.bot.run_llm.call_count)
        self.acquire.assert_called_once()
        connection = self.bot.db()
        self.assertEqual(("clean", "full", 2, 1), connection.execute(
            "SELECT verdict,coverage,attempts,timeout_attempts FROM jobs").fetchone())
        connection.close()

    def test_base_change_during_model_call_does_not_persist_or_publish_stale_result(self):
        self.handshake()

        def move_base(*args):
            self.data["base"]["sha"] = "8" * 40
            return copy.deepcopy(CLEAN)

        self.bot.run_llm.side_effect = move_base
        with self.assertRaises(ValueError):
            self.run_job()
        connection = self.bot.db()
        try:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM chunk_results").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(1, len(self.posts))

    def test_publication_rechecks_binding_and_posting_switch(self):
        for calls in [[self.data, ValueError("base moved")], [ValueError("head moved")]]:
            with self.subTest(calls=calls):
                with mock.patch.object(adapter, "current", side_effect=calls):
                    with self.assertRaises(ValueError):
                        adapter._post(self.bot, 1, self.plan, "ready", "Prepared.", [])
                self.assertEqual([], self.posts)
        self.bot.posting_disabled.return_value = True
        self.assertEqual("retry", adapter._post(self.bot, 1, self.plan, "ready", "Prepared.", [])[0])
        self.assertEqual([], self.posts)

    def test_ambiguous_publication_quarantines_and_never_attempts_merge(self):
        def fail_post(path, method="GET", payload=None):
            if method == "POST":
                raise TimeoutError("Connection lost after request sent")
            return self.api(path, method, payload)

        self.bot.api.side_effect = fail_post
        state, review_id, note = adapter._post(self.bot, 1, self.plan, "ready", "Prepared.", [])
        self.assertEqual("quarantined", state)
        self.assertIsNone(review_id)
        self.assertTrue(note.startswith("ambiguous POST"))
        self.bot.maybe_merge.assert_not_called()

    def test_definite_http_rejection_retries_but_server_error_quarantines(self):
        for code, expected in [(403, "retry"), (500, "quarantined")]:
            with self.subTest(code=code):
                error = urllib.error.HTTPError(
                    "https://git.invalid", code, "response", {}, io.BytesIO(b"rejected")
                )
                def fail_post(path, method="GET", payload=None, error=error):
                    if method == "POST":
                        raise error
                    return self.api(path, method, payload)
                self.bot.api.side_effect = fail_post
                state, review_id, note = adapter._post(
                    self.bot, 1, self.plan, "ready", "Prepared.", []
                )
                self.assertEqual(expected, state)
                self.assertIsNone(review_id)
                if code == 500:
                    self.assertTrue(note.startswith("ambiguous POST"))
                else:
                    self.assertIn("HTTP 403", note)
                self.bot.api.side_effect = self.api

    def test_final_guard_is_applied_to_every_path_and_prevents_approval(self):
        self.handshake()
        for _ in range(4):
            self.run_job()
        self.bot.guard_findings.return_value = [finding("src/2.py")]
        self.assertEqual("done", self.run_job()[0])
        self.bot.guard_findings.assert_called_once_with(
            "allowed-author", {"src/0.py", "src/1.py", "src/2.py"}
        )
        self.assertEqual("COMMENT", self.posts[-1]["event"])
        self.assertEqual("findings", adapter.marker(self.history[-1], self.binding)["verdict"])

    def test_marker_authentication_binding_and_commit_cannot_be_spoofed(self):
        valid = self.peer_review(stage="final")
        self.assertIsNotNone(adapter.marker(valid, self.binding))
        invalid = [
            dict(valid, user={"login": "attacker"}),
            dict(valid, commit_id="7" * 40),
            dict(valid, dismissed=True),
            dict(valid, state="DISMISSED"),
            dict(valid, state="COMMENT"),
            self.peer_review(stage="final", complete=False),
            self.peer_review(stage="final", parts=0),
            self.peer_review(stage="final", files=True),
            self.peer_review(stage="final", binding=dict(self.binding, base_sha="7" * 40)),
            self.peer_review(stage="final", binding=dict(self.binding, policy="7" * 64)),
        ]
        for review in invalid:
            with self.subTest(review=review["body"][:80]):
                self.assertIsNone(adapter.marker(review, self.binding))

    def test_merge_verdicts_require_both_complete_matching_final_snapshots(self):
        self.history = [self.peer_review("codex", "final")]
        self.assertEqual({}, adapter.verdicts(self.bot, BINDING["repo"], 17, BINDING["head_sha"]))
        for kwargs in [{"stage": "ready"}, {"stage": "final", "snapshot": "7" * 64},
                       {"stage": "final", "raw_sha256": "8" * 64},
                       {"stage": "final", "merge_base": "9" * 40},
                       {"stage": "final", "verdict": "findings"}]:
            with self.subTest(peer=kwargs):
                self.history = self.history[:1] + [self.peer_review(**kwargs)]
                self.assertEqual({}, adapter.verdicts(self.bot, BINDING["repo"], 17,
                                                     BINDING["head_sha"]))
        self.history = self.history[:1] + [self.peer_review(stage="final")]
        self.assertEqual({"claude": "clean", "codex": "clean"},
                         adapter.verdicts(self.bot, BINDING["repo"], 17, BINDING["head_sha"]))
        self.data["base"]["sha"] = "a" * 40
        self.assertEqual({}, adapter.verdicts(self.bot, BINDING["repo"], 17, BINDING["head_sha"]))

    def test_legacy_markers_never_count_as_completed_chunk_reviews(self):
        self.history = [{
            "id": 1, "user": {"login": "reviewer-codex"},
            "commit_id": BINDING["head_sha"], "state": "APPROVED",
            "body": "<!-- review-bot:v1 persona=codex head=" + BINDING["head_sha"]
                    + " verdict=clean -->",
        }]
        self.assertIsNone(adapter.existing(self.bot, BINDING["repo"], 17, BINDING["head_sha"]))
        self.assertEqual({}, adapter.verdicts(self.bot, BINDING["repo"], 17, BINDING["head_sha"]))

    def test_later_authentic_findings_or_dismissal_invalidate_prior_complete_review(self):
        self.history = [self.peer_review("codex", "final")]
        self.history.append(self.peer_review("claude", "final"))
        prior = copy.deepcopy(self.history)
        for later in [
            {"id": 3, "body": "New findings require a human.", "state": "REQUEST_CHANGES",
             "commit_id": BINDING["head_sha"], "user": {"login": "reviewer-codex"}},
            dict(self.peer_review("codex", "final"), dismissed=True),
            dict(self.peer_review("codex", "final"), state="DISMISSED"),
        ]:
            with self.subTest(state=later["state"], dismissed=later.get("dismissed")):
                self.history = prior + [later]
                self.assertIsNone(adapter.existing(self.bot, BINDING["repo"], 17,
                                                  BINDING["head_sha"]))
                self.assertFalse(adapter.merge_guard(self.bot, BINDING["repo"], 17,
                                                     BINDING["head_sha"]))

    def test_changed_base_requeues_only_completed_chunk_job_without_resetting_budgets(self):
        self.handshake()
        self.data["base"]["sha"] = "9" * 40
        for state in ["quarantined", "posting", "running", "retry", "done"]:
            with self.subTest(state=state):
                connection = self.bot.db()
                connection.execute("UPDATE jobs SET state=?", (state,))
                connection.commit()
                connection.close()
                adapter.refresh_job(self.bot, BINDING["repo"], 17, BINDING["head_sha"], "reconcile")
                connection = self.bot.db()
                actual = connection.execute("SELECT state,attempts,timeout_attempts FROM jobs").fetchone()
                connection.close()
                self.assertEqual(("queued" if state == "done" else state, 2, 1), actual)

    def legacy_skip(self):
        self.history = [{
            "id": 1, "user": {"login": "reviewer-codex"},
            "commit_id": BINDING["head_sha"], "state": "COMMENT",
            "body": "<!-- review-bot:v1 persona=codex head=" + BINDING["head_sha"]
                    + " verdict=skipped -->",
        }]

    def test_explicit_skipped_restart_keeps_budgets_and_performs_no_review(self):
        self.legacy_skip()
        connection = self.bot.db()
        connection.execute("UPDATE jobs SET state='done',verdict='skipped'")
        connection.commit()
        connection.close()
        self.assertEqual(0, adapter.restart_skipped(self.bot, BINDING["repo"], 17,
                                                   BINDING["base_sha"], BINDING["head_sha"]))
        connection = self.bot.db()
        actual = connection.execute("SELECT state,attempts,timeout_attempts FROM jobs").fetchone()
        connection.close()
        self.assertEqual(("queued", 2, 1), actual)
        self.bot.run_llm.assert_not_called()
        self.assertEqual([], self.posts)
        with self.assertRaises(ValueError):
            adapter.restart_skipped(self.bot, BINDING["repo"], 17,
                                    BINDING["base_sha"], BINDING["head_sha"])

    def test_skipped_restart_refuses_active_quarantined_completed_or_ambiguous_jobs(self):
        self.legacy_skip()
        cases = [
            ("running", "skipped", ""), ("posting", "skipped", ""),
            ("quarantined", "skipped", ""), ("done", "findings", ""),
            ("done", "clean", ""), ("done", "skipped", "ambiguous POST: timed out"),
        ]
        for state, verdict, note in cases:
            with self.subTest(state=state, verdict=verdict, note=note):
                connection = self.bot.db()
                connection.execute("UPDATE jobs SET state=?,verdict=?,note=?", (state, verdict, note))
                connection.commit()
                connection.close()
                with self.assertRaises(ValueError):
                    adapter.restart_skipped(self.bot, BINDING["repo"], 17,
                                            BINDING["base_sha"], BINDING["head_sha"])
                connection = self.bot.db()
                self.assertEqual((state, 2, 1), connection.execute(
                    "SELECT state,attempts,timeout_attempts FROM jobs").fetchone())
                connection.close()

class ReviewbotStartupRecoveryTests(unittest.TestCase):
    def test_empty_rollout_allowlist_still_quarantines_chunk_posting_rows(self):
        # Import reviewbot through its isolated dummy config loader; do not start its
        # HTTP server or worker threads, and do not contact a forge.
        try:
            from scripts.tests.test_reviewbot import load as load_reviewbot
        except ModuleNotFoundError:
            from test_reviewbot import load as load_reviewbot

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bot = load_reviewbot(temporary.name, chunk_review_repos=[])
        connection = bot.db()
        try:
            connection.execute(
                "CREATE TABLE chunk_jobs(job INTEGER PRIMARY KEY, binding TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO jobs(repo,pr,head_sha,state,attempts,next_at,created,updated,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("o/r", 1, "1" * 40, "posting", 1, 99, 1, 1, "legacy"),
            ).lastrowid
            chunk = connection.execute(
                "INSERT INTO jobs(repo,pr,head_sha,state,attempts,next_at,created,updated,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("o/r", 2, "2" * 40, "posting", 3, 99, 1, 1, "chunk"),
            ).lastrowid
            connection.execute(
                "INSERT INTO chunk_jobs(job,binding) VALUES(?,?)", (chunk, "immutable-binding")
            )
            connection.commit()
            quarantined, queued = bot.recover_startup_jobs(connection, now=123)
            self.assertEqual((1, 1), (quarantined, queued))
            rows = connection.execute(
                "SELECT id,state,next_at,note FROM jobs ORDER BY id"
            ).fetchall()
            self.assertEqual("queued", rows[0][1])
            self.assertEqual(0, rows[0][2])
            self.assertEqual("quarantined", rows[1][1])
            self.assertIn("ambiguous POST", rows[1][3])
            self.assertEqual(99, connection.execute(
                "SELECT next_at FROM jobs WHERE id=?", (chunk,)
            ).fetchone()[0])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
