#!/usr/bin/env python3
"""Plan which large Relay release assets to delete (owner-approved retention, 2026-11-23).

    python3 scripts/relay_release_retention.py plan --releases <releases.json> [--keep-newest 5] [--keep vX.Y.Z ...]

Pure planning: reads the release list (the JSON array from GET /repos/cchifor/relay/releases, pages concatenated)
and this checkout's pins, and prints the plan. It never calls the forge; scripts/relay-release-retention.sh fetches
the list and deletes, and only an exact, owner-approved plan (its sha256) is ever executed.

The rule (docs/runbooks/relay.md, "Release asset retention"):
  * Only the large per-commit bundles are ever deleted: relay-codex-*, relay-pi-*, relay-holder-* (and their
    .partNNNN pieces). Every release object, tag and every other asset (manifests, SHA256SUMS, connector binaries,
    skill files, receipts) stays.
  * Kept whole: the newest KEEP_NEWEST releases by creation time, every version named in this checkout's Relay pins
    (PIN_PATHS), every --keep tag, and the releases in ALWAYS_KEEP.
  * v0.2.0 is not a Relay release in this sense: llm-router's deploy/publish.py uses it as its artifact store (router
    image tarballs and their immutable deployment receipts, which ailab runbooks link). It is never touched here.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = re.compile(r"^relay-(codex|pi|holder)-")
VERSION = re.compile(r"\bv\d+\.\d+\.\d+\b")
ALWAYS_KEEP = {"v0.2.0"}
KEEP_NEWEST = 5
# Where a deployed or rollback-relevant Relay version is named. A version mentioned anywhere here keeps its bundles.
PIN_PATHS = (
    "kubernetes/apps/apps/relay",
    "kubernetes/apps/relay-researcher",
    "kubernetes/components",
    "ansible/roles/relay_connector/defaults",
    "ansible/roles/relay_agent_skill",
    "ansible/group_vars",
    "ansible/host_vars",
)


def pinned_versions(root=ROOT, paths=PIN_PATHS):
    found = set()
    for rel in paths:
        base = root / rel
        files = [base] if base.is_file() else (sorted(base.rglob("*")) if base.is_dir() else [])
        for f in files:
            if f.is_file() and f.suffix in {".yaml", ".yml", ".json", ".j2", ".md", ".txt", ""}:
                try:
                    found.update(VERSION.findall(f.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
    return found


def plan(releases, pinned, keep_newest=KEEP_NEWEST, extra_keep=()):
    """-> (keep: {tag: reason}, items: [ {tag, release_id, asset_id, name, size} ]) in a stable order."""
    if keep_newest < 1:
        raise ValueError("keep_newest must be at least 1")
    ordered = sorted(releases, key=lambda r: (r["created_at"], r["id"]), reverse=True)
    keep = {}
    for r in ordered[:keep_newest]:
        keep.setdefault(r["tag_name"], "newest %d" % keep_newest)
    for t in sorted(pinned):
        keep.setdefault(t, "pinned in ailab")
    for t in extra_keep:
        keep.setdefault(t, "--keep")
    for t in ALWAYS_KEEP:
        keep[t] = "llm-router artifact store"
    items = []
    for r in ordered:
        if r["tag_name"] in keep or r.get("draft"):
            continue
        for a in r.get("assets") or []:
            if BUNDLE.match(a["name"]):
                items.append({"tag": r["tag_name"], "release_id": r["id"], "asset_id": a["id"],
                              "name": a["name"], "size": a["size"]})
    items.sort(key=lambda i: (i["tag"], i["name"], i["asset_id"]))
    return keep, items


def plan_sha256(items):
    body = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--releases", required=True, help="JSON array of releases (all pages)")
    p.add_argument("--keep-newest", type=int, default=KEEP_NEWEST)
    p.add_argument("--keep", action="append", default=[], help="an extra tag to keep (repeatable)")
    p.add_argument("--json-out", help="write the plan items here (for the executing wrapper)")
    a = ap.parse_args(argv)
    releases = json.loads(Path(a.releases).read_text(encoding="utf-8"))
    if not isinstance(releases, list) or not releases:
        print("no releases read; refusing to plan", file=sys.stderr)
        return 2
    keep, items = plan(releases, pinned_versions(), a.keep_newest, a.keep)
    sha = plan_sha256(items)
    total = sum(i["size"] for i in items)
    print("KEEP (bundles untouched):")
    for t, why in sorted(keep.items()):
        print("  %-10s %s" % (t, why))
    print("DELETE (bundle assets only; releases, tags and other assets stay):")
    per = {}
    for i in items:
        per.setdefault(i["tag"], []).append(i)
    for t in sorted(per):
        print("  %-10s %2d assets %8.1f MB" % (t, len(per[t]), sum(i["size"] for i in per[t]) / 1e6))
    print("TOTAL %d assets in %d releases, %.2f GB" % (len(items), len(per), total / 1e9))
    print("PLAN-SHA256 %s" % sha)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(items, sort_keys=True, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
