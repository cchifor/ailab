#!/usr/bin/env python3
"""Unit tests for scripts/manifest-paths.py — the Flux Kustomization path discovery step that
scripts/manifest-lint.sh feeds to `kustomize build`.

The subject has two obligations: (1) discovery over the REAL repo returns exactly the set of
locally-buildable paths (the recorded set below — see the note on the "24" anchor), never more and
never fewer, and NEVER a path this checkout cannot actually `kustomize build`; (2) discovery FAILS
CLOSED — a Kustomization naming a path with no kustomization.yaml is a non-zero exit, proven with a
fixture, before it is ever proven with the real tree. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "manifest-paths.py"
_spec = importlib.util.spec_from_file_location("manifest_paths", _MOD_PATH)
mp = importlib.util.module_from_spec(_spec)
sys.modules["manifest_paths"] = mp
_spec.loader.exec_module(mp)  # must NOT perform any I/O at import time

REAL_REPO = pathlib.Path(mp.REPO)
REAL_CLUSTER_AI = pathlib.Path(mp.CLUSTER_AI)

#: The full set of Flux Kustomization paths in kubernetes/apps/clusters/ai that this checkout can
#: actually `kustomize build` — i.e. sourceRef resolves to THIS repo's own flux-system source, per
#: kubernetes/apps/clusters/ai/flux-system/gotk-sync.yaml (GitRepository flux-system's url IS
#: cchifor/ailab.git). Two Kustomizations in that directory are deliberately NOT in this set:
#: `agentforge-tenants` (sourceRef -> GitRepository "agentforge-tenants", path "./tenants") and
#: `platform` (sourceRef -> GitRepository "platform", path "./deploy/gitops/flux/clusters/ailab")
#: each name a path in a DIFFERENT repo that does not exist in this checkout at all.
#:
#: PR C-P0-05's spec (tests_first) describes this as "the 24 Flux Kustomization paths listed in
#: clusters/ai" — the anchor has moved since the plan was written: clusters/ai currently holds 25
#: Kustomization documents total (23 locally-sourced + the 2 externally-sourced ones above), not
#: 24+2. The 23-path set below is independently re-derived (every path checked for a real
#: kustomization.yaml, every exclusion checked against its manifest's own sourceRef) rather than
#: hand-copied from the spec text; see deviations_from_spec in the implement report.
EXPECTED_LOCAL_PATHS = frozenset(
    {
        "./kubernetes/apps/infrastructure/agent-sandbox",
        "./kubernetes/apps/infrastructure/agentforge-broker",
        "./kubernetes/apps/infrastructure/agentforge-ci-runners",
        "./kubernetes/apps/infrastructure/agentforge-codex-refresh",
        "./kubernetes/apps/infrastructure/agentforge-pkgcache",
        "./kubernetes/apps/infrastructure/agentforge-runtimeclasses",
        "./kubernetes/apps/infrastructure/agentforge-sandbox",
        "./kubernetes/apps/infrastructure/agentforge-tenant-platform-dev",
        "./kubernetes/apps/agentforge-tenants-bootstrap",
        "./kubernetes/apps/infrastructure/agentforge-workers",
        "./kubernetes/apps/apps",
        "./kubernetes/apps/infrastructure/cert-manager-config",
        "./kubernetes/apps/databases",
        "./kubernetes/apps/edge-connector",
        "./kubernetes/apps/infrastructure/security/external-secrets",
        "./kubernetes/apps/infrastructure",
        "./kubernetes/apps/infrastructure/autoscaling/keda",
        "./kubernetes/apps/infrastructure/autoscaling/kro",
        "./kubernetes/apps/infrastructure/security/openbao-canary",
        "./kubernetes/apps/infrastructure/security/openbao",
        "./kubernetes/apps/platform-bootstrap",
        "./kubernetes/apps/qnap-storage",
        "./kubernetes/apps/infrastructure/testpool",
    }
)

EXCLUDED_EXTERNAL_PATHS = frozenset({"./tenants", "./deploy/gitops/flux/clusters/ailab"})


class DiscoverPathsAgainstRealRepo(unittest.TestCase):
    """These run against the actual kubernetes/apps/clusters/ai tree, not a fixture: the whole
    point of this discovery step is to describe THIS repo's THIS-moment manifest set."""

    def test_matches_the_expected_local_path_set_exactly(self):
        self.assertEqual(set(mp.discover_paths()), set(EXPECTED_LOCAL_PATHS))

    def test_excludes_externally_sourced_kustomizations(self):
        discovered = set(mp.discover_paths())
        self.assertTrue(discovered.isdisjoint(EXCLUDED_EXTERNAL_PATHS))

    def test_every_discovered_path_has_a_kustomization_yaml(self):
        # This is verify_buildable()'s own job; re-proving it here means a future edit to
        # verify_buildable() that silently stops checking cannot go unnoticed by this file.
        for path in mp.discover_paths():
            target = (REAL_REPO / path).resolve()
            self.assertTrue(
                (target / "kustomization.yaml").exists() or (target / "kustomization.yml").exists(),
                f"{path} -> {target} has no kustomization.yaml",
            )

    def test_verify_buildable_accepts_the_real_set(self):
        mp.verify_buildable(mp.discover_paths())  # must not raise

    def test_yaml_and_regex_parsers_agree_on_the_real_tree(self):
        had_yaml = mp.yaml
        try:
            mp.yaml = None  # force the stdlib regex fallback
            regex_paths = set(mp.discover_paths())
        finally:
            mp.yaml = had_yaml
        self.assertEqual(regex_paths, set(mp.discover_paths()))
        self.assertEqual(regex_paths, set(EXPECTED_LOCAL_PATHS))

    def test_output_is_one_path_per_stdout_line_and_exit_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mp.main()
        self.assertEqual(code, 0)
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(set(lines), set(EXPECTED_LOCAL_PATHS))


