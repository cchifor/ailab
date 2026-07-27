#!/usr/bin/env python3
"""check-inline-hashes.py — verify hand-computed content hashes embedded in
the manifests haven't silently drifted from the content they describe.

The repo carries FOUR inline, hand-computed content-hash values with no
reloader/controller to keep them honest:

  1. The `checksum/capability-kids` pod-template annotation in
     provisioner-deploy.yaml — must equal sha256 of the `capability-kids.json`
     value embedded in capability-kids-configmap.yaml. No reloader is
     installed, so this is what bumps the pod template (Recreate strategy) on
     any policy edit, forcing a re-read of the mounted file.
  2. The content-addressed 10-hex suffix on the platform-dev NFS provisioner
     Job's `metadata.name` (platform-dev-nfs-provisioner-job.yaml) — a Job's
     `spec.template` is immutable, so the suffix changes whenever the
     container image/args script changes, letting Flux prune+recreate the Job
     instead of failing an in-place patch.
  3. The `checksum/config` pod-template annotation in litellm.yaml — must
     equal sha256[:12] of the `litellm-config` ConfigMap's `config.yaml`
     value (also in litellm.yaml). No reloader is installed, so this is what
     rolls the pod on a model_list-only edit.
  4. The `checksum/config` pod-template annotation in litellm-local.yaml —
     same recipe as #3, against the `litellm-local-config` ConfigMap's
     `config.yaml` value (also in litellm-local.yaml).

Run: `python scripts/check-inline-hashes.py` (wired as `just af-verify-hashes`).
Prints `OK <path>` per verified site, `DRIFT <path> expected=<full> actual=<full>`
(full-length, untruncated values) per mismatch, and exits non-zero if ANY site drifted.

Stdlib-only (no PyYAML): manifests are read as text and the specific hashed
spans are located with small, targeted scans/regexes rather than a full YAML
parse, so this carries no runtime dependency beyond Python itself.

Adding a future hash site: write a `check_...() -> Site` function (path,
expected, actual) and append it to SITES below.
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@dataclass
class Site:
    path: Path
    expected: str
    actual: str


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _literal_block(text: str, marker_re: "re.Pattern[str]") -> tuple[list[str], int]:
    """Locate the YAML literal block scalar (`|`) whose marker line matches
    `marker_re` and return (raw_lines, content_indent).

    raw_lines are the literal following lines with their ORIGINAL leading
    whitespace preserved (i.e. NOT dedented). content_indent is the
    indentation (in spaces) YAML itself would treat as the block's own
    indentation, taken from the first non-blank content line — dedenting
    each raw line by content_indent recovers the YAML-parsed scalar value.
    Trailing blank lines are dropped (they belong to whatever follows the
    scalar, not the scalar itself).
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not marker_re.match(line):
            continue
        marker_indent = len(line) - len(line.lstrip(" "))
        raw: list[str] = []
        content_indent = None
        for l in lines[i + 1 :]:
            if l.strip() == "":
                raw.append(l)
                continue
            indent = len(l) - len(l.lstrip(" "))
            if indent <= marker_indent:
                break
            if content_indent is None:
                content_indent = indent
            raw.append(l)
        while raw and raw[-1].strip() == "":
            raw.pop()
        return raw, (content_indent if content_indent is not None else marker_indent + 2)
    raise ValueError(f"no line matching {marker_re.pattern!r} found")


def check_capability_kids_checksum() -> Site:
    """checksum/capability-kids (provisioner-deploy.yaml) vs a fresh sha256 of
    the capability-kids.json value it stamps (capability-kids-configmap.yaml).

    Recipe (matches the header comment in provisioner-deploy.yaml): sha256 of
    the YAML-parsed (dedented) `capability-kids.json` string value, UTF-8
    encoded, INCLUDING its single YAML-clip trailing newline.
    """
    deploy_path = REPO / "kubernetes/apps/infrastructure/security/openbao/provisioner-deploy.yaml"
    cm_path = REPO / "kubernetes/apps/infrastructure/security/openbao/capability-kids-configmap.yaml"

    deploy_text = deploy_path.read_text(encoding="utf-8")
    m = re.search(r'checksum/capability-kids:\s*"([0-9a-f]{64})"', deploy_text)
    if not m:
        raise ValueError(f"checksum/capability-kids annotation not found in {deploy_path}")
    expected = m.group(1)

    cm_text = cm_path.read_text(encoding="utf-8")
    raw_lines, content_indent = _literal_block(
        cm_text, re.compile(r"^[ ]*capability-kids\.json:\s*\|\s*$")
    )
    # YAML `|` (clip chomping) dedents by the block's own indentation and
    # keeps exactly one trailing newline — this recovers the literal
    # capability-kids.json string value as the ConfigMap embeds it.
    dedented = "".join("\n" if l.strip() == "" else l[content_indent:] for l in raw_lines)
    actual = sha256_hex(dedented)
    return Site(deploy_path, expected, actual)


