#!/usr/bin/env python3
"""manifest-paths.py — discover every LOCALLY-BUILDABLE Flux Kustomization path.

THE SOURCE is `kubernetes/apps/clusters/ai/*.yaml` (top level only — NOT the
`flux-system/` subdirectory, which holds the bootstrap `GitRepository`/
`Kustomization` pair Flux itself generated and reconciles the whole tree
from, not a manifest under test here). Every `kind: Kustomization` document
in that directory names a `spec.path` this repo's own tooling can attempt to
`kustomize build`.

NOT EVERY ONE OF THOSE PATHS IS LOCAL, though. Two Kustomizations in that
directory (`agentforge-tenants`, `platform`) point `spec.sourceRef` at a
DIFFERENT `GitRepository` — the CP-written `cchifor/agentforge-tenants` repo
and the `cchifor/platform` repo, respectively — and `spec.path` is then a
path in THAT repo, not this one (`./tenants`, `./deploy/gitops/flux/clusters
/ailab`; neither directory exists in this checkout at all). Feeding either to
`kustomize build` here fails for a reason that has nothing to do with this
repo's manifests being wrong, which is not what a fail-closed gate should
report. So discovery keeps only Kustomizations whose `sourceRef` resolves to
THIS repo: `kind: GitRepository`, `name: flux-system` — verified against
`kubernetes/apps/clusters/ai/flux-system/gotk-sync.yaml`, whose `flux-system`
GitRepository's `url` is `.../cchifor/ailab.git`, i.e. this repo, self-
referentially. Every kept path is additionally asserted to contain a
`kustomization.yaml` (or `.yml`) — a `spec.path` with no such file is a
manifest bug this discovery step should catch, not a silent empty build.

PARSER: PyYAML if importable (safe_load_all — these are plain, anchor-free
manifests), else a stdlib-only regex fallback over `---`-separated documents
that this runner's own `.gitea/workflows/manifests.yaml` never installs a
dependency for, so the fallback is what actually runs there today. Both
parsers are exercised by scripts/tests/test_manifest_paths.py; they agree on
this repo's real manifest set (that agreement is itself asserted).

USAGE
  python3 scripts/manifest-paths.py     # one buildable path per line on stdout
Exit 0 = discovery succeeded (zero or more paths). Exit 1 = a Kustomization
in scope names no spec.path, or a kept path has no kustomization.yaml.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover — exercised by the regex-fallback tests
    yaml = None

REPO = Path(__file__).resolve().parents[1]
CLUSTER_AI = REPO / "kubernetes/apps/clusters/ai"

#: The GitRepository this repo's own bootstrap Kustomization reads from — see
#: kubernetes/apps/clusters/ai/flux-system/gotk-sync.yaml. A Kustomization
#: whose sourceRef does not match this is sourced from a DIFFERENT repo.
LOCAL_SOURCE_KIND = "GitRepository"
LOCAL_SOURCE_NAME = "flux-system"

_KIND_RE = re.compile(r"^kind:\s*Kustomization\s*$", re.MULTILINE)
# `sourceRef:`'s own children can appear in EITHER order (`kind:` then `name:`, or vice versa) and
# need not be adjacent (e.g. an intervening `namespace:` line) — real YAML has no ordering
# requirement on a mapping's keys, and treating this fallback's key order as significant would
# make it FAIL OPEN: a Kustomization the PyYAML parser resolves to `_is_local()` would silently
# vanish from `_docs_via_regex()`'s output instead of erroring, which is worse than a false
# failure. So first capture the whole indented block under `sourceRef:` (every following line
# indented at least as deep as its own children), then search `kind:`/`name:` inside that block
# independently of order or adjacency.
_SOURCE_REF_BLOCK_RE = re.compile(r"^  sourceRef:\n((?:^    \S.*\n?)+)", re.MULTILINE)
_SOURCE_REF_KIND_RE = re.compile(r"^\s*kind:\s*(?P<kind>\S+)\s*$", re.MULTILINE)
_SOURCE_REF_NAME_RE = re.compile(r"^\s*name:\s*(?P<name>\S+)\s*$", re.MULTILINE)
# `path:`'s value may be quoted (`path: "./kubernetes/apps/x"`) — captured whole (not just \S+ )
# and unquoted below, so this fallback does not confuse a quoted YAML string for a literal path
# containing quote characters (which PyYAML would never do).
_PATH_RE = re.compile(r"^  path:\s*(?P<path>.+?)\s*$", re.MULTILINE)


def _unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes, as YAML scalar parsing would."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


class DiscoveryError(RuntimeError):
    """A Kustomization in scope could not be resolved to a buildable path."""


def _docs_via_yaml(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _docs_via_regex(text: str) -> list[dict]:
    """Stdlib fallback: split on '---' document markers and pick the handful
    of top-level fields this module needs out of each Kustomization document
    with regexes, rather than a general YAML parse."""
    docs: list[dict] = []
    for raw in re.split(r"(?m)^---\s*$", text):
        if not _KIND_RE.search(raw):
            continue
        doc: dict = {"kind": "Kustomization"}
        block_m = _SOURCE_REF_BLOCK_RE.search(raw)
        if block_m:
            block = block_m.group(1)
            kind_m = _SOURCE_REF_KIND_RE.search(block)
            name_m = _SOURCE_REF_NAME_RE.search(block)
            if kind_m and name_m:
                doc["sourceRef"] = {"kind": kind_m.group("kind"), "name": name_m.group("name")}
        m = _PATH_RE.search(raw)
        if m:
            doc["path"] = _unquote(m.group("path"))
        docs.append(doc)
    return docs


def _kustomization_specs(text: str) -> list[dict]:
    """Return, per Kustomization document in `text`, a flat dict of the
    fields discovery needs: {"sourceRef": {"kind", "name"} | None, "path": str | None}.
    """
    specs: list[dict] = []
    if yaml is not None:
        for doc in _docs_via_yaml(text):
            if doc.get("kind") != "Kustomization":
                continue
            spec = doc.get("spec") or {}
            specs.append(
                {
                    "sourceRef": spec.get("sourceRef"),
                    "path": spec.get("path"),
                }
            )
    else:
        for doc in _docs_via_regex(text):
            specs.append({"sourceRef": doc.get("sourceRef"), "path": doc.get("path")})
    return specs


def _is_local(source_ref: dict | None) -> bool:
    if not source_ref:
        # No explicit sourceRef would mean Flux's own default (the Kustomization
        # controller requires sourceRef; treat missing as NOT local rather than guess).
        return False
    return (
        source_ref.get("kind") == LOCAL_SOURCE_KIND
        and source_ref.get("name") == LOCAL_SOURCE_NAME
    )


def discover_paths(cluster_ai: Path | None = None) -> list[str]:
    """Every spec.path of a Kustomization in `cluster_ai/*.yaml` (top level
    only) whose sourceRef resolves to this repo's own flux-system source.

    `cluster_ai` defaults to the CURRENT value of the module-level CLUSTER_AI
    global (read at call time, not import time) so tests can monkeypatch
    `manifest_paths.CLUSTER_AI` onto a fixture tree without needing to pass
    it through explicitly.
    """
    if cluster_ai is None:
        cluster_ai = CLUSTER_AI
    paths: list[str] = []
    for manifest in sorted(cluster_ai.glob("*.yaml")):
        text = manifest.read_text()
        for spec in _kustomization_specs(text):
            if not _is_local(spec["sourceRef"]):
                continue
            path = spec["path"]
            if not path:
                raise DiscoveryError(
                    f"{manifest.relative_to(REPO)}: a locally-sourced Kustomization has no spec.path"
                )
            paths.append(path)
    return paths


def verify_buildable(paths: list[str], repo: Path | None = None) -> None:
    """Fail closed: every discovered path must contain a kustomization.yaml.

    `repo` defaults to the CURRENT module-level REPO global (read at call
    time) for the same monkeypatch-friendly reason as discover_paths().
    """
    if repo is None:
        repo = REPO
    for path in paths:
        target = (repo / path).resolve()
        if not (target / "kustomization.yaml").exists() and not (
            target / "kustomization.yml"
        ).exists():
            raise DiscoveryError(f"{path}: no kustomization.yaml found at {target}")


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        paths = discover_paths()
        verify_buildable(paths)
    except DiscoveryError as exc:
        print(f"manifest-paths.py: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
