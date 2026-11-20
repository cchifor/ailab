#!/usr/bin/env python3
r"""Safety gate for a zot retention change: exit 1 if the policy would delete an image that is in use.

It simulates zot v2.1.18 retention (the version running on the registry LXC) for the policy the registry_zot role
renders, against the live registry, and checks every in-use image reference to registry.chifor.me/strive/... .

In-use references come from:
  - the live cluster: `kubectl --context admin@ai get pods -A -o json` and `get deploy,sts,ds,cronjob,job -A -o json`
    (pass the saved JSON with --kube-json). Every string under a pod's or workload's `spec` and every `image`/`imageID`
    under `status` is scanned, so init and ephemeral containers, imageIDs and env-var image refs all count;
  - files and directories passed with --scan (default: this repo's kubernetes/apps);
  - a platform checkout passed with --platform: its ailab Flux overlay and pin sources (PLATFORM_PIN_SOURCES below);
  - --refs-file: one reference per line, e.g. the platform's own `scripts/ci/list-ailab-pins.py` output.
A reference is `registry.chifor.me/strive/<repo>[:tag][@sha256:...]`. A bare `sha256:<64 hex>` in a scanned file (a
Helm `image.digest` pin) is matched against every strive repo: through their tags, else by a HEAD probe of each repo
(an untagged manifest). A bare digest never probed (a snapshot without the probe) fails closed.
Unreadable sources (a file or a directory) fail closed too.

Tags and their push times come from the registry API, read-only: /v2/_catalog, /v2/<repo>/tags/list, and the search
extension's GraphQL ImageList, whose PushTimestamp is the metaDB statistic retention sorts by. The policy is the role's
rendered storage.retention (jinja2 + PyYAML; --set KEY=YAML overrides a role variable), or --config <config.json>.

zot v2.1.18 semantics modelled (pkg/retention/retention.go, rules.go, candidate.go; pkg/storage/gc/gc.go):
  - the FIRST policy whose `repositories` doublestar glob matches the repo applies; without one, nothing is pruned;
  - a tag belongs to the FIRST keepTags entry with a pattern that matches it (Go regexp MatchString: unanchored);
    a tag no entry matches is deleted; an entry with no rules keeps every tag it matches;
  - the rules of ONE entry are OR-ed ("we retain candidates if any of the below rules are met"): each rule picks its
    candidates from all of the entry's tags and the union is kept. mostRecentlyPushedCount sorts by PushTimestamp,
    pushedWithin keeps PushTimestamp after now-d (both per DIGEST: re-tagging an old digest keeps its old time);
  - a tag without metaDB statistics is kept ("tag statistics not found");
  - a manifest stays while a kept tag points at it or a kept index lists it (identifyManifestsReferencedInIndex).
Go's sort is not stable, so tags tied at the count boundary are AMBIGUOUS; the gate treats them as deleted.

Exit codes: 0 = nothing in use would be deleted; 1 = an in-use reference would be deleted (or is ambiguous);
2 = the scan could not complete (registry, policy or input error): fail closed.

Run it in Docker from the repo root (python + PyYAML + jinja2), with the kubectl JSON saved first:
  kubectl --context admin@ai get pods -A -o json > /tmp/pods.json
  kubectl --context admin@ai get deploy,sts,ds,cronjob,job -A -o json > /tmp/workloads.json
  python3 scripts/zot-retention-pin-scan.py --kube-json /tmp/pods.json --kube-json /tmp/workloads.json \
      --platform ../platform --json /tmp/pin-scan.json
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ROLE = REPO_ROOT / "ansible" / "roles" / "registry_zot"
DEFAULT_SCAN = [REPO_ROOT / "kubernetes" / "apps"]
REGISTRY_HOST = "registry.chifor.me"
REPO_PREFIX = "strive/"

# The platform repo's ailab deployment and pin sources, the same set its scripts/ci/list-ailab-pins.py reads: the Flux
# overlay, the Helm provider values, the raw component manifests (workers, the realm-sync Job, CronJobs), the App
# template registry and the historical app-runner digests inside the airlock image, and the first-party images
# Airlock's Dockerfile copies from. deploy/releases/ (dated release records) and tests are NOT deployment sources.
PLATFORM_PIN_SOURCES = (
    "deploy/gitops/flux/clusters/ailab",
    "deploy/helm/values/providers/ailab.yaml",
    "deploy/helm/values/providers/ailab-gatekeeper.yaml",
    "deploy/helm/values/providers/ailab-s2s-registry.yaml",
    "deploy/components",
    "services/airlock/config",
    "services/airlock/src/app/core/config/domain.py",
    "services/airlock/Dockerfile",
)

# Vars the role template needs that come from SOPS at real render time. Placeholders: only storage.retention is read.
_PLACEHOLDER_VARS = {
    "registry_ci_password": "pin-scan-render-only",
    "registry_oidc_client_secret": "pin-scan-render-only",
}

_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
REF_RE = re.compile(
    re.escape(REGISTRY_HOST) + r"/(?P<repo>" + _COMPONENT + r"(?:/" + _COMPONENT + r")*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

TAG_ANNOTATION = "org.opencontainers.image.ref.name"
GRAPHQL_FIELDS = (
    "Tag Digest MediaType PushTimestamp LastPullTimestamp "
    "Referrers { Digest } Manifests { Digest ConfigDigest Referrers { Digest } Layers { Digest Size } }"
)


class ScanError(Exception):
    """The scan cannot reach a trustworthy answer (exit 2)."""


# ---------------------------------------------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------------------------------------------

def refs_from_text(text, source):
    """Image references in `text`: dicts {repo, tag, digest, source}. repo is None for a bare digest."""
    out, spans = [], []
    for m in REF_RE.finditer(text):
        repo = m.group("repo")
        spans.append(m.span())  # a digest inside any registry ref (strive or not) is not a bare pin
        if not repo.startswith(REPO_PREFIX):
            continue
        if m.group("tag") or m.group("digest"):
            out.append({"repo": repo, "tag": m.group("tag"), "digest": m.group("digest"), "source": source})
    for m in DIGEST_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            out.append({"repo": None, "tag": None, "digest": m.group(0), "source": source})
    return out


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _status_images(node):
    """`image` and `imageID` values anywhere under a status (container, init and ephemeral container statuses)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("image", "imageID") and isinstance(v, str):
                yield v
            else:
                yield from _status_images(v)
    elif isinstance(node, list):
        for v in node:
            yield from _status_images(v)


