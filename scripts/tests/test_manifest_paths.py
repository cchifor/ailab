#!/usr/bin/env python3
"""Unit tests for scripts/manifest-paths.py — the Flux Kustomization path discovery step that
scripts/manifest-lint.sh feeds to `kustomize build`.

The subject has four obligations: (1) discovery over the REAL repo returns exactly the set of
locally-buildable paths (the recorded set below — see the note on the "24" anchor), never more and
never fewer, and NEVER a path this checkout cannot actually `kustomize build`; (2) discovery FAILS
CLOSED — a Kustomization naming a path with no kustomization.yaml is a non-zero exit, proven with a
fixture, before it is ever proven with the real tree; (3) a Kustomization's `sourceRef` is RESOLVED
against the GitRepository objects this tree actually declares (namespace + name, then the object's
`spec.url`) — "local" means the resolved object's url IS this repo, "external" means it resolves to
a declared object whose url is a different repo AND that object is a reviewed entry in
EXPECTED_EXTERNAL_SOURCES; anything else (missing/non-mapping sourceRef, missing keys, an
undeclared object, another namespace, a same-named object repointed elsewhere, an unreviewed
external) is a non-zero exit, not a silent exclusion; (4) the stdlib regex fallback (what runs in
CI, where PyYAML is not installed) agrees with PyYAML on every legal YAML layout it accepts and
raises DiscoveryError — never silently drops a document — on anything it cannot parse
unambiguously. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import stat
import subprocess
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
#: hand-copied from the spec text; see deviations_from_spec in the implement report. The 2
#: exclusions are not just "not local", either — mp.EXPECTED_EXTERNAL_SOURCES is a closed,
#: reviewed allowlist of exactly those two GitRepository objects; a THIRD externally-sourced
#: Kustomization appearing in clusters/ai without a matching entry there makes discover_paths()
#: raise DiscoveryError rather than silently grow this exclusion set (round-1 codex cross-review
#: finding; see FailClosedOnABrokenFixture.test_unrecognized_external_source_ref_fails_closed).
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

#: The url the REAL gotk-sync.yaml declares for GitRepository flux-system/flux-system (ADR 0017:
#: the in-cluster Gitea forge is the master; GitHub is a push-mirror backup of the SAME repo).
LOCAL_URL = "http://gitea-http.gitea.svc.cluster.local:3000/cchifor/ailab.git"
GITHUB_BACKUP_URL = "ssh://git@github.com/cchifor/ailab"
TENANTS_URL = "ssh://git@gitea-ssh.gitea.svc.cluster.local:2222/cchifor/agentforge-tenants.git"


@contextlib.contextmanager
def _regex_mode():
    """Force the stdlib regex fallback (what the CI runner actually uses — no PyYAML there)."""
    had_yaml = mp.yaml
    mp.yaml = None
    try:
        yield
    finally:
        mp.yaml = had_yaml


def _git_repository_doc(name: str, url: str, namespace: str = "flux-system") -> str:
    return (
        "apiVersion: source.toolkit.fluxcd.io/v1\n"
        "kind: GitRepository\n"
        "metadata:\n"
        f"  name: {name}\n"
        f"  namespace: {namespace}\n"
        "spec:\n"
        "  interval: 1m0s\n"
        "  ref:\n"
        "    branch: main\n"
        f"  url: {url}\n"
    )


def _write_flux_system_source(cluster_ai: pathlib.Path, url: str = LOCAL_URL) -> None:
    """Mirror the REAL kubernetes/apps/clusters/ai/flux-system/gotk-sync.yaml shape: a comment
    header, a `---` marker, the flux-system GitRepository, then Flux's own bootstrap Kustomization
    (path ./kubernetes/apps/clusters/ai) which discovery must NOT report as a path under test."""
    (cluster_ai / "flux-system").mkdir(parents=True, exist_ok=True)
    (cluster_ai / "flux-system" / "gotk-sync.yaml").write_text(
        "# This manifest was generated by flux. DO NOT EDIT.\n"
        "---\n"
        + _git_repository_doc("flux-system", url)
        + "---\n"
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
        "kind: Kustomization\n"
        "metadata:\n"
        "  name: flux-system\n"
        "  namespace: flux-system\n"
        "spec:\n"
        "  interval: 10m0s\n"
        "  path: ./kubernetes/apps/clusters/ai\n"
        "  prune: true\n"
        "  sourceRef:\n"
        "    kind: GitRepository\n"
        "    name: flux-system\n"
    )


def _kustomization_doc(
    name: str,
    path: str,
    source_name: str = "flux-system",
    source_kind: str = "GitRepository",
    source_namespace: str | None = None,
    namespace: str | None = "flux-system",
) -> str:
    meta = f"  name: {name}\n" + (f"  namespace: {namespace}\n" if namespace else "")
    ref = f"    kind: {source_kind}\n" + (
        f"    namespace: {source_namespace}\n" if source_namespace else ""
    ) + f"    name: {source_name}\n"
    return (
        "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
        "kind: Kustomization\n"
        "metadata:\n"
        + meta
        + "spec:\n"
        "  interval: 10m\n"
        "  sourceRef:\n"
        + ref
        + f"  path: {path}\n"
        "  prune: true\n"
    )


class _FixtureRepo(unittest.TestCase):
    """A throwaway repo tree with clusters/ai + the flux-system source declared, monkeypatched
    onto the module globals (discover_paths()/verify_buildable() read them at call time)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name)
        self.cluster_ai = self.repo / "kubernetes/apps/clusters/ai"
        self.cluster_ai.mkdir(parents=True)
        _write_flux_system_source(self.cluster_ai)
        self._orig_repo, self._orig_cluster_ai = mp.REPO, mp.CLUSTER_AI
        mp.REPO, mp.CLUSTER_AI = self.repo, self.cluster_ai

    def tearDown(self):
        mp.REPO, mp.CLUSTER_AI = self._orig_repo, self._orig_cluster_ai
        self._tmp.cleanup()

    def _mk_target(self, path: str) -> None:
        target = self.repo / path
        target.mkdir(parents=True, exist_ok=True)
        (target / "kustomization.yaml").write_text("resources: []\n")

    def _discover_both_modes(self) -> list[str]:
        """discover_paths() under PyYAML AND the regex fallback; both must agree, and the caller
        gets the shared answer. Used wherever a fixture is legal YAML both parsers must accept."""
        with_yaml = mp.discover_paths()
        with _regex_mode():
            with_regex = mp.discover_paths()
        self.assertEqual(with_regex, with_yaml, "regex fallback disagrees with PyYAML")
        return with_yaml

    def _assert_raises_both_modes(self, needle: str | None = None) -> None:
        for label, ctx in (("pyyaml", contextlib.nullcontext()), ("regex", _regex_mode())):
            with ctx, self.assertRaises(mp.DiscoveryError, msg=f"{label} mode did not fail closed") as cm:
                with contextlib.redirect_stderr(io.StringIO()):
                    mp.discover_paths()
            if needle:
                self.assertIn(needle, str(cm.exception), f"{label} mode: {cm.exception}")


