#!/usr/bin/env python3
"""gen-litellm-consumers.py — ONE source of truth for the LiteLLM routes its consumers list.

A model added to LiteLLM's model_list used to be hand-copied into two other places, and its
rollout checksum hand-recomputed, with nothing forcing any of them to agree. Commit c90ff86
(2026-09-08, "Registered in three places, all of which are needed for it to be usable") is the
record of that process; qwen3.8-flash-next-q6-cloud and qwen3.8-27b-fp8-cloud were then added to
the router and reached NEITHER consumer, which is exactly the failure a hand process produces.

THE SOURCE is the `model_list` inside the `litellm-config` ConfigMap —
`kubernetes/apps/apps/ai/litellm.yaml`, key `config.yaml`. It is read the way
scripts/check-inline-hashes.py reads it (the literal block is located as text and dedented; that
module is imported for the purpose so the two cannot disagree), then parsed with PyYAML. NOTE that
this locates the block the way check-inline-hashes does: the FIRST `config.yaml: |` literal block
in the file, whichever document it sits in — so litellm-config must stay the first such ConfigMap
in litellm.yaml.

DERIVED SPANS (this script owns these end to end; everything around them is preserved byte for
byte):
  1. the `model_ids` array of connection "0" inside OPENAI_API_CONFIGS
                                            — apps/ai/open-webui.yaml (one env line)
     Open WebUI does NOT call /v1/models for a connection that carries a non-empty model_ids, and
     there is no wildcard upstream, so the static allowlist is the only way to put a route under
     the picker's Local group. Only that one JSON array on that one line is rewritten; every other
     key and value in the JSON keeps its order, and the YAML line keeps its exact prefix/suffix.
  2. the rows of the litellm provider's `models:` list  — apps/dsh/settings.seed.yaml
     kubernetes/apps/apps/dsh/reconcile-provider.js splices EXACTLY this block into the live
     settings.yaml on every pod boot, line-based, so the block's shape (plain `litellm:` header,
     direct-child indentation) is left alone: the owned span runs from the first `- id:` row to
     the end of the list, and the explanatory comment lines above the first row stay. A row gets
     `input: [text, image]` only when the LiteLLM entry declares `model_info.supports_vision: true`
     — a hand-entered dsh model is text-only until it says otherwise.
  3. the `checksum/config` pod-template annotation  — apps/ai/litellm.yaml
     The value the documented recipe produces (`yq -r ... | sha256sum | cut -c1-12`), derived with
     check-inline-hashes' own function so the generator and the CI gate are one derivation.

SELECTION RULE — a route is consumer-visible when ALL THREE hold:
  * `litellm_params.api_base` is present and its host is a private IPv4 address (10/8,
    172.16/12, 192.168/16) or ends with `.svc.cluster.local`. Entries with no api_base (the paid
    providers, their embedding model) are never listed: they are what connection "1" discovers as
    External, and dsh is not offered them.
  * `model_info.mode` is unset or `chat`. Both consumers are chat pickers, so a self-hosted
    embedding / rerank / transcription route (mode is LiteLLM's own key for that) is never
    listed even though its api_base is private.
  * `model_info.hidden` is not `true`. That key is the explicit opt-out for a self-hosted chat
    route that should stay reachable by name but not be advertised (a fallback-only deployment,
    a route under test). Set it on the LiteLLM entry; never hand-edit the consumers. What it
    does, precisely: the route leaves Open WebUI's Local group and dsh's model list. It does
    NOT leave Open WebUI altogether — connection "1" has no model_ids, so it still discovers the
    route from /v1/models and shows it under External, because `hidden` is not a key LiteLLM
    knows (model_info is free-form) and the router keeps advertising the name. The value must be
    a YAML boolean: `hidden: "true"`, `hidden: 1` and the like are a FAILURE naming the entry,
    never silently visible.
Order is model_list order, de-duplicated on model_name (LiteLLM allows several deployments under
one name; a consumer lists the name once). An EMPTY selection is refused in both modes: a rule
that matches nothing is a broken source, not an instruction to blank two pickers.

USAGE
  python3 scripts/gen-litellm-consumers.py            # check (default) — exit 1 on drift
  python3 scripts/gen-litellm-consumers.py --check    # same, explicitly (CI)
  python3 scripts/gen-litellm-consumers.py --write    # rewrite the three spans in place

`--check` renders every span into memory and compares it byte for byte with what is on disk, so
"the check passes" and "running --write changes nothing" are the same statement; both modes are
idempotent. A span whose id list already matches but whose text differs (a hand-added comment
inside the generated rows, an extra key on a row, spacing in the JSON array) is reported as
DRIFT too, with a message saying so, and --write normalises it. Files are read and written with
their line endings intact (newline=""), so a CRLF checkout is rewritten only inside the owned
spans and compared as real bytes. Any parse failure is a FAILURE, never a skip. Stdlib + PyYAML,
which is the CI runner's system package (the same assumption scripts/tests/test_worker_probes.py
makes).
"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

REPO = Path(__file__).resolve().parents[1]
LITELLM_REL = "kubernetes/apps/apps/ai/litellm.yaml"
OPEN_WEBUI_REL = "kubernetes/apps/apps/ai/open-webui.yaml"
SEED_REL = "kubernetes/apps/apps/dsh/settings.seed.yaml"

#: The private IPv4 ranges the rule admits. Deliberately NOT ipaddress's `is_private`, which also
#: says yes to loopback, link-local and the CGNAT block — none of which is a backend here.
PRIVATE_V4 = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
CLUSTER_SUFFIX = ".svc.cluster.local"

#: A model_name is written verbatim into a JSON string inside a single-quoted YAML scalar and into
#: a plain YAML list row, so it must be a bounded, quote-free, whitespace-free token.
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")

_CONFIG_MARKER = re.compile(r"^[ ]*config\.yaml:\s*\|\s*$")
_CHECKSUM = re.compile(r'(checksum/config:\s*")([0-9a-f]{12})(")')
_WEBUI_LINE = re.compile(r"^(\s*- \{ name: OPENAI_API_CONFIGS, value: ')(.*)(' \}\s*)$")
_WEBUI_MODEL_IDS = re.compile(r'("0"\s*:\s*\{[^{}]*?"model_ids"\s*:\s*)\[[^\]]*\]')
_SEED_ROW = re.compile(r"^(\s*)- id:\s*(\S+)\s*$")
_SEED_INPUT = re.compile(r"^\s*input:\s*\[(.*)\]\s*$")


class SourceError(RuntimeError):
    """The source could not be read, or a consumer file is not in the shape this script owns."""


def _load_check_inline_hashes():
    """The checksum derivation is check-inline-hashes' — imported, never reimplemented."""
    if "check_inline_hashes" in sys.modules:
        return sys.modules["check_inline_hashes"]
    path = Path(__file__).resolve().parent / "check-inline-hashes.py"
    spec = importlib.util.spec_from_file_location("check_inline_hashes", path)
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: its @dataclass resolves annotations via sys.modules[cls.__module__].
    sys.modules["check_inline_hashes"] = mod
    spec.loader.exec_module(mod)
    return mod