def refs_from_kube(doc, label):
    """References in a `kubectl get -o json` List (or a single object). Every string under each item's `spec`
    counts (containers, initContainers, ephemeralContainers, CronJob jobTemplates, env values), plus status images.
    metadata is skipped: last-applied annotations hold old specs."""
    items = doc.get("items") if isinstance(doc, dict) and "items" in doc else [doc]
    out = []
    for item in items or []:
        meta = item.get("metadata") or {}
        where = f"{label}:{item.get('kind', '?')}/{meta.get('namespace', '-')}/{meta.get('name', '?')}"
        for s in _strings(item.get("spec") or {}):
            if REGISTRY_HOST in s or "sha256:" in s:
                out.extend(r for r in refs_from_text(s, where) if r["repo"] is not None)
        for s in _status_images(item.get("status") or {}):
            out.extend(r for r in refs_from_text(s, where) if r["repo"] is not None)
    return out


def _iter_files(path):
    path = pathlib.Path(path)
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise ScanError(f"scan path does not exist: {path}")

    def unreadable(err):  # rglob would skip an unreadable subtree silently; a missing source must not PASS
        raise ScanError(f"cannot list reference source directory {err.filename} ({err.__class__.__name__})")

    for root, dirs, files in os.walk(path, onerror=unreadable):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            yield pathlib.Path(root) / name


def without_comments(text, name):
    """The part of a file that can deploy anything: comments are history (old pins, rollback notes), not use.
    YAML is parsed and only its keys and values are kept; Python loses its COMMENT tokens; a Dockerfile its `#`
    lines. YAML that does not parse (a Helm template) falls back to dropping `#` lines and ` #` tails."""
    suffix = pathlib.PurePath(name).suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
            return "\n".join(s for doc in yaml.safe_load_all(text) for s in _strings_and_keys(doc))
        except Exception:  # noqa: BLE001 - any parse failure takes the line-based fallback
            return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())
    if suffix == ".py":
        import io
        import tokenize
        try:
            return " ".join(t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
                            if t.type != tokenize.COMMENT)
        except (tokenize.TokenError, SyntaxError, IndentationError):
            return text
    if pathlib.PurePath(name).name.lower().startswith("dockerfile"):
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return text