class DiscoverPathsAgainstRealRepo(unittest.TestCase):
    """These run against the actual kubernetes/apps/clusters/ai tree, not a fixture: the whole
    point of this discovery step is to describe THIS repo's THIS-moment manifest set."""

    def test_matches_the_expected_local_path_set_exactly(self):
        self.assertEqual(set(mp.discover_paths()), set(EXPECTED_LOCAL_PATHS))

    def test_excludes_externally_sourced_kustomizations(self):
        discovered = set(mp.discover_paths())
        self.assertTrue(discovered.isdisjoint(EXCLUDED_EXTERNAL_PATHS))

    def test_real_tree_excludes_only_the_reviewed_allowlist_and_reports_them(self):
        # Every currently non-local Kustomization in clusters/ai must be covered by the closed
        # allowlist (discover_paths() itself already fails closed on anything else — proven by
        # the fixture tests below); this also proves the exclusion is printed, not silent.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            paths = mp.discover_paths()
        self.assertEqual(set(paths), set(EXPECTED_LOCAL_PATHS))
        stderr = buf.getvalue()
        for _namespace, name in mp.EXPECTED_EXTERNAL_SOURCES:
            self.assertIn(name, stderr)

    def test_real_tree_declares_exactly_the_three_git_repositories(self):
        # The resolution table discovery classifies against: this repo's own bootstrap source plus
        # the two reviewed external ones — nothing else, and each with a url that identifies the
        # repo it really points at.
        sources = mp.declared_git_repositories()
        self.assertEqual(
            set(sources),
            {("flux-system", "flux-system"), ("flux-system", "agentforge-tenants"), ("flux-system", "platform")},
        )
        self.assertTrue(mp.is_this_repo(sources[("flux-system", "flux-system")]))
        self.assertFalse(mp.is_this_repo(sources[("flux-system", "agentforge-tenants")]))
        self.assertFalse(mp.is_this_repo(sources[("flux-system", "platform")]))

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
        with _regex_mode():
            regex_paths = set(mp.discover_paths())
            regex_sources = mp.declared_git_repositories()
        self.assertEqual(regex_paths, set(mp.discover_paths()))
        self.assertEqual(regex_paths, set(EXPECTED_LOCAL_PATHS))
        self.assertEqual(regex_sources, mp.declared_git_repositories())

    def test_output_is_one_path_per_stdout_line_and_exit_zero(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = mp.main()
        self.assertEqual(code, 0)
        lines = [l for l in buf.getvalue().splitlines() if l]
        self.assertEqual(set(lines), set(EXPECTED_LOCAL_PATHS))

    def test_main_reports_which_parser_ran(self):
        # A CI log must show whether the real parser or the stdlib fallback classified the tree.
        for ctx, needle in ((contextlib.nullcontext(), "PyYAML"), (_regex_mode(), "stdlib")):
            err = io.StringIO()
            with ctx, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(mp.main(), 0)
            self.assertIn(needle, err.getvalue())


class RepoUrlIdentity(unittest.TestCase):
    """is_this_repo(): the url forms Flux GitRepository objects in this estate actually use."""

    def test_gitea_http_url_is_this_repo(self):
        self.assertTrue(mp.is_this_repo(LOCAL_URL))

    def test_github_backup_url_is_this_repo(self):
        # ADR 0017: GitHub holds a push-mirror of the SAME repo (gotk-sync.yaml's header names it as
        # the rollback url) — same content, so a Kustomization sourced from it IS buildable here.
        self.assertTrue(mp.is_this_repo(GITHUB_BACKUP_URL))
        self.assertTrue(mp.is_this_repo("ssh://git@github.com/cchifor/ailab.git"))
        self.assertTrue(mp.is_this_repo("https://git.chifor.me/cchifor/ailab.git"))

    def test_other_repos_are_not_this_repo(self):
        self.assertFalse(mp.is_this_repo(TENANTS_URL))
        self.assertFalse(mp.is_this_repo("ssh://git@github.com/cchifor/platform"))
        self.assertFalse(mp.is_this_repo("https://github.com/qnap-dev/QNAP-CSI-PlugIn"))
        self.assertFalse(mp.is_this_repo("https://github.com/someone-else/ailab.git"))
        self.assertFalse(mp.is_this_repo(""))

    def test_every_known_host_and_url_form_of_this_repo_is_accepted(self):
        # The hosts that actually serve cchifor/ailab: the in-cluster Gitea http and ssh Services,
        # the public Gitea hostname, and the GitHub push-mirror (ADR 0017) — in every url form
        # Flux's GitRepository accepts (scheme://, with/without userinfo and port, scp-like).
        for url in (
            "http://gitea-http.gitea.svc.cluster.local:3000/cchifor/ailab.git",
            "ssh://git@gitea-ssh.gitea.svc.cluster.local:2222/cchifor/ailab.git",
            "https://git.chifor.me/cchifor/ailab",
            "https://git.chifor.me/cchifor/ailab.git",
            "ssh://git@github.com/cchifor/ailab",
            "https://github.com/cchifor/ailab.git",
            "git@github.com:cchifor/ailab.git",
            "git@git.chifor.me:cchifor/ailab",
        ):
            self.assertTrue(mp.is_this_repo(url), url)

    def test_same_slug_on_an_unknown_host_is_not_this_repo(self):
        # Repository identity is host + slug, not the slug alone: a GitRepository repointed at
        # `unrelated.example/cchifor/ailab` serves content that is NOT this checkout (round-3
        # codex finding, :323).
        for url in (
            "https://unrelated.example/cchifor/ailab.git",
            "ssh://git@evil.example/cchifor/ailab",
            "git@unrelated.example:cchifor/ailab.git",
            "https://github.com.evil.example/cchifor/ailab",
            "https://notgithub.com/cchifor/ailab",
            "https://git.chifor.me/deeper/cchifor/ailab",
        ):
            self.assertFalse(mp.is_this_repo(url), url)


class FailClosedOnABrokenFixture(_FixtureRepo):
    """Proves this module's own contribution to the shape scripts/manifest-lint.sh depends on: a
    Kustomization naming a path with no kustomization.yaml must not be silently skipped —
    discover_paths()/verify_buildable()/main() must fail closed, at the Python level, since
    `bash -euo pipefail scripts/manifest-lint.sh` stops the instant `manifest-paths.py`'s own exit
    is non-zero. manifest-lint.sh's OWN failure paths are exercised by
    ManifestLintScriptFailsClosedOnABrokenFixture below (with a stubbed docker)."""

    def _write_local_kustomization(self, cluster_ai: pathlib.Path, name: str, path: str) -> None:
        (cluster_ai / f"{name}.yaml").write_text(_kustomization_doc(name, path))

    def test_good_fixture_discovers_and_verifies_clean(self):
        self._mk_target("kubernetes/apps/ok")
        self._write_local_kustomization(self.cluster_ai, "ok", "./kubernetes/apps/ok")

        paths = self._discover_both_modes()
        self.assertEqual(paths, ["./kubernetes/apps/ok"])
        mp.verify_buildable(paths)  # must not raise

    def test_flux_bootstrap_kustomization_in_gotk_sync_is_not_a_path_under_test(self):
        # gotk-sync.yaml is read ONLY for the flux-system GitRepository declaration; its own
        # Kustomization (path ./kubernetes/apps/clusters/ai) is Flux's bootstrap, not a manifest
        # under test — with nothing else in clusters/ai discovery must report zero paths.
        self.assertEqual(self._discover_both_modes(), [])

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
        self._assert_raises_both_modes("no spec.path")

    def test_externally_sourced_kustomization_is_excluded_even_when_broken(self):
        # An ALLOWLISTED external source that IS declared as a GitRepository in this tree (no local
        # kustomization.yaml, and never could have one) must be excluded rather than reported as a
        # local failure — and the exclusion must be printed, not silent.
        (self.cluster_ai / "agentforge-tenants-source.yaml").write_text(
            _git_repository_doc("agentforge-tenants", TENANTS_URL)
        )
        (self.cluster_ai / "external.yaml").write_text(
            _kustomization_doc("external", "./somewhere-else", source_name="agentforge-tenants")
        )
        for ctx in (contextlib.nullcontext(), _regex_mode()):
            buf = io.StringIO()
            with ctx, contextlib.redirect_stderr(buf):
                self.assertEqual(mp.discover_paths(), [])
            self.assertIn("agentforge-tenants", buf.getvalue())

    def test_unrecognized_external_source_ref_fails_closed(self):
        # A sourceRef that resolves to a declared GitRepository of a different repo but is NOT a
        # reviewed entry in EXPECTED_EXTERNAL_SOURCES must not be silently dropped — that would
        # let the gate's coverage shrink unnoticed the moment anyone adds a Kustomization with a
        # genuinely new external source. It must fail closed instead (round-1 codex finding).
        (self.cluster_ai / "some-other-repo-source.yaml").write_text(
            _git_repository_doc("some-other-repo", "ssh://git@github.com/cchifor/some-other-repo")
        )
        (self.cluster_ai / "mystery.yaml").write_text(
            _kustomization_doc("mystery", "./somewhere-else", source_name="some-other-repo")
        )
        self._assert_raises_both_modes("EXPECTED_EXTERNAL_SOURCES")

    def test_missing_source_ref_fails_closed_rather_than_silently_excluded(self):
        # A Kustomization with no sourceRef at all used to be treated as "not local" and silently
        # dropped; that is the same silent-narrowing shape as an unrecognized external sourceRef,
        # so it must fail closed too.
        (self.cluster_ai / "nosourceref.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: nosourceref\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  path: ./somewhere-else\n"
            "  prune: true\n"
        )
        self._assert_raises_both_modes("sourceRef")

    def test_regex_fallback_also_fails_closed_on_the_broken_fixture(self):
        (self.repo / "kubernetes/apps/broken").mkdir(parents=True)
        self._write_local_kustomization(self.cluster_ai, "broken", "./kubernetes/apps/broken")

        with _regex_mode():
            paths = mp.discover_paths()
            with self.assertRaises(mp.DiscoveryError):
                mp.verify_buildable(paths)


class SourceRefResolution(_FixtureRepo):
    """A Kustomization's sourceRef is a REFERENCE — (kind, namespace, name) — to a GitRepository
    object this tree declares, and only that object's spec.url says which repo the path lives in.
    Matching the reference's name string alone (`name: flux-system` == local) is not resolution:
    a same-named object in another namespace, a repointed bootstrap GitRepository, or a reference
    to an object nobody declares would all be misclassified (round-2 codex findings, :141/:177).
    Every case here is asserted under BOTH parsers."""

    def test_source_ref_that_is_not_a_mapping_fails_closed(self):
        (self.cluster_ai / "scalar-ref.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: scalar-ref\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef: flux-system\n"
            "  path: ./kubernetes/apps/x\n"
        )
        self._assert_raises_both_modes("sourceRef")

    def test_source_ref_missing_name_fails_closed(self):
        (self.cluster_ai / "noname.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: noname\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "  path: ./kubernetes/apps/x\n"
        )
        self._assert_raises_both_modes("sourceRef")

    def test_source_ref_missing_kind_fails_closed(self):
        (self.cluster_ai / "nokind.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: nokind\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/x\n"
        )
        self._assert_raises_both_modes("sourceRef")

    def test_non_git_repository_source_kind_fails_closed(self):
        # OCIRepository/Bucket sources are not resolvable to a repo url by this tool; the day one
        # appears in clusters/ai is a decision for a reviewer, not a silent skip.
        (self.cluster_ai / "oci.yaml").write_text(
            _kustomization_doc("oci", "./kubernetes/apps/x", source_kind="OCIRepository", source_name="flux-system")
        )
        self._assert_raises_both_modes("OCIRepository")

    def test_reference_to_an_undeclared_git_repository_fails_closed(self):
        # Name string says "flux-system" but NO GitRepository of that name is declared anywhere
        # discovery reads — the reference cannot be resolved, so it cannot be classified local.
        (self.cluster_ai / "flux-system" / "gotk-sync.yaml").unlink()
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(_kustomization_doc("x", "./kubernetes/apps/x"))
        self._assert_raises_both_modes("flux-system/flux-system")

    def test_reference_into_another_namespace_is_not_the_local_source(self):
        # Same name, different namespace: sourceRef.namespace is part of the reference. There is no
        # GitRepository other-ns/flux-system declared, so this must fail closed, not build locally.
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(
            _kustomization_doc("x", "./kubernetes/apps/x", source_namespace="other-ns")
        )
        self._assert_raises_both_modes("other-ns/flux-system")

    def test_source_ref_namespace_defaults_to_the_kustomization_namespace(self):
        # Flux semantics: an omitted sourceRef.namespace means the Kustomization's own namespace.
        # A Kustomization in `other-ns` referencing `flux-system` by bare name therefore points at
        # other-ns/flux-system — undeclared — NOT at flux-system/flux-system.
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(
            _kustomization_doc("x", "./kubernetes/apps/x", namespace="other-ns")
        )
        self._assert_raises_both_modes("other-ns/flux-system")

    def test_explicit_source_ref_namespace_matching_the_declaration_is_local(self):
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(
            _kustomization_doc("x", "./kubernetes/apps/x", source_namespace="flux-system")
        )
        self.assertEqual(self._discover_both_modes(), ["./kubernetes/apps/x"])

    def test_kustomization_without_a_namespace_fails_closed(self):
        # No metadata.namespace and no sourceRef.namespace: the reference's namespace would be
        # whatever the applier defaults to — not something this tool should guess.
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(_kustomization_doc("x", "./kubernetes/apps/x", namespace=None))
        self._assert_raises_both_modes("namespace")

    def test_bootstrap_git_repository_repointed_at_another_repo_is_not_local(self):
        # The flux-system GitRepository exists, but its url is a DIFFERENT repo: what Flux would
        # apply is not this checkout, so building this checkout would validate the wrong content.
        # Not local, and not a reviewed external either -> fail closed.
        _write_flux_system_source(self.cluster_ai, url="ssh://git@github.com/cchifor/platform")
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(_kustomization_doc("x", "./kubernetes/apps/x"))
        self._assert_raises_both_modes("cchifor/platform")

    def test_bootstrap_git_repository_on_an_unknown_host_is_not_local(self):
        # Same slug, unknown host: Flux would consume content from a server this estate does not
        # run, so building THIS checkout would validate the wrong thing -> not local, not a
        # reviewed external -> fail closed (round-3 codex finding, :323).
        _write_flux_system_source(self.cluster_ai, url="https://unrelated.example/cchifor/ailab.git")
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(_kustomization_doc("x", "./kubernetes/apps/x"))
        self._assert_raises_both_modes("unrelated.example")

    def test_bootstrap_git_repository_on_the_github_backup_url_is_local(self):
        # ADR 0017 rollback shape: same repo, mirrored on GitHub.
        _write_flux_system_source(self.cluster_ai, url=GITHUB_BACKUP_URL)
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(_kustomization_doc("x", "./kubernetes/apps/x"))
        self.assertEqual(self._discover_both_modes(), ["./kubernetes/apps/x"])

    def test_allowlisted_external_source_must_still_be_declared(self):
        # `agentforge-tenants` is on the reviewed allowlist, but nothing declares a GitRepository
        # of that name here: an unresolvable reference is an error even when its name is familiar.
        (self.cluster_ai / "external.yaml").write_text(
            _kustomization_doc("external", "./tenants", source_name="agentforge-tenants")
        )
        self._assert_raises_both_modes("flux-system/agentforge-tenants")

    def test_allowlisted_name_whose_declared_url_is_this_repo_is_local(self):
        # The allowlist never overrides resolution: if the object named `platform` actually points
        # at THIS repo, its path is local and buildable, so it must be built, not excluded.
        (self.cluster_ai / "platform-source.yaml").write_text(_git_repository_doc("platform", LOCAL_URL))
        self._mk_target("kubernetes/apps/x")
        (self.cluster_ai / "x.yaml").write_text(_kustomization_doc("x", "./kubernetes/apps/x", source_name="platform"))
        self.assertEqual(self._discover_both_modes(), ["./kubernetes/apps/x"])

    def test_duplicate_git_repository_declaration_fails_closed(self):
        (self.cluster_ai / "dup-source.yaml").write_text(_git_repository_doc("flux-system", TENANTS_URL))
        self._assert_raises_both_modes("declared twice")

    def test_git_repository_without_url_fails_closed(self):
        (self.cluster_ai / "nourl-source.yaml").write_text(
            "apiVersion: source.toolkit.fluxcd.io/v1\n"
            "kind: GitRepository\n"
            "metadata:\n"
            "  name: nourl\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 1m0s\n"
        )
        self._assert_raises_both_modes("spec.url")


class RegexFallbackParityWithPyYAML(_FixtureRepo):
    """The regex fallback (_docs_via_regex) is what actually runs on the CI runner today (see
    manifest-paths.py's module docstring — nothing there installs PyYAML, and the runner image has
    no pip). It must therefore agree with the PyYAML parser on YAML that is legal but NOT in this
    repo's own house style — flow style, other indent widths, blank/comment lines inside a block,
    quoted or comment-trailed scalars, key order — and on anything it CANNOT parse unambiguously it
    must raise DiscoveryError rather than silently drop the document (a silent drop is a
    Kustomization that never gets built while the gate stays green; review findings :68/:92)."""

    def setUp(self):
        super().setUp()
        self._mk_target("kubernetes/apps/reordered")

    def _discover_via_regex(self) -> list[str]:
        with _regex_mode():
            return mp.discover_paths()

    def _assert_parity(self, expected: list[str]) -> None:
        self.assertEqual(self._discover_both_modes(), expected)

    def test_reordered_source_ref_keys_still_resolve_local(self):
        # `name:` BEFORE `kind:` — legal YAML, illegal for the old adjacency-and-order regex.
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  sourceRef:\n"
            "    name: flux-system\n"
            "    kind: GitRepository\n"
            "  path: ./kubernetes/apps/reordered\n"
            "  prune: true\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_source_ref_with_intervening_key_still_resolves_local(self):
        # A `namespace:` line BETWEEN `kind:` and `name:` — also legal, also broke the old regex.
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    namespace: flux-system\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
            "  prune: true\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_quoted_path_is_unquoted_same_as_pyyaml(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            '  path: "./kubernetes/apps/reordered"\n'
            "  prune: true\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_flow_style_source_ref_resolves_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  sourceRef: {kind: GitRepository, name: flux-system}\n"
            "  path: ./kubernetes/apps/reordered\n"
            "  prune: true\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_flow_style_source_ref_with_quotes_and_comment_resolves_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            '  sourceRef: { name: "flux-system", kind: \'GitRepository\' }  # this repo\n'
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_four_space_indentation_resolves_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "    name: reordered\n"
            "    namespace: flux-system\n"
            "spec:\n"
            "    interval: 10m\n"
            "    sourceRef:\n"
            "        kind: GitRepository\n"
            "        name: flux-system\n"
            "    path: ./kubernetes/apps/reordered\n"
            "    prune: true\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_one_space_indentation_resolves_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            " name: reordered\n"
            " namespace: flux-system\n"
            "spec:\n"
            " sourceRef:\n"
            "  kind: GitRepository\n"
            "  name: flux-system\n"
            " path: ./kubernetes/apps/reordered\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_blank_and_comment_lines_inside_source_ref_block_resolve_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "\n"
            "    # the bootstrap source, i.e. this repo\n"
            "\n"
            "    name: flux-system\n"
            "\n"
            "  # own path (estate pattern)\n"
            "  path: ./kubernetes/apps/reordered\n"
            "  prune: true\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_quoted_kind_and_trailing_comments_resolve_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            'kind: "Kustomization"  # quoted, still a Kustomization\n'
            "metadata:\n"
            "  name: 'reordered'\n"
            "  namespace: flux-system # trailing comment\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository # x\n"
            '    name: "flux-system"\n'
            "  path: ./kubernetes/apps/reordered # own path\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_commented_kind_line_resolves_local(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization # comment\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_sequence_items_at_the_parent_key_indent_are_tolerated(self):
        # `dependsOn:` followed by `- name:` at the SAME indent as the key is legal YAML (a block
        # sequence may sit at its parent key's indentation) and appears in generated manifests.
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  dependsOn:\n"
            "  - name: infrastructure\n"
            "  - name: openbao\n"
            "  healthCheckExprs:\n"
            "    - apiVersion: postgresql.cnpg.io/v1\n"
            "      kind: Cluster\n"
            "      current: status.conditions.filter(e, e.type == 'Ready').all(e, e.status == 'True')\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_nested_kind_keys_do_not_masquerade_as_the_document_kind(self):
        # A GitRepository-kind document that mentions `kind: Kustomization` in a nested block is
        # NOT a Kustomization; only the top-level `kind:` counts (both parsers).
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "data:\n"
            "  template: |\n"
            "    kind: Kustomization\n"
            "    path: ./kubernetes/apps/reordered\n"
        )
        self._assert_parity([])

    def test_multi_document_file_with_comment_only_leading_document(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "# header comment only\n"
            "--- # marker with a comment\n"
            + _git_repository_doc("another", "ssh://git@github.com/cchifor/another")
            + "---\n"
            + _kustomization_doc("reordered", "./kubernetes/apps/reordered")
            + "---\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_alias_source_ref_is_rejected_by_the_fallback_not_dropped(self):
        # PyYAML resolves the anchor; the stdlib fallback cannot, and must say so rather than treat
        # the document as sourceRef-less or (worse) skip it.
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "x-source: &src {kind: GitRepository, name: flux-system}\n"
            "spec:\n"
            "  sourceRef: *src\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self.assertEqual(mp.discover_paths(), ["./kubernetes/apps/reordered"])
        with _regex_mode(), self.assertRaises(mp.DiscoveryError) as cm:
            mp.discover_paths()
        self.assertIn("sourceRef", str(cm.exception))

    def test_flow_style_spec_is_rejected_by_the_fallback_not_dropped(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata: {name: reordered, namespace: flux-system}\n"
            "spec: {sourceRef: {kind: GitRepository, name: flux-system}, path: ./kubernetes/apps/reordered}\n"
        )
        self.assertEqual(mp.discover_paths(), ["./kubernetes/apps/reordered"])
        with _regex_mode(), self.assertRaises(mp.DiscoveryError):
            mp.discover_paths()

    def test_block_scalar_path_is_rejected_by_the_fallback_not_dropped(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: >-\n"
            "    ./kubernetes/apps/reordered\n"
        )
        self.assertEqual(mp.discover_paths(), ["./kubernetes/apps/reordered"])
        with _regex_mode(), self.assertRaises(mp.DiscoveryError):
            mp.discover_paths()

    def test_document_with_content_but_no_top_level_kind_fails_closed_in_both_modes(self):
        # Not a Kubernetes object at all (or one whose `kind:` is nested/misindented): neither
        # parser may quietly skip it, because "skipped" and "not a Kustomization" look identical.
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            " kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
        )
        with self.assertRaises(mp.DiscoveryError):
            mp.discover_paths()
        with _regex_mode(), self.assertRaises(mp.DiscoveryError):
            mp.discover_paths()

    def test_sequence_under_a_scalar_valued_key_is_rejected_in_both_modes(self):
        # `interval: 10m` followed by an indented `- invalid` is NOT YAML (a real parser rejects
        # it). The fallback must not quietly attach the stray lines to `interval` just because it
        # never extracts that key — that would let CI pass a manifest Flux rejects (round-3 codex
        # finding, :211).
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "    - invalid\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_raises_both_modes()

    def test_sequence_at_key_indent_under_a_scalar_valued_key_is_rejected_in_both_modes(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  interval: 10m\n"
            "  - invalid\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_raises_both_modes()

    def test_nested_mapping_under_a_scalar_valued_key_is_rejected_in_both_modes(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  prune: true\n"
            "    bogus: 1\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_raises_both_modes()

    def test_block_scalar_under_an_unextracted_key_is_tolerated(self):
        # A block scalar (`|`/`>`) IS a value that legitimately owns the indented lines after it;
        # the strictness above must not reject this legal, common shape.
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "  annotations:\n"
            "    note: |\n"
            "      multi\n"
            "      line\n"
            "spec:\n"
            "  description: >-  # folded\n"
            "    a folded\n"
            "    description\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            "  path: ./kubernetes/apps/reordered\n"
        )
        self._assert_parity(["./kubernetes/apps/reordered"])

    def test_unterminated_quote_is_rejected_by_the_fallback(self):
        (self.cluster_ai / "reordered.yaml").write_text(
            "apiVersion: kustomize.toolkit.fluxcd.io/v1\n"
            "kind: Kustomization\n"
            "metadata:\n"
            "  name: reordered\n"
            "  namespace: flux-system\n"
            "spec:\n"
            "  sourceRef:\n"
            "    kind: GitRepository\n"
            "    name: flux-system\n"
            '  path: "./kubernetes/apps/reordered\n'
        )
        with _regex_mode(), self.assertRaises(mp.DiscoveryError):
            mp.discover_paths()


class ManifestLintScriptFailsClosedOnABrokenFixture(unittest.TestCase):
    """The tests_first requirement (spec: "a fixture kustomization pointing at a missing file
    makes the lint script exit non-zero") is about scripts/manifest-lint.sh itself, not just the
    Python discovery step it calls out to — FailClosedOnABrokenFixture above only proves
    manifest-paths.py's own main() fails closed. This class actually runs
    `bash scripts/manifest-lint.sh` as a subprocess against a fixture repo (a real copy of the two
    scripts, no docker network calls) and proves the script's own control flow — the
    `set -euo pipefail` + "write PATHS_FILE to a real file, not a process substitution" shape
    documented at the top of manifest-lint.sh — actually propagates a discovery failure to the
    script's exit code, BEFORE any `docker run` (kustomize build / kubeconform) is ever reached.
    `docker` is stubbed on PATH (so the script's own `command -v docker` precondition passes) but
    the stub only ever records its arguments, one invocation per line; asserting on that record is
    what proves WHICH docker calls happened (round-1 codex review finding: this was the one failure
    path the test suite previously only proved manually).

    The broken-fixture test was added AFTER manifest-lint.sh's shell-level fail-closed behavior
    already existed and already passed (06b60f0, before this test's own commit) — a genuine
    tests-first red/green commit pair for it would mean rewriting scripts/manifest-lint.sh's
    already-pushed history, which this repo's git rules forbid (no force-push, no amending a pushed
    commit; round-2 codex review finding). To prove this is real coverage rather than a tautological
    backfill, it was manually red-proofed against two independent regressions of the exact shape
    the file's own header comments warn about, each reverted before committing: (1) restoring the
    `mapfile -t PATHS < <(...)` process-substitution form the header comment explicitly rejects;
    (2) removing the `if [ "${#PATHS[@]}" -eq 0 ]` guard together with `|| true`-ing the discovery
    call — the second one flips this test from pass to a genuine failure (`returncode == 0`, stdout
    ends "manifest-lint: OK (0 paths built and validated)"), proving this test would catch that
    class of silent-pass regression were it ever reintroduced."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture_repo = pathlib.Path(self._tmp.name) / "repo"
        (self.fixture_repo / "scripts").mkdir(parents=True)
        shutil.copy(_MOD_PATH, self.fixture_repo / "scripts" / "manifest-paths.py")
        shutil.copy(
            _MOD_PATH.parent / "manifest-lint.sh", self.fixture_repo / "scripts" / "manifest-lint.sh"
        )
        self.cluster_ai = self.fixture_repo / "kubernetes/apps/clusters/ai"
        self.cluster_ai.mkdir(parents=True)
        _write_flux_system_source(self.cluster_ai)

        # A `docker` stub so manifest-lint.sh's `command -v docker` precondition passes without
        # a real docker daemon; it only records each invocation's arguments (one line per call).
        self.stub_bin = pathlib.Path(self._tmp.name) / "stub-bin"
        self.stub_bin.mkdir()
        self.docker_marker = pathlib.Path(self._tmp.name) / "docker-was-invoked"
        docker_stub = self.stub_bin / "docker"
        docker_stub.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {self.docker_marker}\n"
            "exit 0\n"
        )
        docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_lint(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_bin}:{env.get('PATH', '')}"
        return subprocess.run(
            ["bash", "scripts/manifest-lint.sh"],
            cwd=self.fixture_repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _docker_calls(self) -> list[str]:
        if not self.docker_marker.exists():
            return []
        return [l for l in self.docker_marker.read_text().splitlines() if l]

    def test_broken_fixture_makes_manifest_lint_sh_exit_non_zero_before_any_docker_run(self):
        (self.cluster_ai / "broken.yaml").write_text(
            _kustomization_doc("broken", "./kubernetes/apps/does-not-exist")
        )
        result = self._run_lint()
        self.assertNotEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertIn("no kustomization.yaml found", result.stderr)
        self.assertFalse(
            self.docker_marker.exists(),
            "docker was invoked -- failure must happen at discovery, before any build/validate step",
        )

    def test_distinct_paths_that_flatten_to_the_same_slug_are_both_built_and_both_validated(self):
        # `./kubernetes/apps/a/b` and `./kubernetes/apps/a__b` used to flatten to the SAME
        # out/<slug>.yaml (`/` -> `__`), so the second build overwrote the first and kubeconform
        # validated one rendered manifest for two built paths — a silent coverage loss (round-2
        # codex finding, manifest-lint.sh:93/:96). Rendered filenames must be injective.
        for path in ("kubernetes/apps/a/b", "kubernetes/apps/a__b"):
            target = self.fixture_repo / path
            target.mkdir(parents=True)
            (target / "kustomization.yaml").write_text("resources: []\n")
        (self.cluster_ai / "ab.yaml").write_text(_kustomization_doc("ab", "./kubernetes/apps/a/b"))
        (self.cluster_ai / "a-b.yaml").write_text(_kustomization_doc("a-b", "./kubernetes/apps/a__b"))

        result = self._run_lint()
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        calls = self._docker_calls()
        builds = [c for c in calls if " build " in c]
        self.assertEqual(len(builds), 2, calls)
        rendered = sorted(p.name for p in (self.fixture_repo / "out").glob("*.yaml"))
        self.assertEqual(len(rendered), 2, rendered)
        validates = [c for c in calls if "-strict" in c]
        self.assertEqual(len(validates), 1, calls)
        self.assertEqual(validates[0].count("/out/"), 2, validates[0])
        self.assertIn("manifest-lint: OK (2 paths built and validated)", result.stdout)


if __name__ == "__main__":
    unittest.main()
