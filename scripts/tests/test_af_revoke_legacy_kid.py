#!/usr/bin/env python3
"""Pre-checks of scripts/af_revoke_legacy_kid.py, the Phase 3 driver of the playground retirement
(plans/2026-10-30-retire-playground-plan.md).

The removal itself is agentforge's own `_cas_remove_from_registry`, tested in that repo. What this repo
adds is the gate in front of it: the retired kid must be exactly the expected identity, and the kid
that must survive (platform-dev's) must be present with its expected key, or nothing is written. Plus
the wrapper's refusal of any aud the retired kid was never granted on. Stdlib-only, like every module
in scripts/tests.

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""
from __future__ import annotations

import importlib.util
import io
import logging
import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("af_revoke_legacy_kid", REPO / "scripts" / "af_revoke_legacy_kid.py")
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

OLD_PEM = "-----BEGIN PUBLIC KEY-----\nretired\n-----END PUBLIC KEY-----"
KEEP_PEM = "-----BEGIN PUBLIC KEY-----\nsurvivor\n-----END PUBLIC KEY-----"
OLD = {"public_key": OLD_PEM, "allowed_tenants": ["tenant-zero"], "allowed_workspaces": ["playground"]}
KEEP = {"public_key": KEEP_PEM, "allowed_tenants": ["tenant-zero"], "allowed_workspaces": ["platform-dev"]}


def checks(**over):
    base = dict(
        kid="tz-old",
        expect_pk=mod.pk_sha256(OLD)[:16],
        expect_tenants=["tenant-zero"],
        expect_workspaces=["playground"],
        keep_kid="cap-keep",
        keep_pk=mod.pk_sha256(KEEP)[:16],
    )
    base.update(over)
    return base


class Precheck(unittest.TestCase):
    def test_matching_record_is_returned(self) -> None:
        got = mod.precheck({"kids": {"tz-old": OLD, "cap-keep": KEEP}}, **checks())
        self.assertEqual(got, OLD)

    def test_already_absent_is_none_not_an_error(self) -> None:
        self.assertIsNone(mod.precheck({"kids": {"cap-keep": KEEP}}, **checks()))

    def test_fingerprint_ignores_surrounding_whitespace(self) -> None:
        padded = dict(OLD, public_key="\n" + OLD_PEM + "\n")
        self.assertEqual(mod.pk_sha256(padded), mod.pk_sha256(OLD))

    def test_mismatches_are_hard_stops(self) -> None:
        cases = {
            "other public key": ({"tz-old": dict(OLD, public_key=KEEP_PEM), "cap-keep": KEEP}, {}),
            "widened workspaces": ({"tz-old": dict(OLD, allowed_workspaces=["playground", "x"]), "cap-keep": KEEP}, {}),
            "other tenant": ({"tz-old": dict(OLD, allowed_tenants=["other"]), "cap-keep": KEEP}, {}),
            "survivor missing": ({"tz-old": OLD}, {}),
            "survivor rekeyed": ({"tz-old": OLD, "cap-keep": dict(KEEP, public_key=OLD_PEM)}, {}),
            "entry not an object": ({"tz-old": "x", "cap-keep": KEEP}, {}),
            "short fingerprint": ({"tz-old": OLD, "cap-keep": KEEP}, {"expect_pk": "b864a0"}),
        }
        for name, (kids, over) in cases.items():
            with self.subTest(name), self.assertRaises(mod.PrecheckError):
                mod.precheck({"kids": kids}, **checks(**over))

    def test_registry_without_kids_object_is_refused(self) -> None:
        for reg in ({}, {"kids": []}):
            with self.subTest(reg=reg), self.assertRaises(mod.PrecheckError):
                mod.precheck(reg, **checks())


class AuditLogging(unittest.TestCase):
    """_cas_remove_from_registry emits its `revoked` record on agentforge.provisioner.audit at INFO,
    which Python's default WARNING level drops; the driver must route it somewhere that is kept."""

    def tearDown(self) -> None:
        log = logging.getLogger(mod.AUDIT_LOGGER)
        for h in list(log.handlers):
            log.removeHandler(h)
            h.close()

    def test_info_audit_record_reaches_the_stream_and_the_extra_sink(self) -> None:
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            extra = str(pathlib.Path(d) / "pid1-stdout")
            sinks = mod.configure_audit_logging(stream, extra_paths=(extra,))
            logging.getLogger(mod.AUDIT_LOGGER).info("provisioner.audit {\"outcome\":\"revoked\"}")
            for h in logging.getLogger(mod.AUDIT_LOGGER).handlers:
                h.flush()
            self.assertEqual(sinks, ["stream", extra])
            self.assertIn('"outcome":"revoked"', stream.getvalue())
            self.assertIn('"outcome":"revoked"', pathlib.Path(extra).read_text(encoding="utf-8"))
            self.tearDown()

    def test_an_unopenable_extra_sink_is_skipped_not_fatal(self) -> None:
        stream = io.StringIO()
        sinks = mod.configure_audit_logging(stream, extra_paths=("/nonexistent-dir/x/y",))
        self.assertEqual(sinks, ["stream"])