def _strings_and_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str):
                yield k
            yield from _strings_and_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings_and_keys(v)
    elif isinstance(node, str):
        yield node


def refs_from_paths(paths, base=None):
    out = []
    for path in paths:
        for f in _iter_files(path):
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # a binary file holds no reference
            except OSError as err:  # an unread source must not pass silently
                raise ScanError(f"cannot read reference source {f} ({err.__class__.__name__})")
            label = str(f.relative_to(base)) if base and f.is_relative_to(base) else str(f)
            if REGISTRY_HOST in text or "sha256:" in text:
                out.extend(refs_from_text(without_comments(text, f.name), label))
    return out


def platform_paths(root):
    root = pathlib.Path(root)
    paths = [root / rel for rel in PLATFORM_PIN_SOURCES]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise ScanError("platform pin sources missing (wrong --platform checkout?): " + ", ".join(missing))
    return paths


# ---------------------------------------------------------------------------------------------------------------
# Policy (zot v2.1.18 retention)
# ---------------------------------------------------------------------------------------------------------------

def glob_regex(pattern):
    """doublestar Match for the subset zot configs here use: `**` alone, a trailing `/**` (which also matches the bare
    prefix), and `*`/`?` inside a segment. Anything else raises, so a new pattern cannot be silently mis-modelled."""
    if pattern == "**":
        return re.compile(r".*\Z", re.S)
    if re.search(r"[\[\]{}\\]", pattern):
        raise ScanError(f"repository glob not modelled: {pattern!r}")
    body, suffix = pattern, ""
    if pattern.endswith("/**"):
        body, suffix = pattern[:-3], r"(?:/.*)?"
    if "**" in body:
        raise ScanError(f"repository glob not modelled: {pattern!r}")
    rx = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in body)
    return re.compile(rx + suffix + r"\Z", re.S)


def repo_policy(policies, repo):
    """The first policy whose repositories match `repo` (getRepoPolicy), or None."""
    for policy in policies:
        if any(glob_regex(p).match(repo) for p in policy.get("repositories") or []):
            return policy
    return None


def tag_entry(keep_tags, tag):
    """(index, entry) of the first keepTags entry matching `tag` (getTagPolicy), or (None, None) = deleted."""
    for i, entry in enumerate(keep_tags):
        patterns = entry.get("patterns") or []
        if not patterns or any(re.search(p, tag) for p in patterns):
            return i, entry
    return None, None


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_NS = {"ns": 1, "us": 10**3, "µs": 10**3, "ms": 10**6, "s": 10**9, "m": 60 * 10**9, "h": 3600 * 10**9}


def parse_duration_ns(value):
    """A Go duration string ("336h", "1h30m") in nanoseconds."""
    if not isinstance(value, str) or not value or _DURATION_RE.sub("", value) != "":
        raise ScanError(f"not a Go duration: {value!r}")
    return int(sum(float(n) * _UNIT_NS[u] for n, u in _DURATION_RE.findall(value)))


def parse_time_ns(value):
    """RFC 3339 with up to nanoseconds -> integer ns since the epoch (exact, for tie detection). Zero/absent -> 0."""
    if not value:
        return 0
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", value)
    if not m:
        raise ScanError(f"unparseable timestamp: {value!r}")
    base = datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(3) == "Z" else m.group(3)))
    secs = int(base.timestamp())
    frac = int((m.group(2) or "0").ljust(9, "0"))
    return max(0, secs * 10**9 + frac) if base.year > 1970 else 0


def _top_count(cands, n, key):
    """Candidates a mostRecently*Count rule keeps, and those tied across its boundary (Go's sort is not stable)."""
    if n <= 0:
        return set(), set()
    if len(cands) <= n:
        return {c["tag"] for c in cands}, set()
    ordered = sorted(cands, key=lambda c: -c[key])
    cut = ordered[n - 1][key]
    above = {c["tag"] for c in ordered if c[key] > cut}
    tied = {c["tag"] for c in ordered if c[key] == cut}
    if len(above) + len(tied) <= n:
        return above | tied, set()
    return above, tied