def check_litellm_config_checksum() -> Site:
    """checksum/config (litellm.yaml Deployment pod-template annotation) vs a
    fresh sha256[:12] of the litellm-config ConfigMap's `config.yaml` value
    (same file).

    Recipe (matches the header comment above the annotation in litellm.yaml):
        yq -r 'select(.kind=="ConfigMap" and .metadata.name=="litellm-config") | .data."config.yaml"' \\
          kubernetes/apps/apps/ai/litellm.yaml | sha256sum | cut -c1-12
    `yq -r` on a scalar string prints it followed by exactly one trailing
    newline — the same single trailing newline YAML `|` (clip chomping)
    already keeps, so the dedented literal block value hashes identically to
    that pipeline's stdin.
    """
    path = REPO / "kubernetes/apps/apps/ai/litellm.yaml"
    text = path.read_text(encoding="utf-8")

    m = re.search(r'checksum/config:\s*"([0-9a-f]{12})"', text)
    if not m:
        raise ValueError(f"checksum/config annotation not found in {path}")
    expected = m.group(1)

    raw_lines, content_indent = _literal_block(text, re.compile(r"^[ ]*config\.yaml:\s*\|\s*$"))
    dedented = "".join("\n" if l.strip() == "" else l[content_indent:] for l in raw_lines)
    actual = sha256_hex(dedented)[:12]
    return Site(path, expected, actual)


def check_litellm_local_config_checksum() -> Site:
    """checksum/config (litellm-local.yaml Deployment pod-template annotation)
    vs a fresh sha256[:12] of the litellm-local-config ConfigMap's
    `config.yaml` value (same file). Same recipe as
    check_litellm_config_checksum, against litellm-local.yaml.
    """
    path = REPO / "kubernetes/apps/apps/ai/litellm-local.yaml"
    text = path.read_text(encoding="utf-8")

    m = re.search(r'checksum/config:\s*"([0-9a-f]{12})"', text)
    if not m:
        raise ValueError(f"checksum/config annotation not found in {path}")
    expected = m.group(1)

    raw_lines, content_indent = _literal_block(text, re.compile(r"^[ ]*config\.yaml:\s*\|\s*$"))
    dedented = "".join("\n" if l.strip() == "" else l[content_indent:] for l in raw_lines)
    actual = sha256_hex(dedented)[:12]
    return Site(path, expected, actual)


def check_platform_dev_job_suffix() -> Site:
    """The platform-dev NFS provisioner Job's content-addressed name suffix
    vs a fresh sha256(image + "\\n---\\n" + args-script)[:10].

    ⚠ RECIPE NOTE: the header comment describes hashing the container image
    plus the args "shell script" (i.e. the clean, YAML-dedented script text).
    That reading does NOT reproduce the current suffix. Reverse-engineering
    the value that DOES verify shows the suffix was actually computed from
    the args block's RAW, still-YAML-indented text (leading whitespace on
    every line kept, NOT dedented) with only the trailing newline stripped —
    consistent with a naive text-extraction (e.g. sed/awk over the raw file)
    rather than a YAML-aware dedent. Implemented as discovered so the CURRENT
    value verifies; see the task report for detail. Any future edit must
    recompute the suffix the SAME way this script does (raw indented args
    text), not via the header's plain-English "shell script" reading.
    """
    job_path = REPO / "kubernetes/apps/infrastructure/agentforge-sandbox/platform-dev-nfs-provisioner-job.yaml"
    text = job_path.read_text(encoding="utf-8")

    name_m = re.search(
        r"^\s*name:\s*af-sbx-provision-tenant-zero-platform-dev-([0-9a-f]{10})\s*(?:#.*)?$",
        text,
        re.MULTILINE,
    )
    if not name_m:
        raise ValueError(f"content-addressed Job name suffix not found in {job_path}")
    expected = name_m.group(1)

    # Scope the `image:` search to the `containers:` block specifically (not
    # the whole file), so an initContainer added ahead of it in the future
    # can never be mistaken for the main container's image. Bounded by the
    # next line at <= the `containers:` key's own indentation (its next
    # sibling key, e.g. `volumes:`, or EOF).
    containers_m = re.search(r"^([ ]*)containers:\s*$", text, re.MULTILINE)
    if not containers_m:
        raise ValueError(f"containers: block not found in {job_path}")
    containers_indent = len(containers_m.group(1))
    block_start = containers_m.end()
    sibling_m = re.search(rf"^[ ]{{0,{containers_indent}}}\S", text[block_start:], re.MULTILINE)
    block_end = block_start + sibling_m.start() if sibling_m else len(text)
    containers_block = text[block_start:block_end]

    image_m = re.search(r"^\s*image:\s*(\S+)", containers_block, re.MULTILINE)
    if not image_m:
        raise ValueError(f"container image not found in the containers: block of {job_path}")
    image = image_m.group(1)

    args_m = re.search(r"^\s*args:\s*$", text, re.MULTILINE)
    if not args_m:
        raise ValueError(f"container args block not found in {job_path}")
    raw_lines, _ = _literal_block(text[args_m.start() :], re.compile(r"^[ ]*-\s*\|[-+]?\s*$"))
    body = "".join(raw_lines).rstrip("\n")

    actual = sha256_hex(f"{image}\n---\n{body}")[:10]
    return Site(job_path, expected, actual)


# Table of hash sites to verify. Add a `check_...() -> Site` function above
# and append it here to cover a new site.
SITES = [
    check_capability_kids_checksum,
    check_litellm_config_checksum,
    check_litellm_local_config_checksum,
    check_platform_dev_job_suffix,
]


def main() -> int:
    ok = True
    for check in SITES:
        try:
            site = check()
        except Exception as exc:  # missing/unparseable site = gate failure, not a skip
            print(f"ERROR {check.__name__}: {exc}")
            ok = False
            continue
        rel = site.path.relative_to(REPO).as_posix()
        if site.expected == site.actual:
            print(f"OK {rel}")
        else:
            # Full-length, no truncation: the Job-suffix site's values are
            # already only 10 hex chars, so an [:8] slice loses 2 chars of
            # exactly what needs to be visible on a DRIFT.
            print(f"DRIFT {rel} expected={site.expected} actual={site.actual}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
