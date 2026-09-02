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
report.

SO A `sourceRef` IS RESOLVED, NOT STRING-MATCHED. `spec.sourceRef` is a
reference — `kind` + `name` + `namespace` (defaulting, as in Flux, to the
Kustomization's own `metadata.namespace`) — to a `GitRepository` object, and
only that object's `spec.url` says which repo the path lives in. Discovery
therefore first collects every `GitRepository` this tree declares (the
`clusters/ai/*.yaml` files plus `clusters/ai/flux-system/gotk-sync.yaml`,
where Flux's bootstrap `flux-system` GitRepository — url `.../cchifor/
ailab.git`, i.e. this repo — lives) into a `(namespace, name) -> url` table,
then classifies each Kustomization by looking its reference UP in that table:

  * LOCAL    — the resolved object's url identifies THIS repo (`cchifor/
               ailab`; `is_this_repo()` accepts the in-cluster Gitea url and
               the GitHub push-mirror url ADR 0017 keeps as the rollback,
               since both carry the same content). The path is built.
  * EXTERNAL — the resolved object's url is a different repo AND the object
               is a reviewed entry in `EXPECTED_EXTERNAL_SOURCES`, the closed
               allowlist of externally-sourced objects this step is ALLOWED
               to skip (currently exactly `agentforge-tenants` and
               `platform`). The path is excluded and the exclusion is printed
               to stderr so a CI log makes the gate's real coverage visible.
  * ANYTHING ELSE is a `DiscoveryError` — fail closed. That covers: no
               `sourceRef` at all; a `sourceRef` that is not a mapping or
               lacks `kind`/`name`; a kind other than `GitRepository`
               (OCIRepository/Bucket sources are not resolvable to a repo url
               here, and their first appearance is a reviewer's decision, not
               a silent skip); a reference no declared object satisfies (a
               typo'd name, another namespace, a Kustomization with no
               namespace of its own); the bootstrap `flux-system` object
               repointed at another repo (Flux would then apply content that
               is NOT this checkout, so building this checkout would validate
               the wrong thing); and a declared external object nobody added
               to the allowlist. Matching the name string alone (`name:
               flux-system` == local) would misclassify every one of those.

  Checking those other repos out so their paths could be built here too is
  explicitly out of scope for this PR: it would need cross-repo git
  credentials this runner is not provisioned with and multi-repo egress the
  spec's own risk section never anticipated (only registry.k8s.io/ghcr.io/
  raw.githubusercontent.com are named there); growing
  `EXPECTED_EXTERNAL_SOURCES` is a one-line, reviewed change.

PARSER: PyYAML if importable (safe_load_all — these are plain manifests),
else a stdlib-only fallback that this runner's own `.gitea/workflows/
manifests.yaml` never installs a dependency for (the fleet's runner image
ships a python3 with NO pip module — see tenant-guard-cel.yaml), so the
fallback is what actually runs there today. The fallback is NOT a regex over
a few expected lines: it is a small, STRICT block-mapping reader that derives
indentation from the document itself (any width), tolerates blank/comment
lines, key order, flow-style `sourceRef: {kind: .., name: ..}`, quoted and
comment-trailed scalars — and raises `DiscoveryError` on anything it cannot
resolve unambiguously (aliases/anchors, tags, block scalars, a flow-style
`spec:`/`metadata:`, an unterminated quote, a line it cannot read as a
mapping entry, a document with content but no top-level `kind:`). It never
silently skips a document: a Kustomization the fallback failed to see would
be a path that never gets built while the gate stays green, which is the one
outcome this file exists to prevent. Both parsers are exercised by
scripts/tests/test_manifest_paths.py; they must agree on every legal layout
the fallback accepts and on this repo's real manifest set (asserted).
`main()` prints which parser ran to stderr.

USAGE
  python3 scripts/manifest-paths.py     # one buildable path per line on stdout
Exit 0 = discovery succeeded (zero or more paths). Exit 1 = DiscoveryError
(any of the fail-closed cases above, a kept path with no kustomization.yaml,
or a locally-sourced Kustomization with no spec.path).
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

#: `<owner>/<repo>` of THIS repository. A GitRepository whose url ends in this slug (any scheme,
#: host, port, `.git` suffix or not — the in-cluster Gitea url in gotk-sync.yaml and the GitHub
#: push-mirror url ADR 0017 keeps as the rollback both do) is a source of THIS repo's content.
LOCAL_REPO_SLUG = "cchifor/ailab"

#: Reviewed, closed allowlist of (namespace, name) GitRepository objects that are known and
#: accepted to be externally sourced (see the module docstring) — the ONLY resolved sources
#: discovery is allowed to exclude without raising. An entry here must STILL be declared in the
#: tree and must STILL resolve to a non-local url; the allowlist never overrides resolution.
EXPECTED_EXTERNAL_SOURCES = frozenset(
    {
        ("flux-system", "agentforge-tenants"),
        ("flux-system", "platform"),
    }
)

#: Flux's own bootstrap manifest (generated; DO NOT EDIT): read ONLY for the `flux-system`
#: GitRepository declaration. Its Kustomization (path ./kubernetes/apps/clusters/ai) is Flux's
#: entry point into this tree, not a manifest under test.
_BOOTSTRAP_SYNC = Path("flux-system/gotk-sync.yaml")


class DiscoveryError(RuntimeError):
    """A Kustomization in scope could not be resolved to a buildable path (or excluded safely)."""


# --------------------------------------------------------------------------------------------
# Stdlib fallback parser: a strict reader for the block-mapping subset these manifests use.
# --------------------------------------------------------------------------------------------

_DOC_SPLIT_RE = re.compile(r"(?m)^---(?:[ \t].*)?$")
_KEY_LINE_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_.\-/]+):(?:[ \t]+(?P<value>.*?))?[ \t]*$")
_SEQ_ITEM_RE = re.compile(r"^ *-(?:[ \t]|$)")
_FLOW_ENTRY_RE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.\-/]+)\s*:\s*(?P<value>.*?)\s*$")
#: Leading characters that make a plain-looking scalar something this reader will not guess at:
#: anchor, alias, tag, block scalars, flow collections, directive, reserved indicators.
_NOT_A_PLAIN_SCALAR = "&*!|>[{%@`"


def _is_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(raw: str, where: str) -> str | None:
    """Resolve one scalar value the way YAML would for the plain/quoted subset; None for empty."""
    s = raw.strip()
    if not s or s.startswith("#"):
        return None
    if s[0] in "\"'":
        quote = s[0]
        end = s.find(quote, 1)
        if end < 0:
            raise DiscoveryError(f"{where}: unterminated quoted scalar {s!r}")
        rest = s[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            raise DiscoveryError(f"{where}: trailing content after quoted scalar {s!r}")
        return s[1:end]
    s = re.split(r"\s+#", s, maxsplit=1)[0].rstrip()
    if s[0] in _NOT_A_PLAIN_SCALAR:
        raise DiscoveryError(
            f"{where}: value {s!r} is not a plain scalar the stdlib fallback can resolve "
            "unambiguously (anchor/alias/tag/block scalar/flow collection) — install PyYAML or "
            "write it as a plain value"
        )
    return s


def _mapping(lines: list[str], start: int, stop: int, parent_indent: int, where: str) -> dict[str, list]:
    """Read the block mapping whose entries sit between lines[start:stop] at the first content
    line's indentation (which must exceed parent_indent). Returns key -> [indent, raw_value,
    child_start, child_end]; lines indented deeper than the entry (or sequence items at the
    entry's own indent) are that entry's children. Raises on anything not readable as an entry."""
    entries: dict[str, list] = {}
    i = start
    while i < stop and _is_blank(lines[i]):
        i += 1
    if i >= stop:
        return entries
    child_indent = _indent(lines[i])
    if child_indent <= parent_indent:
        return entries
    current: str | None = None
    while i < stop:
        line = lines[i]
        if _is_blank(line):
            i += 1
            continue
        if line.startswith("\t") or line.lstrip(" ").startswith("\t"):
            raise DiscoveryError(f"{where}: line {i + 1}: tab indentation is not valid YAML")
        indent = _indent(line)
        if indent < child_indent:
            break
        if indent > child_indent or _SEQ_ITEM_RE.match(line):
            if current is None:
                raise DiscoveryError(f"{where}: line {i + 1}: unexpected indentation: {line.strip()!r}")
            entries[current][3] = i + 1
            i += 1
            continue
        m = _KEY_LINE_RE.match(line)
        if not m:
            raise DiscoveryError(f"{where}: line {i + 1}: cannot read {line.strip()!r} as a mapping entry")
        key = m.group("key")
        if key in entries:
            raise DiscoveryError(f"{where}: line {i + 1}: duplicate key {key!r}")
        entries[key] = [indent, m.group("value") or "", i + 1, i + 1]
        current = key
        i += 1
    return entries


def _scalar_entry(lines: list[str], entry: list, where: str) -> str | None:
    _indent_, raw, child_start, child_end = entry
    if any(not _is_blank(l) for l in lines[child_start:child_end]):
        raise DiscoveryError(f"{where}: expected a scalar but found a nested block")
    return _scalar(raw, where)


def _flow_mapping(raw: str, where: str) -> dict[str, str | None]:
    body = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].rstrip()
    if not (body.startswith("{") and body.endswith("}")):
        raise DiscoveryError(f"{where}: {body!r} is neither a block mapping nor a flow mapping")
    inner = body[1:-1].strip()
    if not inner:
        return {}
    if any(c in inner for c in "{}[]"):
        raise DiscoveryError(f"{where}: nested flow collections are not supported by the stdlib fallback")
    out: dict[str, str | None] = {}
    for part in inner.split(","):
        m = _FLOW_ENTRY_RE.match(part)
        if not m:
            raise DiscoveryError(f"{where}: cannot read flow entry {part.strip()!r}")
        if m.group("key") in out:
            raise DiscoveryError(f"{where}: duplicate key {m.group('key')!r}")
        out[m.group("key")] = _scalar(m.group("value"), where)
    return out


def _mapping_entry(lines: list[str], entry: list, where: str) -> dict[str, str | None]:
    """A sub-mapping whose values are all scalars: block style (children) or flow style (inline)."""
    indent, raw, child_start, child_end = entry
    inline = raw.strip()
    if inline and not inline.startswith("#"):
        if any(not _is_blank(l) for l in lines[child_start:child_end]):
            raise DiscoveryError(f"{where}: both an inline value and a nested block")
        return _flow_mapping(raw, where)
    children = _mapping(lines, child_start, child_end, indent, where)
    return {k: _scalar_entry(lines, e, f"{where}.{k}") for k, e in children.items()}


def _block_section(lines: list[str], top: dict[str, list], key: str, where: str) -> dict[str, list]:
    """`metadata:` / `spec:` as block mappings (a flow-style section is refused, not guessed)."""
    if key not in top:
        return {}
    indent, raw, child_start, child_end = top[key]
    inline = raw.strip()
    if inline and not inline.startswith("#"):
        raise DiscoveryError(f"{where}: `{key}:` must be a block mapping (inline/flow style is not supported by the stdlib fallback)")
    return _mapping(lines, child_start, child_end, indent, f"{where}.{key}")


def _doc_via_fallback(raw: str, where: str) -> dict | None:
    """One `---`-separated document -> the normalized subset discovery needs, or None if the
    document has no content (blank/comment-only). Raises on anything not readable strictly."""
    lines = raw.split("\n")
    top = _mapping(lines, 0, len(lines), -1, where)
    if not top:
        return None
    if "kind" not in top:
        raise DiscoveryError(f"{where}: a document has content but no top-level `kind:` — refusing to classify it")
    kind = _scalar_entry(lines, top["kind"], f"{where}.kind")
    meta_entries = _block_section(lines, top, "metadata", where)
    spec_entries = _block_section(lines, top, "spec", where)
    metadata = {
        k: _scalar_entry(lines, meta_entries[k], f"{where}.metadata.{k}") for k in ("name", "namespace") if k in meta_entries
    }
    spec: dict = {k: _scalar_entry(lines, spec_entries[k], f"{where}.spec.{k}") for k in ("path", "url") if k in spec_entries}
    if "sourceRef" in spec_entries:
        spec["sourceRef"] = _mapping_entry(lines, spec_entries["sourceRef"], f"{where}.spec.sourceRef")
    return {"kind": kind, "metadata": metadata, "spec": spec}


def _docs_via_regex(text: str, where: str = "<text>") -> list[dict]:
    docs: list[dict] = []
    for raw in _DOC_SPLIT_RE.split(text):
        doc = _doc_via_fallback(raw, where)
        if doc is not None:
            docs.append(doc)
    return docs


def _docs_via_yaml(text: str, where: str = "<text>") -> list[dict]:
    try:
        loaded = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise DiscoveryError(f"{where}: not parseable as YAML: {exc}") from exc
    docs: list[dict] = []
    for d in loaded:
        if d is None:
            continue
        if not isinstance(d, dict):
            raise DiscoveryError(f"{where}: a document is not a mapping — refusing to classify it")
        if "kind" not in d:
            raise DiscoveryError(f"{where}: a document has content but no top-level `kind:` — refusing to classify it")
        metadata = d.get("metadata") or {}
        spec = d.get("spec") or {}
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise DiscoveryError(f"{where}: `metadata:`/`spec:` must be mappings")
        docs.append(
            {
                "kind": d["kind"],
                "metadata": {k: metadata.get(k) for k in ("name", "namespace") if k in metadata},
                "spec": {k: spec.get(k) for k in ("path", "url", "sourceRef") if k in spec},
            }
        )
    return docs


def _load_docs(manifest: Path) -> list[dict]:
    """Every document in `manifest`, normalized to {kind, metadata{name,namespace}, spec{path,url,
    sourceRef}} by whichever parser is available (see the module docstring)."""
    where = _rel(manifest)
    text = manifest.read_text()
    if yaml is not None:
        return _docs_via_yaml(text, where)
    return _docs_via_regex(text, where)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[/:]([^/:]+/[^/:]+?)(?:\.git)?/?$")


def is_this_repo(url: str | None) -> bool:
    """Does a GitRepository url identify THIS repository (LOCAL_REPO_SLUG), whatever the scheme,
    host or port — the in-cluster Gitea url and the GitHub push-mirror url alike?"""
    if not isinstance(url, str):
        return False
    m = _SLUG_RE.search(url.strip())
    return bool(m) and m.group(1).lower() == LOCAL_REPO_SLUG.lower()


def _source_manifests(cluster_ai: Path) -> list[Path]:
    manifests = sorted(cluster_ai.glob("*.yaml"))
    bootstrap = cluster_ai / _BOOTSTRAP_SYNC
    if bootstrap.exists():
        manifests.append(bootstrap)
    return manifests


def declared_git_repositories(cluster_ai: Path | None = None) -> dict[tuple[str, str], str]:
    """(namespace, name) -> spec.url for every GitRepository declared in `cluster_ai/*.yaml` and
    `cluster_ai/flux-system/gotk-sync.yaml` — the table a Kustomization's sourceRef is resolved
    against. A GitRepository without name/namespace/url, or declared twice, is a DiscoveryError."""
    if cluster_ai is None:
        cluster_ai = CLUSTER_AI
    sources: dict[tuple[str, str], str] = {}
    for manifest in _source_manifests(cluster_ai):
        for doc in _load_docs(manifest):
            if doc["kind"] != "GitRepository":
                continue
            name = doc["metadata"].get("name")
            namespace = doc["metadata"].get("namespace")
            url = doc["spec"].get("url")
            if not isinstance(name, str) or not isinstance(namespace, str) or not isinstance(url, str):
                raise DiscoveryError(
                    f"{_rel(manifest)}: GitRepository must declare metadata.name, metadata.namespace and spec.url "
                    f"(got name={name!r} namespace={namespace!r} url={url!r})"
                )
            key = (namespace, name)
            if key in sources:
                raise DiscoveryError(f"{_rel(manifest)}: GitRepository {namespace}/{name} is declared twice")
            sources[key] = url
    return sources


def _resolve_source(doc: dict, sources: dict[tuple[str, str], str], where: str) -> tuple[str, tuple[str, str], str]:
    """Resolve a Kustomization's sourceRef -> ("local" | "external", (namespace, name), url), or
    raise DiscoveryError for every case the module docstring lists as fail-closed."""
    ref = doc["spec"].get("sourceRef")
    if ref is None:
        raise DiscoveryError(f"{where}: Kustomization has no spec.sourceRef — cannot tell which repo its path lives in")
    if not isinstance(ref, dict):
        raise DiscoveryError(f"{where}: spec.sourceRef must be a mapping with kind/name, got {ref!r}")
    kind, name = ref.get("kind"), ref.get("name")
    if not isinstance(kind, str) or not isinstance(name, str):
        raise DiscoveryError(f"{where}: spec.sourceRef must carry both `kind:` and `name:` (got {ref!r})")
    if kind != "GitRepository":
        raise DiscoveryError(
            f"{where}: sourceRef kind {kind} is not resolvable to a repository url by this tool "
            "(only GitRepository is); a reviewer must decide how to gate it rather than have it skipped"
        )
    namespace = ref.get("namespace")
    if namespace is None:
        namespace = doc["metadata"].get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise DiscoveryError(
            f"{where}: neither spec.sourceRef.namespace nor metadata.namespace is set — the reference's "
            "namespace would be whatever the applier defaults to, which this tool refuses to guess"
        )
    key = (namespace, name)
    url = sources.get(key)
    if url is None:
        declared = ", ".join(f"{ns}/{n}" for ns, n in sorted(sources)) or "none"
        raise DiscoveryError(
            f"{where}: sourceRef GitRepository {namespace}/{name} is not declared anywhere discovery "
            f"reads (declared: {declared}) — an unresolvable reference cannot be classified"
        )
    if is_this_repo(url):
        return "local", key, url
    if key not in EXPECTED_EXTERNAL_SOURCES:
        raise DiscoveryError(
            f"{where}: sourceRef GitRepository {namespace}/{name} resolves to {url}, which is not this "
            f"repo ({LOCAL_REPO_SLUG}) and is not a reviewed entry in EXPECTED_EXTERNAL_SOURCES — "
            "refusing to silently drop it from the gate. Fix the sourceRef (or the GitRepository's url) "
            "if this was meant to resolve locally, or add the object to EXPECTED_EXTERNAL_SOURCES if it "
            "is a reviewed, accepted external source."
        )
    return "external", key, url


def discover_paths(cluster_ai: Path | None = None) -> list[str]:
    """Every spec.path of a Kustomization in `cluster_ai/*.yaml` (top level only) whose sourceRef
    RESOLVES to this repo (see the module docstring). Each accepted external exclusion is printed
    to stderr so it stays visible in a CI log; every other non-local case raises DiscoveryError.

    `cluster_ai` defaults to the CURRENT value of the module-level CLUSTER_AI global (read at call
    time, not import time) so tests can monkeypatch `manifest_paths.CLUSTER_AI` onto a fixture
    tree without needing to pass it through explicitly.
    """
    if cluster_ai is None:
        cluster_ai = CLUSTER_AI
    sources = declared_git_repositories(cluster_ai)
    paths: list[str] = []
    for manifest in sorted(cluster_ai.glob("*.yaml")):
        where = _rel(manifest)
        for doc in _load_docs(manifest):
            if doc["kind"] != "Kustomization":
                continue
            verdict, (namespace, name), url = _resolve_source(doc, sources, where)
            path = doc["spec"].get("path")
            if verdict == "local":
                if not isinstance(path, str) or not path:
                    raise DiscoveryError(f"{where}: a locally-sourced Kustomization has no spec.path")
                paths.append(path)
                continue
            print(
                f"manifest-paths.py: excluding {where} (sourceRef GitRepository {namespace}/{name} -> {url}) "
                f"path {path!r}: sourced from a different repo, not buildable in this checkout",
                file=sys.stderr,
            )
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
    parser = f"PyYAML {yaml.__version__}" if yaml is not None else "stdlib fallback (strict block-mapping reader)"
    print(f"manifest-paths.py: parser = {parser}", file=sys.stderr)
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
