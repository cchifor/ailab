#!/usr/bin/env python3
"""Plan which large Relay release assets to delete (owner-approved retention, 2026-11-23).

    python3 scripts/relay_release_retention.py plan --releases <releases.json> [--keep-newest 5] [--keep vX.Y.Z ...]
    python3 scripts/relay_release_retention.py verify --before <releases.json> --plan <plan.json> --after <release.json>

`plan` reads the release list (every page of GET /repos/cchifor/relay/releases) and this checkout's pins, and prints
the plan: every asset it would delete, by release id, asset id and JSON-escaped name, and the plan's sha256. `verify`
checks one release re-read after execution: its assets must be exactly the ones it had minus the planned ones.
Neither calls the forge; scripts/relay-release-retention.sh fetches, and deletes only an owner-approved plan (its sha).

The rule (docs/runbooks/relay.md, "Release asset retention"):
  * Only the three per-commit bundle files are ever deleted (BUNDLE, exact names). Every release object, tag and other
    asset (manifests, SHA256SUMS, receipts, connector binaries, skill files, agent tools) stays.
  * Kept whole: the newest KEEP_NEWEST (at least 5) releases by creation time, every version named in this checkout's
    pin sources (PIN_PATHS), every --keep tag, and ALWAYS_KEEP.
  * v0.2.0 is llm-router's artifact store (its deploy/publish.py: router image tarballs and the deployment receipts the
    runbooks link), not a Relay release in this sense. Never touched.
  * Fail closed: a missing pin source or an unreadable file means no plan at all.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA40 = r"[0-9a-f]{40}"
# Exactly the published bundle names (surveyed 2026-11-23 across all 47 releases): never a receipt or a .sha256.
BUNDLE = re.compile(r"^relay-(?:codex-linux-x64-%s\.tar\.gz\.part-\d{4}|pi-linux-x64-%s\.tar\.gz|holder-%s\.tar\.gz)$"
                    % (SHA40, SHA40, SHA40))
# A version as pins write it, with or without the `v` (`relay_connector_release.version: 0.10.11`), with any prerelease
# suffix, normalised to the tag. Not a fragment of a longer dotted run (`192.168.0.36` is no version).
VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)(?![.\d])")
ALWAYS_KEEP = {"v0.2.0"}
KEEP_NEWEST = 5   # the approved minimum
# Every place a deployed, rollback or worker-runtime Relay version is named. A version mentioned anywhere here keeps its
# bundles. Broad on purpose: a version kept by mistake costs ~300 MB; one deleted by mistake costs a rebuild.
PIN_PATHS = (
    "kubernetes/apps",
    "kubernetes/components",
    "ansible",
    "docs/relay-workers",   # researcher-binding-inputs.json agentRelease.tag, runtime-pins.json
)


class PinReadError(Exception):
    """A pin source is missing or a file under it cannot be read: its versions would silently lose protection."""


def _walk_files(base):
    """Every file under base. os.walk with an onerror that raises: Path.rglob() silently skips a subdirectory it
    cannot list, which would drop that subtree's pins without stopping the plan (reviewer-codex, #1957 round 3)."""
    def fail(err):
        raise PinReadError("cannot list %s: %s" % (getattr(err, "filename", base), err))
    out = []
    for dirpath, _dirs, files in os.walk(base, onerror=fail, followlinks=False):
        out.extend(Path(dirpath) / f for f in files)
    return sorted(out)


def pinned_versions(root=ROOT, paths=PIN_PATHS):
    found = set()
    for rel in paths:
        base = root / rel
        if not base.exists():
            raise PinReadError("pin path missing: %s (renamed? update PIN_PATHS)" % rel)
        files = [base] if base.is_file() else _walk_files(base)
        for f in files:   # every file, whatever its suffix
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise PinReadError("cannot read %s: %s" % (f, e)) from e
            found.update("v" + m for m in VERSION.findall(text))
    return found