_cih = _load_check_inline_hashes()


# ---------------------------------------------------------------------------
# the source: litellm.yaml's config.yaml literal block
# ---------------------------------------------------------------------------


def litellm_config_text(text: str) -> str:
    """The YAML-parsed (dedented) `config.yaml` value, exactly as check-inline-hashes sees it.

    Line breaks are normalised to LF first: YAML normalises them inside a scalar, and
    check-inline-hashes reads the file through universal newlines, so a CRLF checkout must hash
    to the same annotation value as an LF one.
    """
    text = text.replace("\r\n", "\n")
    try:
        raw_lines, content_indent = _cih._literal_block(text, _CONFIG_MARKER)
    except ValueError as exc:
        raise SourceError(f"{LITELLM_REL}: {exc}") from exc
    return "".join("\n" if l.strip() == "" else l[content_indent:] for l in raw_lines)


def config_checksum(text: str) -> str:
    """checksum/config for this litellm.yaml text: the documented yq recipe, via check-inline-hashes.

    The extra newline is the one `yq -r` appends; see check_litellm_local_config_checksum there.
    """
    return _cih.sha256_hex(litellm_config_text(text) + "\n")[:12]


@dataclass(frozen=True)
class Model:
    name: str
    vision: bool


def _private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    try:
        addr = ipaddress.IPv4Address(host)
    except ValueError:
        return host.endswith(CLUSTER_SUFFIX) and len(host) > len(CLUSTER_SUFFIX)
    return any(addr in net for net in PRIVATE_V4)


