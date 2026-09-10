#!/usr/bin/env python3
"""Behaviour and wiring gates for the dsh OpenBao credentials provider.

WHY THIS EXISTS. openbao-credentials.mjs sits in the credential path of a
1-replica Recreate Deployment and REPLACES dsh's shipped credentials service.
Two failure shapes matter and neither is visible to `kustomize build` or
`kubeconform`:

  * BEHAVIOUR. The provider must distinguish "OpenBao has no such credential"
    (fall through to the lower layer) from "I could not read the mount" (fail
    the operation). Getting that backwards silently resolves a stale local
    value while OpenBao holds a rotated one -- the exact failure the change
    exists to prevent. A reference also becomes a path segment under the
    mount, so the grammar check is the only thing between a malformed
    reference and an arbitrary file read; a mutation removing it made the
    provider return /etc/passwd.

  * WIRING. The cordis row names './openbao-credentials.mjs', which resolves
    against the PROFILE directory. If the row ships without the file being
    installed there, boot() throws MODULE_NOT_FOUND and the pod crash-loops --
    it does not degrade. Row, ConfigMap key and install line must move together.

The behaviour half runs the provider under node against stub base classes in
scripts/tests/fixtures/, so it exercises the real file rather than a
description of it. It SKIPS if node is unavailable; the wiring half always runs.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml


class _TolerantLoader(yaml.SafeLoader):
    """cordis.patch.yml carries `!!js` tags this test has no need to evaluate."""


_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def _rows(text):
    return [r for r in yaml.load(text, Loader=_TolerantLoader) if isinstance(r, dict)]

ROOT = pathlib.Path(__file__).resolve().parents[2]
DSH = ROOT / "kubernetes" / "apps" / "apps" / "dsh"
TESTS = pathlib.Path(__file__).resolve().parent

PLUGIN = DSH / "openbao-credentials.mjs"
HARNESS = TESTS / "dsh_credentials_harness.mjs"
INTEGRATION = TESTS / "dsh_credentials_integration.mjs"

#: The shipped closure the integration check runs against. Pinned: checking one
#: version and generalising to another is how the `@latest` inspection of this
#: same package produced a wrong conclusion earlier in its design.
REAL_PACKAGE = "@deepseek-ai/dsh-credentials-local@0.1.5-alpha.2"
STUBS = TESTS / "fixtures" / "dsh-credentials-stubs"

#: The relative specifier the cordis row must name, and the basename the
#: seed-settings initContainer must install into the profile directory.
SPECIFIER = "./openbao-credentials.mjs"
BASENAME = "openbao-credentials.mjs"


def _node():
    return shutil.which("node")


class PluginBehaviour(unittest.TestCase):
    """Run the real provider under node against stub base classes."""

    def test_behaviour_suite_passes(self):
        node = _node()
        if node is None:
            self.skipTest("node is not on PATH; the wiring gates below still run")
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            # A node_modules tree so the plugin's bare specifiers resolve to the
            # stubs. Copying beats a loader flag: no experimental surface, and
            # the file under test is byte-identical to the shipped one.
            shutil.copytree(STUBS, work / "node_modules")
            (work / "package.json").write_text('{"type":"module"}\n')
            shutil.copy(PLUGIN, work / BASENAME)
            shutil.copy(HARNESS, work / "harness.mjs")
            proc = subprocess.run(
                [node, "harness.mjs"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(
            proc.returncode,
            0,
            f"provider behaviour suite failed:\n{proc.stdout}\n{proc.stderr}",
        )
        # Guard against a harness that silently asserts nothing: the count is
        # checked, not just the exit status.
        self.assertIn("passed", proc.stdout)
        summary = proc.stdout.strip().splitlines()[-1]
        passed, total = summary.split()[0].split("/")
        self.assertEqual(passed, total, summary)
        self.assertGreaterEqual(int(total), 15, f"harness lost cases: {summary}")


class RealClosureCompatibility(unittest.TestCase):
    """Run the provider against the SHIPPED base class and real cordis.

    The stub suite above establishes this file's own logic; it cannot establish
    that the real LocalCredentialProvider and the real cordis loader behave the
    way the stubs do. This check closes that gap: it registers the provider with
    a real Context and drives it through `ctx.credentials`, which is the actual
    service-dispatch path, shadow receiver included.

    OPT-IN, because it needs the npm registry. Enable with:

        DSH_REAL_CLOSURE=1 python3 -m unittest discover -s scripts/tests -p "test_*.py"

    It has already earned its place: it is what showed the base class refuses to
    start on a credentials document readable beyond its owner (mode 0600
    required), a behaviour this subclass inherits.
    """

    def test_against_the_shipped_provider(self):
        if not os.environ.get("DSH_REAL_CLOSURE"):
            self.skipTest("set DSH_REAL_CLOSURE=1 to run against the shipped closure (needs npm)")
        node = _node()
        npm = shutil.which("npm")
        if node is None or npm is None:
            self.skipTest("node and npm are both required")
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            (work / "package.json").write_text('{"name":"c","type":"module","private":true}\n')
            install = subprocess.run(
                [npm, "i", "--silent", "--no-audit", "--no-fund", REAL_PACKAGE],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=600,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            shutil.copy(PLUGIN, work / BASENAME)
            shutil.copy(INTEGRATION, work / "integration.mjs")
            proc = subprocess.run(
                [node, "integration.mjs"], cwd=work, capture_output=True, text=True, timeout=300
            )
        self.assertEqual(
            proc.returncode, 0, f"real-closure check failed:\n{proc.stdout}\n{proc.stderr}"
        )
        summary = proc.stdout.strip().splitlines()[-1]
        passed, rest = summary.split()[0].split("/")
        self.assertEqual(passed, rest, summary)


class Wiring(unittest.TestCase):
    """The row, the ConfigMap key and the install line must move together."""

    def setUp(self):
        self.patch = (DSH / "cordis.patch.yml").read_text()
        self.kustomization = (DSH / "kustomization.yaml").read_text()
        self.deployment = (DSH / "deployment.yaml").read_text()

    def test_plugin_file_exists(self):
        self.assertTrue(PLUGIN.is_file(), f"{PLUGIN} is missing")

    def test_row_names_the_relative_specifier(self):
        # A BARE specifier would resolve through the profile's symlink farm,
        # which holds dsh's own closure only, and throw at boot.
        self.assertIn(f"name: '{SPECIFIER}'", self.patch)

    def test_replacement_is_disable_then_insert_not_a_rename(self):
        # applyEntryPatches() treats `name` as an ASSERTION and destructures it
        # out of the overrides, so a patch can never change a row's
        # implementation -- and a patch whose name does not match the target is
        # SKIPPED ENTIRELY, config and all, with only a warning. Patching
        # `- id: credentials` with the new name would therefore leave the
        # shipped provider active and make provisioning OpenBao a no-op, while
        # the pod booted perfectly. The shipped row must be disabled and the
        # replacement inserted under its own loader id.
        rows = _rows(self.patch)
        shipped = [r for r in rows if r.get("id") == "credentials"]
        self.assertEqual(len(shipped), 1, "expected exactly one patch of the shipped row")
        self.assertIs(shipped[0].get("disabled"), True)
        self.assertNotIn(
            "name",
            shipped[0],
            "a name on this patch is an assertion against the SHIPPED implementation; "
            "supplying the replacement's name skips the patch",
        )
        inserted = [
            e
            for r in rows
            for e in (r.get("insert") or [])
            if e.get("name") == SPECIFIER
        ]
        self.assertEqual(len(inserted), 1, "the replacement must be inserted, not renamed")
        self.assertNotEqual(
            inserted[0].get("id"),
            "credentials",
            "the inserted row needs its own loader id; the singleton constraint is on the "
            "SERVICE name, not the row id",
        )

    def test_disable_and_insert_never_ship_apart(self):
        # Disabling without inserting leaves dsh with no credentials service at
        # all; inserting without disabling registers two on one service name.
        rows = _rows(self.patch)
        disabled = any(r.get("id") == "credentials" and r.get("disabled") is True for r in rows)
        inserted = any(
            e.get("name") == SPECIFIER for r in rows for e in (r.get("insert") or [])
        )
        self.assertEqual(disabled, inserted, "the disable and the insert must move together")

    def test_configmap_ships_the_file(self):
        self.assertIn(f"- {BASENAME}", self.kustomization)

    def test_seed_installs_it_into_the_profile_directory(self):
        self.assertIn(
            f"install -m 0644 /seed/{BASENAME} /dsh-home/profiles/web/{BASENAME}",
            self.deployment,
        )

    def test_secret_volume_is_optional(self):
        # The OpenBao path behind dsh-credentials is an operator write that does
        # not exist yet. Without `optional: true` the kubelet refuses to start
        # the pod, turning an unconfigured additive feature into an outage.
        self.assertRegex(
            self.deployment,
            r"secretName: dsh-credentials[^}]*optional: true",
        )

    def test_secret_is_not_mounted_with_subpath(self):
        # A subPath mount is a point-in-time copy kubelet never updates, so a
        # rotated credential would never arrive.
        for line in self.deployment.splitlines():
            if "/dsh-credentials" in line and "mountPath" in line:
                self.assertNotIn("subPath", line, line)

    def test_mount_is_read_only(self):
        self.assertIn("mountPath: /dsh-credentials, readOnly: true", self.deployment)

    def test_secret_mode_is_group_readable(self):
        # kubelet mounts a Secret volume owned by ROOT with the group set to the
        # pod's fsGroup (verified live: `-r--r----- 1 0 65532`). This container
        # runs as uid 1000, so an owner-only mode makes every read fail EACCES --
        # which the provider treats as failure rather than absence, so every
        # resolution would throw. The break would only surface once the operator
        # provisioned OpenBao, long after the change merged.
        self.assertRegex(self.deployment, r"secretName: dsh-credentials, defaultMode: 0440")
        self.assertNotIn("secretName: dsh-credentials, defaultMode: 0400", self.deployment)


if __name__ == "__main__":
    unittest.main()