def plan(releases, pinned, keep_newest=KEEP_NEWEST, extra_keep=()):
    """-> (keep: {tag: reason}, items: [ {tag, release_id, asset_id, name, size} ]) in a stable order."""
    if keep_newest < KEEP_NEWEST:
        raise ValueError("keep_newest must be at least %d (the approved rule)" % KEEP_NEWEST)
    ordered = sorted(releases, key=lambda r: (r["created_at"], r["id"]), reverse=True)
    published = [r for r in ordered if not r.get("draft") and not r.get("prerelease")]
    tags = {r["tag_name"] for r in releases}
    unknown = [t for t in extra_keep if t not in tags]
    if unknown:   # a typo would leave the tag it meant unprotected
        raise ValueError("--keep names no release: %s" % ", ".join(unknown))
    keep = {}
    for r in published[:keep_newest]:   # five real releases: drafts and prereleases take no slot
        keep.setdefault(r["tag_name"], "newest %d" % keep_newest)
    for t in sorted(pinned):
        keep.setdefault(t, "pinned in ailab")
    for t in extra_keep:
        keep.setdefault(t, "--keep")
    for t in ALWAYS_KEEP:
        keep[t] = "llm-router artifact store"
    items = []
    for r in ordered:
        if r["tag_name"] in keep or r.get("draft") or r.get("prerelease"):
            continue
        for a in r.get("assets") or []:
            if BUNDLE.match(a["name"]):
                items.append({"tag": r["tag_name"], "release_id": int(r["id"]), "asset_id": int(a["id"]),
                              "name": a["name"], "size": int(a["size"])})
    items.sort(key=lambda i: (i["tag"], i["name"], i["asset_id"]))
    return keep, items


def plan_sha256(items):
    body = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def verify(before, plan_items, after):
    """One re-read release vs the pre-execution inventory: -> list of problems (empty = exactly the planned removal)."""
    problems = []
    if not isinstance(after, dict) or not isinstance(after.get("id"), int) or not isinstance(after.get("assets"), list):
        return ["the re-read is not a release object with an asset list"]
    rid = after["id"]
    pre = next((r for r in before if r["id"] == rid), None)
    if pre is None:
        return ["release %d was not in the pre-execution inventory" % rid]
    planned = {i["asset_id"] for i in plan_items if i["release_id"] == rid}
    want = {a["id"] for a in pre.get("assets") or []} - planned
    got = {a["id"] for a in after["assets"]}
    if got & planned:
        problems.append("release %d: planned assets still present: %s" % (rid, sorted(got & planned)))
    if want - got:
        problems.append("release %d: assets NOT in the plan are gone: %s" % (rid, sorted(want - got)))
    if got - want - planned:
        problems.append("release %d: unexpected new assets: %s" % (rid, sorted(got - want - planned)))
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--releases", required=True, help="JSON array of releases (all pages)")
    p.add_argument("--keep-newest", type=int, default=KEEP_NEWEST)
    p.add_argument("--keep", action="append", default=[], help="an extra tag to keep (repeatable)")
    p.add_argument("--json-out", help="write the plan items here (the executing wrapper reads ids from it)")
    v = sub.add_parser("verify")
    v.add_argument("--before", required=True)
    v.add_argument("--plan", required=True)
    v.add_argument("--after", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        before = json.loads(Path(a.before).read_text(encoding="utf-8"))
        items = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        try:
            after = json.loads(Path(a.after).read_text(encoding="utf-8"))
        except ValueError:
            after = None
        problems = verify(before, items, after)
        for line in problems:
            print("VERIFY FAILED: " + line)
        if not problems:
            print("verified release %d (%s): exactly the planned assets are gone" % (after["id"], after["tag_name"]))
        return 1 if problems else 0
    releases = json.loads(Path(a.releases).read_text(encoding="utf-8"))
    if not isinstance(releases, list) or not releases:
        print("STOP: no releases read; refusing to plan", file=sys.stderr)
        return 2
    try:
        pins = pinned_versions()
        keep, items = plan(releases, pins, a.keep_newest, a.keep)
    except (PinReadError, ValueError) as e:
        print("STOP: %s; no plan" % e, file=sys.stderr)
        return 3
    sha = plan_sha256(items)
    total = sum(i["size"] for i in items)
    print("KEEP (bundles untouched):")
    for t, why in sorted(keep.items()):
        print("  %-10s %s" % (t, why))
    per = {}
    for i in items:
        per.setdefault(i["tag"], []).append(i)
    print("DELETE (bundle assets only; releases, tags and other assets stay):")
    for t in sorted(per):
        print("  %-10s %2d assets %8.1f MB" % (t, len(per[t]), sum(i["size"] for i in per[t]) / 1e6))
    print("ASSETS (release id, asset id, size, name as JSON):")
    for i in items:
        print("  %-10s %6d %8d %11d %s" % (i["tag"], i["release_id"], i["asset_id"], i["size"], json.dumps(i["name"])))
    print("TOTAL %d assets in %d releases, %.2f GB" % (len(items), len(per), total / 1e9))
    print("PLAN-SHA256 %s" % sha)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(items, sort_keys=True, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