def simulate_repo(policy, tags, now_ns):
    """Decide every tag of one repo. `tags`: {tag: {digest, push, pull, stats}} where stats=False means zot has no
    statistics for it. Returns {tag: (decision, reason)} with decision in keep / delete / ambiguous."""
    if policy is None or not policy.get("keepTags"):
        return {t: ("keep", "no tag retention for this repo") for t in tags}
    keep_tags = policy["keepTags"]
    out, groups = {}, {}
    for tag, info in tags.items():
        if not info.get("stats", True):
            out[tag] = ("keep", "tag statistics not found")
            continue
        idx, _ = tag_entry(keep_tags, tag)
        if idx is None:
            out[tag] = ("delete", "matches no keepTags entry")
        else:
            groups.setdefault(idx, []).append({"tag": tag, **info})
    for idx, cands in groups.items():
        entry = keep_tags[idx]
        label = f"keepTags[{idx}] {entry.get('patterns')}"
        rules = []
        if entry.get("mostRecentlyPulledCount"):
            rules.append(("mostRecentlyPulledCount", *_top_count(cands, entry["mostRecentlyPulledCount"], "pull")))
        if entry.get("mostRecentlyPushedCount"):
            rules.append(("mostRecentlyPushedCount", *_top_count(cands, entry["mostRecentlyPushedCount"], "push")))
        if entry.get("pulledWithin") is not None:
            cutoff = now_ns - parse_duration_ns(entry["pulledWithin"])
            rules.append(("pulledWithin", {c["tag"] for c in cands if c["pull"] > cutoff or c["push"] > cutoff},
                          set()))
        if entry.get("pushedWithin") is not None:
            cutoff = now_ns - parse_duration_ns(entry["pushedWithin"])
            rules.append(("pushedWithin", {c["tag"] for c in cands if c["push"] > cutoff}, set()))
        for c in cands:
            if not rules:
                out[c["tag"]] = ("keep", f"{label}: patterns only")
                continue
            kept_by = [name for name, kept, _ in rules if c["tag"] in kept]
            if kept_by:
                out[c["tag"]] = ("keep", f"{label}: " + " + ".join(kept_by))
            elif any(c["tag"] in tied for _, _, tied in rules):
                out[c["tag"]] = ("ambiguous", f"{label}: tied at the count boundary")
            else:
                out[c["tag"]] = ("delete", f"{label}: outside every rule")
    return out


def render_role_retention(role_dir, overrides):
    """storage.retention as the registry_zot role renders it (defaults + overrides), like the role's tests do."""
    try:
        import jinja2
        import yaml
    except ImportError as err:  # pragma: no cover - environment problem
        raise ScanError(f"rendering the role needs jinja2 and PyYAML ({err})")
    role_dir = pathlib.Path(role_dir)
    context = yaml.safe_load((role_dir / "defaults" / "main.yml").read_text())
    context.update(_PLACEHOLDER_VARS)
    context.update(overrides)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = ansible_bool
    text = env.from_string((role_dir / "templates" / "config.json.j2").read_text()).render(**context)
    return json.loads(text)["storage"]["retention"]


