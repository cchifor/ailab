#!/usr/bin/env python3
"""Pre-check before enabling or widening zot `deleteReferrers` (registry_zot_strive_delete_referrers_repos).

zot v2.1.18 GC (pkg/storage/gc/gc.go removeReferrer) deletes a referrer when its `subject` digest is not among the
TOP-LEVEL entries of the repo's index.json (isManifestReferencedInIndex does not recurse into nested indexes), and
it ignores keepTags for that decision. So deleteReferrers is only safe for a repo if:
  1. every digest referenced from inside any index is ALSO a top-level entry (else a referrer of a live platform
     child would look orphaned), and
  2. no referrer that would be deleted carries a tag (it would lose a tag keepTags promises to keep).
This walks the store READ-ONLY and exits 1 if either rule is broken for any repo. Run it on the registry LXC
(node-ssh.py does not forward stdin, so push a temporary copy and remove it):
  B=$(base64 -w0 scripts/zot-referrer-orphan-check.py)
  python scripts/node-ssh.py 192.168.0.2 "echo $B | base64 -d > /tmp/zroc.py && pct push 5004 /tmp/zroc.py \\
    /tmp/zroc.py && pct exec 5004 -- python3 /tmp/zroc.py strive/web; rc=\\$?; pct exec 5004 -- rm -f /tmp/zroc.py; \\
    rm -f /tmp/zroc.py; echo check-rc=\\$rc; exit \\$rc"
The cleanup must not hide the result: only `check-rc=0` (and every line `"safe": true`) is a pass.
Default store: /var/lib/registry/store; override with --root.
"""
import argparse
import json
import os
import sys

TAG = "org.opencontainers.image.ref.name"


def is_safe(res):
    """FAIL CLOSED: safe only with a non-empty index whose every manifest was read, no live child missing from the
    top level, and no tagged referrer among the would-be-deleted ones."""
    return (res["manifests"] > 0 and res["unreadable"] == 0 and res["nested_not_top"] == 0
            and res["tagged_orphans"] == 0)


def check_repo(entries, load):
    """Classify one repo. `entries` = index.json `manifests`; `load(digest)` returns the parsed manifest or None.
    Returns a dict of counts; see is_safe() for what must hold before deleteReferrers is enabled."""
    top = {m["digest"] for m in entries}
    nested, seen, stack = set(), set(), [m["digest"] for m in entries]
    while stack:
        digest = stack.pop()
        if digest in seen:
            continue
        seen.add(digest)
        for child in (load(digest) or {}).get("manifests", []) or []:
            nested.add(child["digest"])
            stack.append(child["digest"])
    out = {"manifests": len(entries), "nested_not_top": len(nested - top), "orphans": 0, "tagged_orphans": 0,
           "kept_referrers": 0, "unreadable": 0}
    for m in entries:
        doc = load(m["digest"])
        if doc is None:  # an unreadable manifest could be a referrer: never treat missing data as safe
            out["unreadable"] += 1
            continue
        subject = (doc.get("subject") or {}).get("digest")
        if not subject:
            continue
        if subject in top:
            out["kept_referrers"] += 1
        else:
            out["orphans"] += 1
            if (m.get("annotations") or {}).get(TAG):
                out["tagged_orphans"] += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--root", default="/var/lib/registry/store")
    args = ap.parse_args(argv)
    ok = True
    for repo in args.repos:
        base = os.path.join(args.root, repo)
        try:
            with open(os.path.join(base, "index.json")) as fh:
                entries = json.load(fh).get("manifests") or []
        except (OSError, ValueError) as err:
            print(json.dumps({"repo": repo, "safe": False, "error": f"index.json unreadable ({err.__class__.__name__})"}))
            ok = False
            continue

        def load(digest, base=base):
            try:
                with open(os.path.join(base, "blobs", *digest.split(":"))) as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return None

        res = check_repo(entries, load)
        safe = is_safe(res)
        ok = ok and safe
        print(json.dumps({"repo": repo, "safe": safe, **res}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
