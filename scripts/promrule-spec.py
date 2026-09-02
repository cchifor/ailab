#!/usr/bin/env python3
"""Extract the `spec:` of a single-document PrometheusRule manifest as a promtool rules file.

`promtool check rules` reads a plain `groups:` document, not the PrometheusRule CRD wrapper Flux
applies, so scripts/rules-lint.sh runs this first over monitoring/*-rules.yaml and hands promtool the
extracted specs. STDLIB ONLY, deliberately: the CI runner installs no dependency (no PyYAML), and
this repo's rule files are single-document manifests whose `spec:` is a top-level key with a
2-space-indented body, which a textual cut handles exactly. Comment lines are transparent wherever
they sit (these files are comment-heavy and a column-0 `#` inside spec is legal YAML); the cut
ends only at the next top-level KEY. Everything else FAILS CLOSED rather than guessing: more than
one document, a kind that is not PrometheusRule, no `spec:` at column 0, a spec body that is not
uniformly 2-space-indented, any other column-0 line inside the spec, a spec without `groups:`, or
an extracted spec that carries fewer `- alert:`/`- record:` entries than the manifest is a
SpecError and a non-zero exit — a rules file this cannot extract is a rules file the gate has not
checked, and a rules file it truncated would be one the gate only pretended to check.

    python3 scripts/promrule-spec.py --out <dir> <rules.yaml>...

writes <dir>/<basename> per input and prints one line per file (with its rule count) plus a total
line, `promrule-spec: N rules across M files`, that scripts/rules-lint.sh reconciles against
promtool's own "N rules found"; the input order is preserved.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys


class SpecError(Exception):
    """A rules manifest this extractor refuses to guess about (see module docstring)."""


_DOC_SEP = re.compile(r"(?m)^---[ \t]*$")
_KIND = re.compile(r"(?m)^kind:[ \t]*(\S+)[ \t]*$")
_SPEC = re.compile(r"(?m)^spec:[ \t]*$")
_TOP_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*:(?:[ \t]|$)")
_RULE = re.compile(r"(?m)^[ \t]*-[ \t]*(?:alert|record):[ \t]*\S")


def rule_count(text: str) -> int:
    """How many `- alert:` / `- record:` entries `text` (a manifest or an extracted spec) carries."""
    return len(_RULE.findall(text))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def extract_spec(text: str, label: str) -> str:
    """Return the dedented `spec:` body of the one PrometheusRule document in `text`."""
    docs = [d for d in _DOC_SEP.split(text) if any(_is_content(ln) for ln in d.splitlines())]
    if len(docs) != 1:
        raise SpecError(f"{label}: expected exactly 1 YAML document, found {len(docs)}")
    doc = docs[0]
    kinds = _KIND.findall(doc)
    if kinds != ["PrometheusRule"]:
        raise SpecError(f"{label}: expected one `kind: PrometheusRule`, found {kinds}")
    specs = list(_SPEC.finditer(doc))
    if len(specs) != 1:
        raise SpecError(f"{label}: expected exactly one top-level `spec:`, found {len(specs)}")
    body: list[str] = []
    for line in doc[specs[0].end() :].splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            body.append("")  # blank or comment: transparent, but keep the line numbering aligned
            continue
        if not line.startswith((" ", "\t")):
            if _TOP_KEY.match(line):
                break  # next top-level key
            raise SpecError(f"{label}: unexpected column-0 line inside spec: {line!r}")
        if not line.startswith("  "):
            raise SpecError(f"{label}: spec body line is not 2-space indented: {line!r}")
        body.append(line[2:])
    spec = "\n".join(body).rstrip("\n") + "\n"
    if not re.search(r"(?m)^groups:[ \t]*$", spec):
        raise SpecError(f"{label}: spec has no top-level `groups:`")
    if rule_count(spec) != rule_count(doc):
        raise SpecError(
            f"{label}: extracted spec has {rule_count(spec)} rules but the manifest has "
            f"{rule_count(doc)} — refusing to hand promtool a truncated spec"
        )
    return spec


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=pathlib.Path, help="directory to write specs into")
    parser.add_argument("files", nargs="+", type=pathlib.Path, help="PrometheusRule manifests")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    names = [f.name for f in args.files]
    if len(set(names)) != len(names):
        print(f"promrule-spec: duplicate basenames would overwrite each other: {names}", file=sys.stderr)
        return 1
    total = 0
    for src in args.files:
        try:
            spec = extract_spec(src.read_text(encoding="utf-8"), str(src))
        except (OSError, SpecError) as exc:
            print(f"promrule-spec: {exc}", file=sys.stderr)
            return 1
        (args.out / src.name).write_text(spec, encoding="utf-8")
        n = rule_count(spec)
        total += n
        print(f"extracted {src} -> {args.out / src.name} ({n} rules)")
    print(f"promrule-spec: {total} rules across {len(args.files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