FAKE_KUBECTL = """#!/usr/bin/env bash
# Stands in for `kubectl exec ... -- python -u -`: swallows the driver on stdin, then prints what the
# driver would. FAKE_MODE=fail exits 7 after the audit line; anything else hangs, to be interrupted.
cat > /dev/null
for a in "$@"; do [ "$a" = --execute ] && execute=1; done
echo 'ROLLBACK {"aud": "anthropic/claude-max-1", "version": 1, "kid": "tz-213512e80fde7c84", "record": {"public_key": "PEM"}}'
if [ -z "${execute:-}" ]; then echo "anthropic/claude-max-1: DRY RUN"; exit 0; fi
echo 'INFO agentforge.provisioner.audit provisioner.audit {"outcome":"revoked"}'
[ "${FAKE_MODE:-hang}" = fail ] && exit 7
sleep 60
"""


@unittest.skipUnless(hasattr(os, "killpg"), "needs POSIX process groups and bash")
class WrapperKeepsTheExecuteRecord(unittest.TestCase):
    """The execute pass is streamed into its log as it arrives, so a failed or interrupted session
    still leaves the audit line of a removal that already happened."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.fake = d / "kubectl"
        self.fake.write_text(FAKE_KUBECTL, encoding="utf-8")
        self.fake.chmod(0o755)
        self.rollback = d / "rollback"
        self.env = dict(os.environ, AF_REVOKE_KUBECTL=str(self.fake), AF_REVOKE_ROLLBACK_DIR=str(self.rollback))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _log(self) -> str:
        logs = list(self.rollback.glob("*.execute.log"))
        return logs[0].read_text(encoding="utf-8") if logs else ""

    def test_a_failed_execute_keeps_its_audit_line_and_exit_status(self) -> None:
        proc = subprocess.run(["bash", Wrapper.SCRIPT, "anthropic/claude-max-1", "--execute"], cwd=REPO,
                              env=dict(self.env, FAKE_MODE="fail"), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertIn('"outcome":"revoked"', self._log())
        self.assertEqual(len(list(self.rollback.glob("*.rollback.json"))), 1)

    def test_an_interrupted_execute_keeps_the_lines_already_seen(self) -> None:
        proc = subprocess.Popen(["bash", Wrapper.SCRIPT, "anthropic/claude-max-1", "--execute"], cwd=REPO,
                                env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while '"outcome":"revoked"' not in self._log() and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        self.assertIn('"outcome":"revoked"', self._log(), "the audit line was lost on interruption")


class Wrapper(unittest.TestCase):
    SCRIPT = "scripts/af-revoke-legacy-kid.sh"

    def test_refuses_an_aud_the_kid_was_never_granted_on(self) -> None:
        for aud in ("anthropic/claude-max-3", "openai/other", "x"):
            with self.subTest(aud=aud):
                proc = subprocess.run(["bash", self.SCRIPT, aud], cwd=REPO, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("refusing aud", proc.stderr)

    def test_rejects_unknown_arguments(self) -> None:
        proc = subprocess.run(["bash", self.SCRIPT, "anthropic/claude-max-1", "--exec"], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unknown argument", proc.stderr)

    def test_saves_and_verifies_the_rollback_record_before_executing(self) -> None:
        text = (REPO / self.SCRIPT).read_text(encoding="utf-8")
        verify = text.index("did not verify; nothing was written")
        execute = text.index("run --execute")
        self.assertLess(verify, execute, "the rollback record must be verified before the execute pass")

    def test_pins_the_retired_and_surviving_identities(self) -> None:
        text = (REPO / self.SCRIPT).read_text(encoding="utf-8")
        for needle in ("--kid tz-213512e80fde7c84", "--expect-workspace playground",
                       "--keep-kid cap-b5663179affeeaba5366c3ffd232681d", "--keep-pk db3a6d0ec8e1d6a9",
                       "--expect-pk b864a0136bbc8d39"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
