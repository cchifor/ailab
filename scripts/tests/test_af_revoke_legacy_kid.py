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
import pathlib
import subprocess
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


class Wrapper(unittest.TestCase):
    SCRIPT = "scripts/af-revoke-legacy-kid.sh"

    def test_refuses_an_aud_the_kid_was_never_granted_on(self) -> None:
        for aud in ("anthropic/claude-max-3", "openai/other", "x"):
            with self.subTest(aud=aud):
                proc = subprocess.run(["bash", self.SCRIPT, aud], cwd=REPO, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("refusing aud", proc.stderr)

    def test_pins_the_retired_and_surviving_identities(self) -> None:
        text = (REPO / self.SCRIPT).read_text(encoding="utf-8")
        for needle in ("--kid tz-213512e80fde7c84", "--expect-workspace playground",
                       "--keep-kid cap-b5663179affeeaba5366c3ffd232681d", "--keep-pk db3a6d0ec8e1d6a9",
                       "--expect-pk b864a0136bbc8d39"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
