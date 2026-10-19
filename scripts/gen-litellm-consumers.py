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
     the picker's Local group. The rewrite is structural: the value is parsed with json.loads,
     `"0".model_ids` is replaced, and the object is serialised back as canonical compact JSON
     (json.dumps with separators (",", ":"), the form the file uses), so every other key and
     value keeps its order and a same-named key nested anywhere else is never touched. The
     committed value must already BE that canonical form, byte for byte, else it is a FAILURE
     telling you to normalise it once by hand; the YAML line keeps its exact prefix/suffix.
  2. the `checksum/config` pod-template annotation  — apps/ai/litellm.yaml
     The value the documented recipe produces (`yq -r ... | sha256sum | cut -c1-12`), derived with
     check-inline-hashes' own function so the generator and the CI gate are one derivation. The
     anchor is the ONE uncommented `checksum/config: "<12 hex>"` line (leading whitespace, then
     the key); a `#`-prefixed mention is never the anchor, and zero or several real lines is a
     FAILURE.

dsh was span 2 until 2026-10-19 (the litellm provider's `models:` rows in its settings seed); it
now takes every model from llm-router (ADR 0039) and is no longer a consumer.

SELECTION RULE — exactly what Open WebUI's Local group takes. A route is listed when ALL THREE
hold:
  * `litellm_params.api_base` is present and its host is a private IPv4 address (10/8,
    172.16/12, 192.168/16) or ends with `.svc.cluster.local`. Entries with no api_base (the paid
    providers, their embedding model) are never listed: they are what connection "1" discovers
    as External.
  * `model_info.mode` is unset or `chat`. The Local group is a chat picker, so a self-hosted
    embedding / rerank / transcription route (mode is LiteLLM's own key for that) is never
    listed even though its api_base is private.
  * `model_info.hidden` is not `true`. That key is the explicit opt-out for a self-hosted chat
    route that should stay reachable by name but not be advertised (a fallback-only deployment,
    a route under test). Set it on the LiteLLM entry; never hand-edit the consumer. What it
    does, precisely: the route leaves Open WebUI's Local group. It does NOT leave Open WebUI
    altogether — connection "1" has no model_ids, so it still discovers the route from
    /v1/models and shows it under External, because `hidden` is not a key LiteLLM knows
    (model_info is free-form) and the router keeps advertising the name. The value must be a
    YAML boolean on EVERY entry that carries model_info, self-hosted or not, checked before the
    other two filters: `hidden: "true"`, `hidden: 1` and the like are a FAILURE naming the entry,
    never silently visible and never skipped because another filter excluded the entry first.
`model_info.dsh_only` is INERT. It was dsh's per-route opt-in to a paid route; litellm.yaml still
carries one `dsh_only: false`, left in place because editing config.yaml rolls the shared LiteLLM
pod, so the key is ignored whatever its value — it neither lists nor withdraws a route.
Order is model_list order, de-duplicated on model_name (LiteLLM allows several deployments under
one name — qwen3.8-27b-ailab on node2 and node3 — and the Local group lists the name once). A
selected model_name must be a non-empty string without a single quote: it is written,
JSON-escaped, inside a single-quoted YAML scalar in open-webui.yaml, and json.dumps does not
escape `'`. An EMPTY selection is refused in both modes: a rule that matches nothing is a broken
source, not an instruction to blank the picker.

USAGE
  python3 scripts/gen-litellm-consumers.py            # check (default) — exit 1 on drift
  python3 scripts/gen-litellm-consumers.py --check    # same, explicitly (CI)
  python3 scripts/gen-litellm-consumers.py --write    # rewrite both spans in place

`--check` renders both spans into memory and compares them byte for byte with what is on disk, so
"the check passes" and "running --write changes nothing" are the same statement; both modes are
idempotent. Non-canonical JSON in open-webui.yaml is refused rather than normalised (see span 1).
Files are read and written with their line endings intact (newline=""), so a CRLF checkout is
rewritten only inside the owned spans and compared as real bytes. Any parse failure is a FAILURE,
never a skip. Stdlib + PyYAML, which is the CI runner's system package (the same assumption
scripts/tests/test_worker_probes.py makes).
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

#: The private IPv4 ranges the rule admits. Deliberately NOT ipaddress's `is_private`, which also
#: says yes to loopback, link-local and the CGNAT block — none of which is a backend here.
PRIVATE_V4 = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
CLUSTER_SUFFIX = ".svc.cluster.local"

_CONFIG_MARKER = re.compile(r"^[ ]*config\.yaml:\s*\|\s*$")
#: The annotation line itself: leading whitespace, the key, the quoted 12-hex value. Matched per
#: line, so a `#`-prefixed mention of the key is never the anchor.
_CHECKSUM = re.compile(r'^[ \t]*checksum/config:[ \t]*"([0-9a-f]{12})"')
_WEBUI_LINE = re.compile(r"^(\s*- \{ name: OPENAI_API_CONFIGS, value: ')(.*)(' \}\s*)$")


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


def _private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    try:
        addr = ipaddress.IPv4Address(host)
    except ValueError:
        return host.endswith(CLUSTER_SUFFIX) and len(host) > len(CLUSTER_SUFFIX)
    return any(addr in net for net in PRIVATE_V4)


def _hidden(entry: dict, info) -> bool:
    """`model_info.hidden`, validated before any filter reads it.

    Raises SourceError when the key is present but is not a YAML boolean, on EVERY entry that
    carries model_info: the opt-out must never fail open because of a quoted "true" or a 1, and its
    type rule must not depend on which entry it is on or on which filter would have excluded it.
    """
    if isinstance(info, dict) and "hidden" in info and not isinstance(info["hidden"], bool):
        raise SourceError(
            f"{LITELLM_REL}: model_list entry {entry.get('model_name')!r}: model_info.hidden must be "
            f"a YAML boolean (true/false), got {info['hidden']!r}"
        )
    return isinstance(info, dict) and info.get("hidden") is True


def consumer_visible(entry: dict) -> bool:
    """The Open WebUI Local-group rule: a SELF-HOSTED chat route that is not hidden.

    `model_info.dsh_only` is deliberately not read (see the module docstring).
    """
    info = entry.get("model_info") or {}
    if _hidden(entry, info):
        return False
    mode = info.get("mode") if isinstance(info, dict) else None
    if mode is not None and mode != "chat":
        return False
    params = entry.get("litellm_params") or {}
    api_base = params.get("api_base") if isinstance(params, dict) else None
    host = urlsplit(api_base).hostname if isinstance(api_base, str) and api_base else None
    return bool(host) and _private_host(host)


def check_model_name(name: str) -> None:
    """The name must survive being written into open-webui.yaml (see the module docstring).

    json.dumps escapes everything JSON needs escaped, so the one hazard left is a single quote,
    which would end the single-quoted YAML scalar the JSON sits in.
    """
    if "'" in name:
        raise SourceError(
            f"{LITELLM_REL}: model_name {name!r} carries a single quote, which would end the "
            f"single-quoted OPENAI_API_CONFIGS scalar in {OPEN_WEBUI_REL}"
        )


def models_from_config(config_text: str) -> list[str]:
    """The selected model_names, in model_list order, each once."""
    try:
        cfg = yaml.safe_load(config_text)
    except yaml.YAMLError as exc:
        raise SourceError(f"{LITELLM_REL}: config.yaml does not parse: {exc}") from exc
    model_list = cfg.get("model_list") if isinstance(cfg, dict) else None
    if not isinstance(model_list, list) or not model_list:
        raise SourceError(f"{LITELLM_REL}: config.yaml has no model_list sequence")
    out: list[str] = []
    for i, entry in enumerate(model_list):
        if not isinstance(entry, dict) or not isinstance(entry.get("model_name"), str) or not entry["model_name"]:
            raise SourceError(f"{LITELLM_REL}: model_list[{i}] has no non-empty string model_name")
        if not consumer_visible(entry):
            continue
        name = entry["model_name"]
        check_model_name(name)
        # A model_name may legitimately appear more than once: that is how LiteLLM expresses several
        # DEPLOYMENTS of one model for the router to balance across (qwen3.8-27b-ailab runs on node2
        # and node3). The picker must still see exactly ONE row, so later entries fold into the
        # first. A row is only the name, so there is nothing for two deployments to disagree on.
        if name not in out:
            out.append(name)
    if not out:
        raise SourceError(
            f"{LITELLM_REL}: no consumer-visible route in model_list; refusing to render an empty "
            f"Open WebUI Local group"
        )
    return out


def load_models(repo: Path) -> list[str]:
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
        # Both must hold: the bytes are what --write would produce AND what the file says
        # (`committed`) is the derived value.
        return self.current == self.rendered and self.committed == self.derived


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


def _compact(obj) -> str:
    """The one JSON form open-webui.yaml uses: json.dumps, no whitespace, key order kept."""
    return json.dumps(obj, separators=(",", ":"))


def render_open_webui(text: str, models: list[str]) -> Span:
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
        cfg = json.loads(json_text)
    except ValueError as exc:
        raise SourceError(f"{OPEN_WEBUI_REL}: OPENAI_API_CONFIGS is not valid JSON: {exc}") from exc
    # The value is rewritten by re-serialising the parsed object, so the committed text must
    # already be that serialisation: otherwise "the ids match" and "the bytes match" would part.
    if _compact(cfg) != json_text:
        raise SourceError(f"{OPEN_WEBUI_REL}: OPENAI_API_CONFIGS is not canonical compact JSON; normalise it once by hand")
    conn0 = cfg.get("0") if isinstance(cfg, dict) else None
    if not isinstance(conn0, dict) or "model_ids" not in conn0:
        raise SourceError(f'{OPEN_WEBUI_REL}: OPENAI_API_CONFIGS has no object at "0" carrying "model_ids"')
    current_ids = conn0["model_ids"]
    if not isinstance(current_ids, list) or not all(isinstance(x, str) for x in current_ids):
        raise SourceError(f'{OPEN_WEBUI_REL}: OPENAI_API_CONFIGS "0".model_ids is not a list of strings')

    ids = list(models)
    conn0["model_ids"] = ids
    lines[hits[0]] = prefix + _compact(cfg) + suffix
    return Span(
        rel=OPEN_WEBUI_REL,
        what='OPENAI_API_CONFIGS "0".model_ids',
        committed=_ids_text(current_ids),
        derived=_ids_text(ids),
        current=text,
        rendered="".join(lines),
    )


def render_litellm(text: str) -> Span:
    lines = text.splitlines(keepends=True)
    hits = [(i, m) for i, l in enumerate(lines) if (m := _CHECKSUM.match(l))]
    if len(hits) != 1:
        raise SourceError(f"{LITELLM_REL}: expected exactly one checksum/config annotation, found {len(hits)}")
    i, m = hits[0]
    committed = m.group(1)
    derived = config_checksum(text)
    lines[i] = lines[i][:m.start(1)] + derived + lines[i][m.end(1):]
    rendered = "".join(lines)
    return Span(
        rel=LITELLM_REL,
        what="checksum/config",
        committed=committed,
        derived=derived,
        current=text,
        rendered=rendered,
    )


def render_all(repo: Path) -> tuple[list[str], list[Span]]:
    litellm_text = _read(repo / LITELLM_REL)
    models = models_from_config(litellm_config_text(litellm_text))
    spans = [
        render_open_webui(_read(repo / OPEN_WEBUI_REL), models),
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
        else:
            drift = True
            print(f"DRIFT {s.rel} {s.what}: committed {s.committed} -> derived {s.derived}")
    if not drift:
        print(f"\nboth spans match litellm.yaml's model_list ({len(models)} consumer-visible routes).")
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
