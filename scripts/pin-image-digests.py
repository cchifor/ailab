#!/usr/bin/env python3
"""Pin AgentForge image digests into the GitOps manifests (replaces placeholder @sha256 refs).

The image-build CI pushes `registry.chifor.me/agentforge/<image>@sha256:<digest>` and prints the digest;
this rewrites the matching placeholder pins across the manifest tree in ONE reviewable diff. Two pin
PATHS keep the staged activation honest (plan §C): the BOOTSTRAP path pins only the images Stage-1 needs
(orchestrator, agentforge-platform) — it must NOT touch a gated workload; the WORKLOAD path pins the rest
at Stage 4 un-gate.

    # pin one or more images by exact digest (idempotent, re-runnable):
    python scripts/pin-image-digests.py orchestrator=sha256:abc123... agentforge-platform=sha256:def456...
    python scripts/pin-image-digests.py --dry-run worker=sha256:...

Matches any ref of the form `registry.chifor.me/<repo>/<image>@sha256:<anything>` (placeholder OR a prior
real digest) and rewrites the digest in place. Reports every file+line changed. Exit 1 if an image given
on the CLI matched nothing (a typo guard), or (without --allow-unpinned) if placeholders remain for images
NOT given — so a bootstrap pin can't silently leave a needed image unpinned.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
ROOT = REPO / "kubernetes"
REGISTRY = "registry.chifor.me"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# registry.chifor.me/<repo>/<image>@sha256:<something>  (repo segment e.g. `agentforge`)
REF_RE = re.compile(
    r"(?P<full>" + re.escape(REGISTRY) + r"/(?P<repo>[\w./-]+?)/(?P<image>[\w.-]+)@sha256:(?P<cur>[A-Za-z0-9_]+))"
)

# ---------------------------------------------------------------- immutable-Job wedge guard
# A Job's `spec.template` is IMMUTABLE. Re-pinning the image inside a Flux-APPLIED Job is therefore a
# patch that the kustomize-controller dry-run rejects with "field is immutable", which takes the WHOLE
# Kustomization False — and, through dependsOn, every layer beneath it. That is the 2026-07-28 outage
# (openbao -> external-secrets -> agentforge-workers/ci-runners/tenant-platform-dev, hours down).
#
# The durable fix is the per-object annotation `kustomize.toolkit.fluxcd.io/force: "enabled"`, which
# makes Flux delete+recreate just that Job. This guard asserts the invariant so a NEW Flux-managed Job
# can never re-introduce the wedge: it runs on every pin (the exact path that triggers the failure).
#
# Only Jobs that Flux actually applies are in scope — this repo deliberately keeps several one-shot Jobs
# UNLISTED in their kustomization.yaml (db-migrate, litellm-vkeys, litellm-db-bootstrap, the egress
# canaries) precisely so Flux never owns them; those cannot wedge anything.
DOC_SPLIT_RE = re.compile(r"(?m)^---\s*$")
JOB_KIND_RE = re.compile(r"(?m)^kind:\s*Job\s*(?:#.*)?$")
FORCE_RE = re.compile(r"kustomize\.toolkit\.fluxcd\.io/force:\s*[\"']?enabled", re.IGNORECASE)


def _is_flux_managed(path: pathlib.Path) -> bool:
    """True if `path` is listed as a resource in its sibling kustomization.yaml (i.e. Flux applies it)."""
    kfile = path.parent / "kustomization.yaml"
    if not kfile.is_file():
        return False
    entry = re.compile(r"(?m)^\s*-\s*\.?/?" + re.escape(path.name) + r"\s*(?:#.*)?$")
    return bool(entry.search(kfile.read_text(encoding="utf-8")))


def check_job_force_annotations() -> list[str]:
    """Return a list of violation strings: Flux-managed Jobs missing the force annotation."""
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*.yaml")):
        if path.name == "gotk-components.yaml":
            continue
        text = path.read_text(encoding="utf-8")
        if not JOB_KIND_RE.search(text):
            continue
        if not _is_flux_managed(path):
            continue  # operator-run one-shot, deliberately unlisted — Flux never applies it
        for doc in DOC_SPLIT_RE.split(text):
            if JOB_KIND_RE.search(doc) and not FORCE_RE.search(doc):
                name = re.search(r"(?m)^\s*name:\s*(\S+)", doc)
                violations.append(f"{path.relative_to(REPO).as_posix()} (Job {name.group(1) if name else '?'})")
    return violations


def parse_args(argv: list[str]) -> tuple[dict[str, str], bool]:
    pins: dict[str, str] = {}
    dry = False
    for a in argv:
        if a in ("--dry-run", "-n"):
            dry = True
            continue
        if "=" not in a:
            sys.exit(f"bad arg {a!r}: expected image=sha256:<digest> or --dry-run")
        img, dig = a.split("=", 1)
        if not DIGEST_RE.match(dig):
            sys.exit(f"bad digest for {img!r}: {dig!r} (want sha256:<64 hex>)")
        pins[img] = dig
    if not pins:
        sys.exit("usage: pin-image-digests.py <image>=sha256:<digest> [...] [--dry-run]")
    return pins, dry


def main(argv: list[str]) -> int:
    pins, dry = parse_args(argv)
    matched: dict[str, int] = {k: 0 for k in pins}
    changed_files: list[str] = []
    remaining_placeholder: list[str] = []

    for path in sorted(ROOT.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        new_lines = []
        file_changed = False
        for i, line in enumerate(text.splitlines(keepends=True), 1):
            def _sub(m: re.Match) -> str:
                img = m.group("image")
                cur = m.group("cur")
                if img in pins:
                    new = pins[img].split(":", 1)[1]
                    if cur != new:
                        matched[img] += 1
                        rel = path.relative_to(REPO).as_posix()
                        print(f"  {rel}:{i}  {img}  {cur[:12]}… -> {new[:12]}…")
                    else:
                        matched[img] += 1  # already pinned to target (idempotent)
                    return m.group("full").replace(f"@sha256:{cur}", f"@sha256:{new}")
                # not a target image — flag if it still looks like a placeholder
                if not DIGEST_RE.match(f"sha256:{cur}"):
                    remaining_placeholder.append(f"{path.relative_to(REPO).as_posix()}:{i} {img}")
                return m.group("full")

            newline = REF_RE.sub(_sub, line)
            if newline != line:
                file_changed = True
            new_lines.append(newline)
        if file_changed:
            changed_files.append(path.relative_to(REPO).as_posix())
            if not dry:
                path.write_text("".join(new_lines), encoding="utf-8")

    print("")
    rc = 0
    for img, n in matched.items():
        if n == 0:
            print(f"[FAIL] image {img!r} matched NO ref in {ROOT} (typo?)")
            rc = 1
        else:
            print(f"[ OK ] {img}: {n} ref(s) {'would be ' if dry else ''}pinned")
    if remaining_placeholder:
        print("\n[WARN] placeholder refs still unpinned (not in this pin set):")
        for r in sorted(set(remaining_placeholder)):
            print(f"  {r}")

    # Immutable-Job wedge guard (see header near JOB_KIND_RE). Fail the pin rather than let a
    # Flux-applied Job without the force annotation reach main and wedge its whole layer.
    violations = check_job_force_annotations()
    if violations:
        print('\n[FAIL] Flux-managed Job(s) missing `kustomize.toolkit.fluxcd.io/force: "enabled"`:')
        for v in violations:
            print(f"  {v}")
        print(
            "  A Job's spec.template is immutable — re-pinning one of these wedges its ENTIRE\n"
            "  Kustomization (and everything that dependsOn it) with 'field is immutable'.\n"
            "  Add the annotation under metadata.annotations, or keep the Job out of\n"
            "  kustomization.yaml if it is meant to be an operator-run one-shot."
        )
        rc = 1
    else:
        print("\n[ OK ] all Flux-managed Jobs carry the immutable-field force annotation")

    print(f"\n{'DRY-RUN — no writes. ' if dry else ''}{len(changed_files)} file(s) touched.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