def ansible_bool(value):
    """Ansible's `bool` filter: a string such as "true"/"no" is parsed, not taken as truthy."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
    return value in ("yes", "on", "1", "true", 1)


def parse_set(items):
    try:
        import yaml
    except ImportError as err:  # pragma: no cover
        raise ScanError(f"--set needs PyYAML ({err})")
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ScanError(f"--set wants KEY=VALUE, got {item!r}")
        out[key] = yaml.safe_load(value)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Registry (read-only)
# ---------------------------------------------------------------------------------------------------------------

class Registry:
    def __init__(self, base, timeout=120):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _get(self, path, data=None, method=None):
        headers = {"User-Agent": "zot-retention-pin-scan"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        for attempt in (1, 2, 3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as err:
                if err.code < 500 or attempt == 3:
                    return err.code, err.read()
            except OSError as err:
                if attempt == 3:
                    raise ScanError(f"registry unreachable: {path} ({err})")
            time.sleep(2 * attempt)
        raise ScanError(f"registry unreachable: {path}")  # pragma: no cover

    def catalog(self):
        status, body = self._get("/v2/_catalog?n=100000")
        if status != 200:
            raise ScanError(f"/v2/_catalog answered {status}")
        return json.loads(body).get("repositories") or []

    def tags(self, repo):
        status, body = self._get(f"/v2/{repo}/tags/list?n=100000")
        if status == 404:
            return []
        if status != 200:
            raise ScanError(f"tags/list for {repo} answered {status}")
        return json.loads(body).get("tags") or []

    def images(self, repo):
        query = '{ ImageList(repo: %s, requestedPage: {limit: 0, offset: 0}) { Page { TotalCount } Results { %s } } }' % (
            json.dumps(repo), GRAPHQL_FIELDS)
        status, body = self._get("/v2/_zot/ext/search", data=json.dumps({"query": query}).encode())
        if status != 200:
            raise ScanError(f"GraphQL ImageList for {repo} answered {status}")
        doc = json.loads(body)
        if doc.get("errors"):
            raise ScanError(f"GraphQL ImageList for {repo}: {doc['errors'][:1]}")
        return doc["data"]["ImageList"]["Results"]

    def manifest_exists(self, repo, digest):
        accept = ", ".join((
            "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json"))
        req = urllib.request.Request(f"{self.base}/v2/{repo}/manifests/{digest}", method="HEAD",
                                     headers={"Accept": accept, "User-Agent": "zot-retention-pin-scan"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            raise ScanError(f"HEAD {repo}@{digest} answered {err.code}")
        except OSError as err:
            raise ScanError(f"HEAD {repo}@{digest}: {err}")


def load_repo(registry, repo):
    """Tag table for one repo: {tag: {digest, push, pull, stats, children, referrers, layers}}."""
    listed = set(registry.tags(repo))
    images = registry.images(repo)
    tags = {}
    for img in images:
        tag = img.get("Tag")
        if not tag:
            continue
        # referrers per subject: the index itself ("") and each platform child (buildx attests the child)
        referrers = {"": sorted({r["Digest"] for r in img.get("Referrers") or []})}
        children, layers = [], {}
        for m in img.get("Manifests") or []:
            if m.get("Digest") and m["Digest"] != img["Digest"]:
                children.append(m["Digest"])
                referrers[m["Digest"]] = sorted({r["Digest"] for r in m.get("Referrers") or []})
            else:  # a single-platform image summarises itself
                referrers[""] = sorted(set(referrers[""]) | {r["Digest"] for r in m.get("Referrers") or []})
            for layer in m.get("Layers") or []:
                layers[layer["Digest"]] = int(layer.get("Size") or 0)
        tags[tag] = {"digest": img["Digest"], "push": parse_time_ns(img.get("PushTimestamp")),
                     "pull": parse_time_ns(img.get("LastPullTimestamp")), "stats": True,
                     "children": children, "referrers": referrers, "layers": layers}
    for tag in listed - set(tags):  # in index.json but not in metaDB: zot keeps these
        tags[tag] = {"digest": None, "push": 0, "pull": 0, "stats": False, "children": [], "referrers": {},
                     "layers": {}}
    return tags


# ---------------------------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------------------------

def evaluate(repos, refs):
    """Join decisions and references. `repos`: {repo: {"tags": tag table, "decisions": {tag: (decision, reason)}}}.
    Returns (failures, warnings, notes) - lists of dicts with a `ref` and a `why`."""
    surviving, doomed, maybe = {}, {}, {}
    digest_repos = {}
    for repo, data in repos.items():
        s, d, m = set(), set(), set()
        for tag, info in data["tags"].items():
            if not info["digest"]:
                continue
            decision = data["decisions"][tag][0]
            group = s if decision == "keep" else m if decision == "ambiguous" else d
            group.add(info["digest"])
            group.update(info["children"])
            for dg in [info["digest"], *info["children"]]:
                digest_repos.setdefault(dg, set()).add(repo)
        surviving[repo], doomed[repo], maybe[repo] = s, d - s - m, m - s

    failures, warnings, notes = [], [], []
    for ref in refs:
        label = _ref_label(ref)
        if ref["repo"] is None:
            hits = sorted(digest_repos.get(ref["digest"], ()))
            if hits:
                targets = [(r, None, ref["digest"], ref.get("exists")) for r in hits]
            elif ref.get("found_in") is None:  # a bare pin no tag holds, never probed: it could be first-party
                failures.append({"ref": label, "source": ref["source"],
                                 "why": "bare digest no strive tag holds; its presence in the strive repos is unknown"})
                continue
            elif not ref["found_in"]:
                notes.append({"ref": label, "source": ref["source"],
                              "why": "bare digest is in no strive repo (a third-party pin, or not in the registry)"})
                continue
            else:  # an untagged strive manifest pinned by a bare digest (a Helm image.digest)
                targets = [(r, None, ref["digest"], True) for r in ref["found_in"]]
        else:
            targets = [(ref["repo"], ref["tag"], ref["digest"], ref.get("exists"))]
        for repo, tag, digest, exists in targets:
            data = repos.get(repo)
            if data is None:
                warnings.append({"ref": label, "source": ref["source"], "why": f"{repo} is not a scanned repository"})
                continue
            if tag is not None and digest is None:  # with a digest, the digest is what gets pulled, not the tag
                if tag not in data["tags"]:
                    warnings.append({"ref": label, "source": ref["source"], "why": f"tag {tag} does not exist"})
                else:
                    decision, reason = data["decisions"][tag]
                    if decision != "keep":
                        failures.append({"ref": label, "source": ref["source"],
                                         "why": f"tag {repo}:{tag} would be {decision}d ({reason})"})
            if digest is not None:
                if digest in surviving[repo]:
                    continue
                if digest in doomed[repo] or digest in maybe[repo]:
                    state = "deleted" if digest in doomed[repo] else "ambiguous"
                    failures.append({"ref": label, "source": ref["source"],
                                     "why": f"{repo}@{digest[:19]} would be {state} (only its doomed tags hold it)"})
                elif exists is False:
                    warnings.append({"ref": label, "source": ref["source"], "why": "digest is not in the registry"})
                elif deletes_untagged(data.get("policy")):
                    # no tag and no kept index holds it: GC collects an untagged manifest in this repo (or its
                    # existence could not be confirmed): fail closed
                    seen = "exists untagged" if exists else "untagged, existence unknown"
                    failures.append({"ref": label, "source": ref["source"],
                                     "why": f"{repo}@{digest[:19]} {seen}, and the repo's policy deletes untagged "
                                            "manifests (deleteUntagged)"})
    return failures, warnings, notes


def deletes_untagged(policy):
    """zot v2.1.18 HasDeleteUntagged: the policy's deleteUntagged, true when unset or when no policy matches."""
    if policy is None or policy.get("deleteUntagged") is None:
        return True
    return bool(policy["deleteUntagged"])


