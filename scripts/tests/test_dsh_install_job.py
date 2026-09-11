#!/usr/bin/env python3
"""Wiring gates for the dsh install Job's profile-plugin staging step.

WHY THIS EXISTS. The staging step is what puts the delegation providers
(DSH_PLUGINS, e.g. dsh-subagent-codex) into the profile closure. It used to run
`corepack enable pnpm` and then a bare `pnpm add`. As uid 1000, `corepack
enable` cannot write its shims into /usr/local/bin (verified in docker on both
node:22 and node:24), so the bare `pnpm` only ever resolved where a shim already
happened to exist -- and the node:24 image ships none. The 2026-09-11 Job run
ended with `/bin/sh: pnpm: not found`, staging reported `failed`, and dsh booted
without the codex provider: a silent feature loss on every pod roll after that.

The fix runs a PINNED pnpm through corepack directly, with corepack's download
cache on the app volume. This test pins that shape so a future edit cannot
quietly reintroduce the shim dependency, and so the pnpm version is an explicit,
reviewable pin rather than whatever corepack considers "last known good" today.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
JOB = ROOT / "kubernetes" / "apps" / "apps" / "dsh" / "install-job.yaml"


class StagingInvocation(unittest.TestCase):
    def setUp(self):
        self.text = JOB.read_text(encoding="utf-8")
        # The shell lives inside a YAML string; judge its CODE, not the comments
        # that explain it (which legitimately name the anti-patterns).
        self.code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )

    def test_no_corepack_enable_and_no_bare_pnpm(self):
        # `corepack enable` needs a writable /usr/local/bin, which uid 1000 does
        # not have; a bare `pnpm` then depends on a shim the image may not ship.
        self.assertNotIn("corepack enable", self.code)
        self.assertIsNone(
            re.search(r"(?<![\w@/.-])pnpm add ", self.code),
            "a bare `pnpm add` depends on a shim; invoke pnpm through corepack with a pin",
        )

    def test_pinned_pnpm_through_corepack(self):
        m = re.search(r'corepack "\$PNPM" add', self.code)
        self.assertIsNotNone(m, "staging must run `corepack \"$PNPM\" add ...`")
        pin = re.search(r'^\s*PNPM=pnpm@(\d+\.\d+\.\d+)\s*$', self.code, re.M)
        self.assertIsNotNone(pin, "PNPM must be pinned to an exact pnpm@X.Y.Z")
        major, minor = int(pin.group(1).split(".")[0]), int(pin.group(1).split(".")[1])
        # pnpm-workspace.yaml copied from the scratch profile carries
        # minimumReleaseAgeExclude, which landed in pnpm 10.16.
        self.assertTrue((major, minor) >= (10, 16), f"pnpm {pin.group(1)} predates minimumReleaseAgeExclude")

    def test_corepack_cache_on_the_app_volume_and_no_prompt(self):
        # The Job's root filesystem is not the place for a download cache, and
        # a re-run must not re-download; /app is the writable, persistent volume.
        self.assertIn("COREPACK_HOME=/app/.corepack", self.code)
        self.assertIn("COREPACK_ENABLE_DOWNLOAD_PROMPT=0", self.code)


if __name__ == "__main__":
    unittest.main()