def consumer_visible(entry: dict) -> bool:
    """The selection rule, over one model_list entry (see the module docstring).

    Raises SourceError for a `model_info.hidden` that is not a YAML boolean: the opt-out must
    never fail open because of a quoted "true" or a 1.
    """
    params = entry.get("litellm_params") or {}
    api_base = params.get("api_base") if isinstance(params, dict) else None
    if not isinstance(api_base, str) or not api_base:
        return False
    host = urlsplit(api_base).hostname
    if not host or not _private_host(host):
        return False
    info = entry.get("model_info") or {}
    if not isinstance(info, dict):
        return True
    mode = info.get("mode")
    if mode is not None and mode != "chat":
        return False
    if "hidden" in info:
        hidden = info["hidden"]
        if not isinstance(hidden, bool):
            raise SourceError(
                f"{LITELLM_REL}: model_list entry {entry.get('model_name')!r}: model_info.hidden must be "
                f"a YAML boolean (true/false), got {hidden!r}"
            )
        if hidden:
            return False
    return True


def models_from_config(config_text: str) -> list[Model]:
    try:
        cfg = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise SourceError(f"{LITELLM_REL}: config.yaml does not parse: {exc}") from exc
    model_list = cfg.get("model_list") if isinstance(cfg, dict) else None
    if not isinstance(model_list, list) or not model_list:
        raise SourceError(f"{LITELLM_REL}: config.yaml has no model_list sequence")
    seen: set[str] = set()
    out: list[Model] = []
    for i, entry in enumerate(model_list):
        if not isinstance(entry, dict) or not isinstance(entry.get("model_name"), str):
            raise SourceError(f"{LITELLM_REL}: model_list[{i}] has no string model_name")
        if not consumer_visible(entry):
            continue
        name = entry["model_name"]
        if not _MODEL_NAME.match(name):
            raise SourceError(f"{LITELLM_REL}: model_name {name!r} is not a plain token")
        if name in seen:
            continue
        seen.add(name)
        info = entry.get("model_info") or {}
        out.append(Model(name=name, vision=isinstance(info, dict) and info.get("supports_vision") is True))
    if not out:
        raise SourceError(
            f"{LITELLM_REL}: no consumer-visible route in model_list; refusing to render empty consumer lists"
        )
    return out


def load_models(repo: Path) -> list[Model]:
    return models_from_config(litellm_config_text(_read(repo / LITELLM_REL)))


# ---------------------------------------------------------------------------
# the derived spans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    rel: str
    what: str
    committed: str
    derived: str
    current: str
    rendered: str

    @property
    def clean(self) -> bool:
        return self.current == self.rendered


def _read(path: Path) -> str:
    # newline="": line endings come through untouched, so a CRLF file is rewritten only inside the
    # owned span (the write below uses newline="" too) and --check compares the real bytes.
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc


def _ids_text(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def _describe(models: list[Model]) -> str:
    return _ids_text([m.name + (" (vision)" if m.vision else "") for m in models])


def render_open_webui(text: str, models: list[Model]) -> Span:
    lines = text.splitlines(keepends=True)
    hits = [i for i, l in enumerate(lines) if _WEBUI_LINE.match(l)]
    if len(hits) != 1:
        raise SourceError(
            f"{OPEN_WEBUI_REL}: expected exactly one `- {{ name: OPENAI_API_CONFIGS, value: '...' }}` "
            f"line, found {len(hits)}"
        )
    m = _WEBUI_LINE.match(lines[hits[0]])
    prefix, json_text, suffix = m.group(1), m.group(2), m.group(3)
    try:
        current_cfg = json.loads(json_text)
        current_ids = list(current_cfg["0"]["model_ids"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f'{OPEN_WEBUI_REL}: OPENAI_API_CONFIGS has no parseable "0".model_ids: {exc}') from exc

    ids = [mo.name for mo in models]
    new_array = json.dumps(ids, separators=(",", ":"))
    new_json, n = _WEBUI_MODEL_IDS.subn(lambda mm: mm.group(1) + new_array, json_text, count=1)
    if n != 1:
        raise SourceError(f'{OPEN_WEBUI_REL}: could not locate "0".model_ids inside OPENAI_API_CONFIGS')
    # Prove the textual rewrite did what it claims before anything is written. A committed id
    # containing `]` ends the array match early and leaves a dangling tail, which this catches.
    try:
        rewritten = json.loads(new_json)
    except ValueError as exc:
        raise SourceError(f"{OPEN_WEBUI_REL}: rewritten OPENAI_API_CONFIGS is not valid JSON: {exc}") from exc
    try:
        carried = rewritten["0"]["model_ids"]
    except (KeyError, TypeError) as exc:
        raise SourceError(f'{OPEN_WEBUI_REL}: rewritten OPENAI_API_CONFIGS lost "0".model_ids: {exc}') from exc
    if carried != ids:
        raise SourceError(f"{OPEN_WEBUI_REL}: rewritten OPENAI_API_CONFIGS does not carry the derived model_ids")
    lines[hits[0]] = prefix + new_json + suffix
    return Span(
        rel=OPEN_WEBUI_REL,
        what='OPENAI_API_CONFIGS "0".model_ids',
        committed=_ids_text(current_ids),
        derived=_ids_text(ids),
        current=text,
        rendered="".join(lines),
    )


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _header_re(key: str, depth: int) -> re.Pattern[str]:
    # The same header shape reconcile-provider.js's KEY_HEADER accepts: plain, quoted, optional
    # trailing comment, nothing else on the line.
    return re.compile(rf'^ {{{depth}}}(?:{re.escape(key)}|"{re.escape(key)}"|\'{re.escape(key)}\')[ \t]*:[ \t]*(#.*)?$')


def _block_end(lines: list[str], start: int, depth: int, to: int) -> int:
    """End (exclusive) of the block headed at `start`; the rule reconcile-provider.js's blockEnd uses."""
    end = start + 1
    while end < to:
        l = lines[end]
        if _is_blank(l) or _indent(l) > depth:
            end += 1
            continue
        if _is_comment(l):
            k = end
            while k < to and (_is_blank(lines[k]) or _is_comment(lines[k])):
                k += 1
            if k < to and _indent(lines[k]) > depth:
                end = k
                continue
        break
    while end > start + 1 and _is_blank(lines[end - 1]):
        end -= 1
    return end


def _child_indent(lines: list[str], start: int, end: int) -> int | None:
    for i in range(start + 1, end):
        if _is_blank(lines[i]) or _is_comment(lines[i]):
            continue
        return _indent(lines[i])
    return None


def _resolve(lines: list[str], path: list[str]) -> tuple[int, int, int]:
    """Walk `path` as DIRECT children (as reconcile-provider.js does). Returns (start, end, depth)."""
    frm, to, depth = 0, len(lines), 0
    start = end = -1
    for d, key in enumerate(path):
        pat = _header_re(key, depth)
        hits = [i for i in range(frm, to) if pat.match(lines[i].rstrip("\r\n"))]
        if len(hits) != 1:
            raise SourceError(
                f"{SEED_REL}: expected exactly one `{key}:` block header at indent {depth} under "
                f"{' > '.join(path[:d]) or '(root)'}, found {len(hits)}"
            )
        start = hits[0]
        end = _block_end(lines, start, depth, to)
        if d < len(path) - 1:
            ci = _child_indent(lines, start, end)
            if ci is None or ci <= depth:
                raise SourceError(f"{SEED_REL}: `{' > '.join(path[:d + 1])}` has no child block")
            frm, to, depth = start + 1, end, ci
    return start, end, depth


def _describe_rows(rows: list[str]) -> str:
    items: list[str] = []
    for l in rows:
        m = _SEED_ROW.match(l)
        if m:
            items.append(m.group(2))
        elif items and _SEED_INPUT.match(l) and "image" in _SEED_INPUT.match(l).group(1):
            items[-1] += " (vision)"
    return _ids_text(items)


def render_seed(text: str, models: list[Model]) -> Span:
    lines = text.splitlines(keepends=True)
    start, end, _ = _resolve(lines, ["llm-pi-ai", "providers", "litellm", "models"])
    models_depth = _indent(lines[start])
    first = next(
        (i for i in range(start + 1, end) if _SEED_ROW.match(lines[i]) and _indent(lines[i]) > models_depth),
        None,
    )
    if first is None:
        raise SourceError(f"{SEED_REL}: the litellm `models:` list has no `- id:` row to anchor the generated span")
    row_indent = _indent(lines[first])
    # The generated rows take the line ending of the row they replace (CRLF checkouts stay CRLF).
    nl = "\r\n" if lines[first].endswith("\r\n") else "\n"
    rows: list[str] = []
    for m in models:
        rows.append(" " * row_indent + f"- id: {m.name}{nl}")
        if m.vision:
            rows.append(" " * (row_indent + 2) + f"input: [text, image]{nl}")
    # `end` excludes trailing blank lines, so a blank separator after the list is kept as-is.
    rendered = "".join(lines[:first]) + "".join(rows) + "".join(lines[end:])
    return Span(
        rel=SEED_REL,
        what="llm-pi-ai > providers > litellm > models",
        committed=_describe_rows(lines[first:end]),
        derived=_describe(models),
        current=text,
        rendered=rendered,
    )


def render_litellm(text: str) -> Span:
    hits = _CHECKSUM.findall(text)
    if len(hits) != 1:
        raise SourceError(f"{LITELLM_REL}: expected exactly one checksum/config annotation, found {len(hits)}")
    committed = hits[0][1]
    derived = config_checksum(text)
    rendered = _CHECKSUM.sub(lambda m: m.group(1) + derived + m.group(3), text, count=1)
    return Span(
        rel=LITELLM_REL,
        what="checksum/config",
        committed=committed,
        derived=derived,
        current=text,
        rendered=rendered,
    )


def render_all(repo: Path) -> tuple[list[Model], list[Span]]:
    litellm_text = _read(repo / LITELLM_REL)
    models = models_from_config(litellm_config_text(litellm_text))
    spans = [
        render_open_webui(_read(repo / OPEN_WEBUI_REL), models),
        render_seed(_read(repo / SEED_REL), models),
        render_litellm(litellm_text),
    ]
    return models, spans


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def cmd_check(repo: Path) -> int:
    models, spans = render_all(repo)
    drift = False
    for s in spans:
        if s.clean:
            print(f"OK {s.rel} {s.what}")
        elif s.committed == s.derived:
            drift = True
            print(
                f"DRIFT {s.rel} {s.what}: ids match {s.derived} but the span differs in formatting "
                f"or carries extra lines; --write normalises it"
            )
        else:
            drift = True
            print(f"DRIFT {s.rel} {s.what}: committed {s.committed} -> derived {s.derived}")
    if not drift:
        print(f"\nall {len(spans)} spans match litellm.yaml's model_list ({len(models)} consumer-visible routes).")
        return 0
    print("\nFAIL")
    print("  * a consumer span disagrees with litellm.yaml's model_list (the source).")
    print("    Run `just af-gen-litellm` (python3 scripts/gen-litellm-consumers.py --write) and review the diff.")
    return 1


def cmd_write(repo: Path) -> int:
    models, spans = render_all(repo)
    changed = False
    for s in spans:
        if s.clean:
            print(f"unchanged {s.rel} {s.what}")
            continue
        (repo / s.rel).write_text(s.rendered, encoding="utf-8", newline="")
        print(f"wrote     {s.rel} {s.what}: {s.committed} -> {s.derived}")
        changed = True
    if not changed:
        print(f"\nnothing to do — the tree already matches litellm.yaml ({len(models)} consumer-visible routes).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify (default); exit 1 on drift")
    group.add_argument("--write", action="store_true", help="rewrite the derived spans in place")
    parser.add_argument("--repo", type=Path, default=REPO, help="repo root (default: this checkout)")
    args = parser.parse_args(argv)

    try:
        if args.write:
            return cmd_write(args.repo)
        return cmd_check(args.repo)
    except SourceError as exc:
        # An unreadable or malformed source is a FAILURE, never a skip: a gate that cannot
        # determine the answer must not report success.
        print(f"ERROR {exc}")
        return 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