def _ref_label(ref):
    if ref["repo"] is None:
        return ref["digest"]
    out = f"{REGISTRY_HOST}/{ref['repo']}"
    if ref["tag"]:
        out += f":{ref['tag']}"
    if ref["digest"]:
        out += f"@{ref['digest']}"
    return out


def totals(repos):
    """Per repo: tag counts by decision, images (digests) freed, manifests deleted (index + children + referrers),
    and layer bytes no surviving strive image uses (an UPPER bound: base layers shared with mirror repos stay)."""
    surviving_layers = set()
    per = {}
    for repo, data in repos.items():
        keep_d, gone_d = set(), set()
        for tag, info in data["tags"].items():
            if info["digest"]:
                (keep_d if data["decisions"][tag][0] == "keep" else gone_d).add(info["digest"])
        gone_d -= keep_d
        keep_children = {c for t, i in data["tags"].items() if i["digest"] in keep_d for c in i["children"]}
        manifests, layers = set(), {}
        for tag, info in data["tags"].items():
            if info["digest"] in keep_d:
                surviving_layers.update(info["layers"])
            elif info["digest"] in gone_d:
                manifests.add(info["digest"])
                manifests.update(info["referrers"].get("", ()))
                for child in info["children"]:
                    if child not in keep_children:  # a child a kept index lists stays, and so do its referrers
                        manifests.add(child)
                        manifests.update(info["referrers"].get(child, ()))
                layers.update(info["layers"])
        counts = {"keep": 0, "delete": 0, "ambiguous": 0}
        for decision, _ in data["decisions"].values():
            counts[decision] += 1
        per[repo] = {"tags": counts, "images": len(gone_d), "manifests": len(manifests), "layers": layers}
    seen = {}
    for repo, row in per.items():
        row["bytes"] = sum(sz for dg, sz in row["layers"].items() if dg not in surviving_layers)
        for dg, sz in row.pop("layers").items():
            if dg not in surviving_layers:
                seen[dg] = sz
    return per, sum(seen.values())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--registry", default=f"https://{REGISTRY_HOST}")
    ap.add_argument("--prefix", default=REPO_PREFIX, help="repositories to evaluate (catalog prefix)")
    ap.add_argument("--role", default=str(DEFAULT_ROLE), help="registry_zot role to render the policy from")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=YAML", help="override a role variable")
    ap.add_argument("--config", help="a rendered zot config.json to take storage.retention from (instead of --role)")
    ap.add_argument("--kube-json", action="append", default=[], help="saved `kubectl get ... -o json` output")
    ap.add_argument("--scan", action="append", default=None, help="file or directory to scan for references")
    ap.add_argument("--platform", help="platform repo checkout: scan its ailab overlay and pin sources")
    ap.add_argument("--refs-file", action="append", default=[], help="extra references, one per line")
    ap.add_argument("--now", help="evaluate at this RFC 3339 time instead of now (default: now, or the snapshot's)")
    ap.add_argument("--snapshot-out", help="save the registry listing (tags, push times, children) as JSON")
    ap.add_argument("--snapshot-in", help="replay a saved listing instead of querying the registry")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--top", type=int, default=12, help="largest repos to list")
    args = ap.parse_args(argv)
    try:
        return _run(args)
    except ScanError as err:
        print(f"FAIL-CLOSED: {err}", file=sys.stderr)
        return 2
    except Exception as err:  # noqa: BLE001 - exit 1 must only ever mean "an in-use image would be deleted"
        print(f"FAIL-CLOSED: {err.__class__.__name__}: {err}", file=sys.stderr)
        return 2


