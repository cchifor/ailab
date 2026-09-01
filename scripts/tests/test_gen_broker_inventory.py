#!/usr/bin/env python3
"""Unit tests for the seed-coverage record in scripts/gen-broker-inventory.py.

The subject is the part of that script CI cannot derive for itself: the KEY-NAMES-ONLY record of
what the SOPS-encrypted operator seeds document declares, and the `seedsDocumentSha256` stamp that
ties that record to the document it describes. NOTHING here decrypts anything — the fixture seeds
document is opaque bytes, exactly as CI sees the real one. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

Every test runs against a SANDBOX COPY of the real repo files, so the fixtures cannot drift from
the manifests the script actually parses, and no test can write into the working tree.

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "gen-broker-inventory.py"
_spec = importlib.util.spec_from_file_location("gen_broker_inventory", _MOD_PATH)
gbi = importlib.util.module_from_spec(_spec)
# Registered BEFORE exec: @dataclass resolves annotations via sys.modules[cls.__module__].
sys.modules["gen_broker_inventory"] = gbi
_spec.loader.exec_module(gbi)  # must NOT perform any I/O at import time

REAL_REPO = pathlib.Path(gbi.REPO)

#: Asserted absent from every generated artefact and every line of output.
VALUE_SENTINEL = "SENTINEL_VALUE_ab12cd34ef56_do_not_leak"

#: A plausible seeds document: the four broker oauth paths plus the non-broker paths that make
#: `declaredSeedPaths` worth recording at all (they are invisible to `declaredOauthPaths`).
SEED_PATHS = [
    "operator/broker/anthropic/claude-max-1/oauth",
    "operator/broker/anthropic/claude-max-2/oauth",
    "operator/broker/anthropic/claude-max-3/oauth",
    "operator/broker/openai/codex-pro/oauth",
    "operator/broker/anthropic/claude-max-1/kids",
    "operator/estate/gitea/bot-tokens",
    "operator/tenant/platform-dev/orchestrator",
]


def seeds_json(paths=None):
    """A seeds document whose VALUES all carry the sentinel — nothing may echo them."""
    return json.dumps({p: {"token": VALUE_SENTINEL} for p in (paths or SEED_PATHS)})


class Sandbox:
    """A throwaway copy of the real files the script reads, with the module repointed at it."""

    _COPIED = ("CP_DEPLOY", "PROVISIONER_DEPLOY", "OPERATOR_SEEDS", "INVENTORY")

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self._saved = {name: getattr(gbi, name) for name in ("REPO", "BROKER_DIR", *self._COPIED)}

        for name in self._COPIED:
            src = pathlib.Path(self._saved[name])
            self._place(src)
        for src in sorted(pathlib.Path(self._saved["BROKER_DIR"]).glob("broker-*.yaml")):
            self._place(src)

        gbi.REPO = self.root
        gbi.BROKER_DIR = self._mirror(pathlib.Path(self._saved["BROKER_DIR"]))
        for name in self._COPIED:
            setattr(gbi, name, self._mirror(pathlib.Path(self._saved[name])))
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(gbi, name, value)
        self._tmp.cleanup()
        return False

    def _mirror(self, src: pathlib.Path) -> pathlib.Path:
        return self.root / src.relative_to(REAL_REPO)

    def _place(self, src: pathlib.Path) -> None:
        dst = self._mirror(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    # -- driving the script ------------------------------------------------

    def seats(self):
        return gbi.load_seats()

    def refresh(self, paths=None, recorded_at="2026-09-01"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gbi.cmd_refresh_seed_coverage(self.seats(), seeds_json(paths), recorded_at)
        return rc, out.getvalue()

    def check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gbi.cmd_check(self.seats())
        return rc, out.getvalue()

    def write(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gbi.cmd_write(self.seats())
        return rc, out.getvalue()

    # -- poking the fixtures -----------------------------------------------

    def inventory_text(self) -> str:
        return gbi.INVENTORY.read_text(encoding="utf-8")

    def touch_seeds(self) -> None:
        """The smallest possible edit to the encrypted document: one trailing byte."""
        gbi.OPERATOR_SEEDS.write_bytes(gbi.OPERATOR_SEEDS.read_bytes() + b"\n")


class DigestIsOverCiphertext(unittest.TestCase):
    def test_hashes_the_file_bytes_verbatim(self):
        with Sandbox() as box:
            expected = hashlib.sha256(gbi.OPERATOR_SEEDS.read_bytes()).hexdigest()
            self.assertEqual(gbi.seeds_document_sha256(), expected)

    def test_one_changed_byte_changes_the_digest(self):
        with Sandbox() as box:
            before = gbi.seeds_document_sha256()
            box.touch_seeds()
            self.assertNotEqual(gbi.seeds_document_sha256(), before)

    def test_missing_seeds_document_fails_closed(self):
        with Sandbox():
            gbi.OPERATOR_SEEDS.unlink()
            with self.assertRaises(gbi.SourceError):
                gbi.seeds_document_sha256()

    def test_real_repo_document_is_hashable_without_decryption(self):
        # The gate must be computable in CI, which holds no age key. No sandbox: the real file.
        self.assertRegex(gbi.seeds_document_sha256(), r"^[0-9a-f]{64}$")


class CheckGate(unittest.TestCase):
    def test_check_passes_right_after_a_refresh(self):
        with Sandbox() as box:
            self.assertEqual(box.refresh()[0], 0)
            rc, out = box.check()
            self.assertEqual(rc, 0, out)

    def test_check_fails_when_the_seeds_document_changes(self):
        with Sandbox() as box:
            box.refresh()
            box.touch_seeds()
            rc, out = box.check()
            self.assertEqual(rc, 1)
            self.assertIn("seedsDocumentSha256 is STALE", out)
            self.assertIn("--refresh-seed-coverage", out)

    def test_check_fails_when_the_stamp_was_never_taken(self):
        with Sandbox() as box:
            box.refresh()
            text = box.inventory_text()
            digest = gbi.seeds_document_sha256()
            gbi.INVENTORY.write_text(text.replace(digest, ""), encoding="utf-8", newline="")
            rc, out = box.check()
            self.assertEqual(rc, 1)
            self.assertIn("seedsDocumentSha256 is EMPTY", out)
            self.assertIn("--refresh-seed-coverage", out)

    def test_write_does_not_re_bless_a_changed_document(self):
        # --write regenerates derived artefacts; it must NOT re-stamp, or the generator itself
        # would launder a seeds change nobody re-read.
        with Sandbox() as box:
            box.refresh()
            stamped = gbi.seeds_document_sha256()
            box.touch_seeds()
            box.write()
            self.assertIn(f'seedsDocumentSha256: "{stamped}"', box.inventory_text())
            self.assertEqual(box.check()[0], 1)

    def test_rendered_inventory_is_byte_stable_across_a_reread(self):
        # The record round-trips through the file: refresh, then render again from what was read.
        with Sandbox() as box:
            box.refresh()
            first = box.inventory_text()
            rendered = gbi.render_inventory(box.seats(), gbi._read_seed_coverage())
            self.assertEqual(rendered, first)


class DeclaredPaths(unittest.TestCase):
    def test_records_every_top_level_path_not_only_the_oauth_ones(self):
        with Sandbox() as box:
            box.refresh()
            record = gbi._read_seed_coverage()
            self.assertEqual(sorted(record.seed_paths), sorted(SEED_PATHS))
            self.assertIn("operator/estate/gitea/bot-tokens", record.seed_paths)

    def test_a_dropped_path_shows_up_as_a_removed_plaintext_line(self):
        with Sandbox() as box:
            box.refresh()
            before = box.inventory_text()
            dropped = "operator/estate/gitea/bot-tokens"
            box.refresh(paths=[p for p in SEED_PATHS if p != dropped])
            after = box.inventory_text()
            self.assertIn(dropped, before)
            self.assertNotIn(dropped, after)

    def test_oauth_paths_still_drive_the_kv_gc_gate(self):
        with Sandbox() as box:
            box.refresh(paths=[p for p in SEED_PATHS if not p.endswith("/oauth")])
            record = gbi._read_seed_coverage()
            self.assertEqual(record.oauth_paths, [])
            original = gbi.kv_gc_enabled
            gbi.kv_gc_enabled = lambda: True
            try:
                failures = gbi._coverage_gate(box.seats())
            finally:
                gbi.kv_gc_enabled = original
            self.assertTrue(any("KV garbage collector" in f for f in failures), failures)

    def test_oauth_subset_is_unchanged_by_the_wider_record(self):
        with Sandbox() as box:
            box.refresh()
            record = gbi._read_seed_coverage()
            self.assertEqual(
                sorted(record.oauth_paths),
                sorted(p for p in SEED_PATHS if p.endswith("/oauth")),
            )
            self.assertIn("  kvGcReady: true", box.inventory_text())

    def test_a_non_path_key_is_refused_without_echoing_it(self):
        bad_keys = {
            # would forge a second seedCoverage block if it were ever written out
            "newline": "SENTINEL_KEY_9f8e7d\nseedCoverage: {}",
            # would read back TRUNCATED at the space, making the record oscillate
            "space": "SENTINEL_KEY_9f8e7d has a space",
            "unbounded": "SENTINEL_KEY_9f8e7d" + "x" * 500,
        }
        for label, bad_key in bad_keys.items():
            with self.subTest(label), Sandbox() as box:
                out = io.StringIO()
                payload = json.dumps({bad_key: {"token": VALUE_SENTINEL}})
                with contextlib.redirect_stdout(out):
                    rc = gbi.cmd_refresh_seed_coverage(box.seats(), payload, "2026-09-01")
                self.assertEqual(rc, 1)
                self.assertNotIn(VALUE_SENTINEL, out.getvalue())
                self.assertNotIn("SENTINEL_KEY_9f8e7d", out.getvalue())
                self.assertNotIn("SENTINEL_KEY_9f8e7d", box.inventory_text())


class NoValueEverLeaks(unittest.TestCase):
    def test_no_seed_value_reaches_the_inventory_or_stdout(self):
        with Sandbox() as box:
            rc, out = box.refresh()
            self.assertEqual(rc, 0)
            self.assertNotIn(VALUE_SENTINEL, out)
            self.assertNotIn(VALUE_SENTINEL, box.inventory_text())
            self.assertNotIn(VALUE_SENTINEL, box.check()[1])

    def test_invalid_stdin_is_reported_without_the_payload(self):
        with Sandbox() as box:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gbi.cmd_refresh_seed_coverage(box.seats(), "not json " + VALUE_SENTINEL, "")
            self.assertEqual(rc, 1)
            self.assertNotIn(VALUE_SENTINEL, out.getvalue())

    def test_empty_stdin_fails_closed(self):
        with Sandbox() as box:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = gbi.cmd_refresh_seed_coverage(box.seats(), "   \n", "")
            self.assertEqual(rc, 1)
            self.assertIn("fail closed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