class FailClosedOnABrokenFixture(unittest.TestCase):
    """Proves the shape scripts/manifest-lint.sh depends on: a Kustomization naming a path with
    no kustomization.yaml must not be silently skipped — discovery itself must fail closed, since
    `bash -euo pipefail scripts/manifest-lint.sh` stops the instant this script's exit is non-zero."""

    def _write_local_kustomization(self, cluster_ai: pathlib.Path, name: str, path: str) -> None:
        (cluster_ai / f"{name}.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            f"  name: {name}\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            f"  path: {path}\n"
            "  prune: true\n"
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.cluster_ai = self.repo / "kubernetes/apps/clusters/ai"
        self.cluster_ai.mkdir(parents=True)
        self._orig_repo, self._orig_cluster_ai = mp.REPO, mp.CLUSTER_AI
        mp.REPO, mp.CLUSTER_AI = self.repo, self.cluster_ai

    def tearDown(self):
        mp.REPO, mp.CLUSTER_AI = self._orig_repo, self._orig_cluster_ai
        self._tmp.cleanup()

    def test_good_fixture_discovers_and_verifies_clean(self):
        good = self.repo / "kubernetes/apps/ok"
        good.mkdir(parents=True)
        (good / "kustomization.yaml").write_text("resources: []\n")
        self._write_local_kustomization(self.cluster_ai, "ok", "./kubernetes/apps/ok")

        paths = mp.discover_paths()
        self.assertEqual(paths, ["./kubernetes/apps/ok"])
        mp.verify_buildable(paths)  # must not raise

    def test_missing_kustomization_yaml_fails_verify_buildable(self):
        # Deliberately create NO kustomization.yaml at the named path.
        (self.repo / "kubernetes/apps/broken").mkdir(parents=True)
        self._write_local_kustomization(self.cluster_ai, "broken", "./kubernetes/apps/broken")

        paths = mp.discover_paths()
        self.assertEqual(paths, ["./kubernetes/apps/broken"])
        with self.assertRaises(mp.DiscoveryError):
            mp.verify_buildable(paths)

    def test_missing_kustomization_yaml_makes_main_exit_non_zero(self):
        (self.repo / "kubernetes/apps/broken").mkdir(parents=True)
        self._write_local_kustomization(self.cluster_ai, "broken", "./kubernetes/apps/broken")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mp.main()
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")  # no partial path list on failure
        self.assertIn("broken", err.getvalue())

    def test_locally_sourced_kustomization_with_no_path_fails_closed(self):
        (self.cluster_ai / "nopath.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: nopath\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  prune: true\n"
        )
        with self.assertRaises(mp.DiscoveryError):
            mp.discover_paths()

    def test_externally_sourced_kustomization_is_excluded_even_when_broken(self):
        # A path in another repo (no local kustomization.yaml, and never could have one) must be
        # excluded rather than reported as a local failure.
        (self.cluster_ai / "external.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: external\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: some-other-repo\n"
            "  path: ./somewhere-else\n"
            "  prune: true\n"
        )
        self.assertEqual(mp.discover_paths(), [])

    def test_regex_fallback_also_fails_closed_on_the_broken_fixture(self):
        (self.repo / "kubernetes/apps/broken").mkdir(parents=True)
        self._write_local_kustomization(self.cluster_ai, "broken", "./kubernetes/apps/broken")

        had_yaml = mp.yaml
        try:
            mp.yaml = None
            paths = mp.discover_paths()
            with self.assertRaises(mp.DiscoveryError):
                mp.verify_buildable(paths)
        finally:
            mp.yaml = had_yaml


if __name__ == "__main__":
    unittest.main()