def _run(args):
    now_ns = parse_time_ns(args.now) if args.now else time.time_ns()
    if args.config:
        retention = json.loads(pathlib.Path(args.config).read_text())["storage"]["retention"]
    else:
        retention = render_role_retention(args.role, parse_set(args.set))
    policies = retention.get("policies") or []

    refs = []
    for path in args.kube_json:
        refs.extend(refs_from_kube(json.loads(pathlib.Path(path).read_text(encoding="utf-8")),
                                   pathlib.Path(path).name))
    scan = args.scan if args.scan is not None else [str(p) for p in DEFAULT_SCAN]
    refs.extend(refs_from_paths(scan, base=REPO_ROOT))
    if args.platform:
        refs.extend(refs_from_paths(platform_paths(args.platform), base=pathlib.Path(args.platform).resolve()))
    for path in args.refs_file:
        refs.extend(refs_from_text(pathlib.Path(path).read_text(encoding="utf-8"), pathlib.Path(path).name))
    # one entry per distinct reference; keep the first source and count the rest
    unique = {}
    for ref in refs:
        key = (ref["repo"], ref["tag"], ref["digest"])
        if key in unique:
            unique[key]["sources"] += 1
        else:
            unique[key] = {**ref, "sources": 1}
    refs = list(unique.values())

    registry, exists = None, {}
    if args.snapshot_in:  # replay a saved registry listing offline (policy what-ifs, tests)
        snap = json.loads(pathlib.Path(args.snapshot_in).read_text())
        tables, exists = snap["repos"], snap.get("exists") or {}
        if not args.now:
            now_ns = parse_time_ns(snap["taken_at"])
    else:
        registry = Registry(args.registry)
        names = sorted(r for r in registry.catalog() if r.startswith(args.prefix))
        if not names:
            raise ScanError(f"no {args.prefix}* repositories in the catalog")
        tables = {name: load_repo(registry, name) for name in names}
    repos = {}
    for name, tags in tables.items():
        policy = repo_policy(policies, name)
        repos[name] = {"tags": tags, "policy": policy, "decisions": simulate_repo(policy, tags, now_ns)}

    for ref in refs:  # existence check for in-use digests no tag holds (rare; tells "missing" from "untagged")
        if ref["repo"] in repos and ref["digest"]:
            held = any(ref["digest"] in (i["digest"], *i["children"]) for i in repos[ref["repo"]]["tags"].values())
            key = f"{ref['repo']}@{ref['digest']}"
            if not held and key not in exists and registry is not None:
                exists[key] = registry.manifest_exists(ref["repo"], ref["digest"])
            if not held and key in exists:
                ref["exists"] = exists[key]
    # A bare digest (a Helm image.digest) that no strive tag holds may still be an untagged strive manifest: probe
    # every strive repo for it (HEAD: read-only, and zot updates no statistics on HEAD).
    held_anywhere = {dg for tags in tables.values() for i in tags.values()
                     for dg in ([i["digest"]] if i["digest"] else []) + i["children"]}
    for ref in refs:
        if ref["repo"] is None and ref["digest"] not in held_anywhere:
            key = f"bare:{ref['digest']}"
            if key not in exists and registry is not None:
                exists[key] = [name for name in tables if registry.manifest_exists(name, ref["digest"])]
            ref["found_in"] = exists.get(key)  # None (a snapshot without the probe): unknown, fails closed

    failures, warnings, notes = evaluate(repos, refs)
    per, freed = totals(repos)
    when = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.snapshot_out:
        pathlib.Path(args.snapshot_out).write_text(json.dumps({"taken_at": when, "repos": tables, "exists": exists}))
    _report(repos, refs, per, freed, failures, warnings, notes, when, args.top)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({
            "evaluated_at": when, "retention": retention, "failures": failures, "warnings": warnings,
            "notes": notes, "per_repo": per, "freed_bytes_upper_bound": freed,
            "decisions": {r: {t: {"decision": d[0], "reason": d[1], "digest": repos[r]["tags"][t]["digest"],
                                  "push_ns": repos[r]["tags"][t]["push"]}
                              for t, d in data["decisions"].items()} for r, data in repos.items()},
        }, indent=1, sort_keys=True))
    return 1 if failures else 0


def _report(repos, refs, per, freed, failures, warnings, notes, when, top):
    by_source = {}
    for ref in refs:
        kind = "bare digest" if ref["repo"] is None else "tag+digest" if ref["tag"] and ref["digest"] else \
            "digest" if ref["digest"] else "tag"
        by_source[kind] = by_source.get(kind, 0) + 1
    print(f"zot retention pin scan, evaluated at {when}")
    print(f"in-use references: {len(refs)} distinct ({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))})")
    total = {"keep": 0, "delete": 0, "ambiguous": 0}
    images = manifests = 0
    for row in per.values():
        for k in total:
            total[k] += row["tags"][k]
        images += row["images"]
        manifests += row["manifests"]
    print(f"repositories: {len(repos)}; tags keep {total['keep']}, delete {total['delete']}, "
          f"ambiguous {total['ambiguous']}; images freed {images}; manifests deleted ~{manifests} "
          f"(index + platform children + referrers); layer bytes freed <= {freed / 1e9:.1f} GB")
    print(f"largest repos by would-delete tags (top {top}):")
    print(f"  {'repository':34} {'tags':>5} {'keep':>5} {'del':>5} {'amb':>4} {'manifests':>9} {'GB<=':>6}")
    ranked = sorted(per.items(), key=lambda kv: (-kv[1]["tags"]["delete"], kv[0]))
    for repo, row in ranked[:top]:
        n = sum(row["tags"].values())
        print(f"  {repo:34} {n:5d} {row['tags']['keep']:5d} {row['tags']['delete']:5d} "
              f"{row['tags']['ambiguous']:4d} {row['manifests']:9d} {row['bytes'] / 1e9:6.1f}")
    for title, rows in (("FAIL", failures), ("WARN", warnings)):
        for row in rows:
            print(f"{title}: {row['ref']}  [{row['source']}]  {row['why']}")
    if notes:
        print(f"note: {len(notes)} bare digest(s) are in no strive repo (third-party pins, or not in the registry):")
        for row in notes:
            print(f"  {row['ref'][:19]}  [{row['source']}]")
    print("RESULT: " + ("FAIL - an in-use image would be deleted" if failures else "PASS - nothing in use is deleted"))


if __name__ == "__main__":
    sys.exit(main())
