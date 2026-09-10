#!/usr/bin/env python3
"""Generate an agent-team preset from a shipped dsh preset.

A team preset is a COPY of an upstream one. Copying by hand invites drift and
retyping errors -- the same reason cordis.patch.yml's rows are generated from
`--dump-config` rather than typed. This does the copy mechanically and applies
exactly one transformation: disable the named model-facing rows.

  python3 scripts/gen-agent-team.py \
      --source <path to presets/standard/agent.cordis.yml> \
      --out kubernetes/apps/apps/dsh/agent-teams/team-solo.agent.cordis.yml \
      --disable tool-subagent-control,tool-subagent,...

The source lives inside @deepseek-ai/dsh-agent-presets at the pinned DSH_VERSION;
pull it out of a container rather than vendoring the package:

  docker run --rm mirror.gcr.io/library/node:22 sh -c \
    'npm i --prefix /a --no-fund --no-audit @deepseek-ai/dsh@0.1.5-alpha.2 >/dev/null 2>&1;
     cat /a/node_modules/@deepseek-ai/dsh-agent-presets/presets/standard/agent.cordis.yml'
"""
import argparse
import re
import sys

def drop_rows(text, targets):
    """Remove whole rows (header, comments above it, and its body).

    A row a team must NOT have cannot be expressed by disabling it: `disabled`
    still documents the row as part of the composition, and `subagent_fork` in
    particular exists to preserve the parent's route -- keeping it disabled in a
    team whose whole point is re-routing children invites someone to enable it.
    """
    src = text.splitlines(keepends=True)
    out, i, n, hit = [], 0, len(src), set()
    while i < n:
        m = re.match(r"^(\s*)- id: (\S+)\s*$", src[i])
        if not m or m.group(2) not in targets:
            out.append(src[i]); i += 1
            continue
        indent, rid = m.group(1), m.group(2)
        hit.add(rid)
        # Drop the comment block immediately above this row, which describes it.
        while out and re.match(r"^\s*#", out[-1]):
            out.pop()
        while out and out[-1].strip() == "":
            out.pop()
        i += 1
        # Drop the body: everything indented deeper than the row header.
        while i < n and (src[i].strip() == "" or len(src[i]) - len(src[i].lstrip()) > len(indent)):
            if src[i].strip() != "" and re.match(r"^" + indent + r"- id: ", src[i]):
                break
            i += 1
    return "".join(out), hit


def route_rows(text, targets, provider, model):
    """Give named rows an explicit child route via `agentOptions`.

    Spawn children INHERIT the parent's provider/model when nothing overrides
    it, so on a conductor running a different route than its workers this is the
    difference between the team doing what it says and quietly running every
    child on the conductor's model.
    """
    src = text.splitlines(keepends=True)
    out, i, n, hit = [], 0, len(src), set()
    while i < n:
        m = re.match(r"^(\s*)- id: (\S+)\s*$", src[i])
        if not m or m.group(2) not in targets:
            out.append(src[i]); i += 1
            continue
        indent, rid = m.group(1), m.group(2)
        out.append(src[i]); i += 1
        body_indent = indent + "  "
        has_config = False
        # Copy the row body, noting whether it already carries a `config:` block.
        while i < n:
            line = src[i]
            if line.strip() == "":
                out.append(line); i += 1; continue
            cur = len(line) - len(line.lstrip())
            if cur <= len(indent):
                break
            if re.match(r"^" + body_indent + r"config:\s*$", line):
                has_config = True
            out.append(line); i += 1
        if not has_config:
            out.append(f"{body_indent}config:\n")
        key = body_indent + "  "
        out.append(f"{key}agentOptions:\n")
        out.append(f"{key}  provider: {provider}\n")
        out.append(f"{key}  model: {model}\n")
        hit.add(rid)
    return "".join(out), hit


def enable_rows(text, targets):
    """Remove `disabled: true` from named rows, and any comment lines above it.

    Upstream ships some rows dormant (tool-subagent-codex and friends), so a team
    that wants one must actively clear the flag rather than merely not setting it.

    The comments matter as much as the flag. Both this generator's own note and
    upstream's explanation of why a row is dormant sit directly above it, so
    clearing the flag alone leaves "Dormant until ..." standing over a row that is
    now live -- a file that contradicts itself, which is worse than one that is
    merely wrong. They are already in `out` by the time the flag is reached, so
    removing them is a lookback, not a lookahead.
    """
    src = text.splitlines(keepends=True)
    out, i, n, hit = [], 0, len(src), set()
    while i < n:
        m = re.match(r"^(\s*)- id: (\S+)\s*$", src[i])
        if not m or m.group(2) not in targets:
            out.append(src[i]); i += 1
            continue
        indent, rid = m.group(1), m.group(2)
        out.append(src[i]); i += 1
        # Walk the row body, dropping any `disabled: true` at the row's own key depth.
        while i < n:
            line = src[i]
            if line.strip() != "" and len(line) - len(line.lstrip()) <= len(indent):
                break
            if re.match(r"^" + indent + r"  disabled:\s*true\s*$", line):
                hit.add(rid)
                # Drop the comment block immediately preceding the flag.
                while out and re.match(r"^\s*#", out[-1]):
                    out.pop()
                i += 1
                continue
            out.append(line); i += 1
    return "".join(out), hit


def set_keys(text, assignments):
    """Append scalar `key: value` entries to named rows' config blocks.

    `assignments` maps row id -> list of (key, value). Used for real per-row
    policy that is NOT a child route: workflow-worker-thread's
    maxConcurrentAgents / maxTotalAgents, for instance, which are the only
    fan-out ceilings dsh exposes declaratively.
    """
    src = text.splitlines(keepends=True)
    out, i, n, hit = [], 0, len(src), set()
    while i < n:
        m = re.match(r"^(\s*)- id: (\S+)\s*$", src[i])
        if not m or m.group(2) not in assignments:
            out.append(src[i]); i += 1
            continue
        indent, rid = m.group(1), m.group(2)
        start_of_row = len(out)
        out.append(src[i]); i += 1
        body_indent = indent + "  "
        has_config = False
        while i < n:
            line = src[i]
            if line.strip() == "":
                out.append(line); i += 1; continue
            if len(line) - len(line.lstrip()) <= len(indent):
                break
            if re.match(r"^" + body_indent + r"config:\s*$", line):
                has_config = True
            out.append(line); i += 1
        if not has_config:
            out.append(f"{body_indent}config:\n")
        # REPLACE an existing key rather than appending beside it. Appending
        # produces a duplicate mapping key, which YAML either rejects or resolves
        # last-wins -- and either way the file then shows two values for one
        # bound, which is exactly the kind of thing nobody reads twice.
        # tool-ralph ships `maxRounds: 64`, so this path is the normal case.
        #
        # ONLY AT THE CONFIG MAPPING'S OWN DEPTH. Matching any indentation inside
        # the row lets a key nested under `agentOptions` (which has its own
        # `provider` and `model`) be overwritten instead of the direct one -- and
        # when both exist, whichever appears first in the file wins, which is
        # position luck rather than intent. `tool-subagent` on the conductor has
        # `provider` at BOTH depths, so this is a live collision, not a
        # hypothetical.
        config_depth = body_indent + "  "
        for key, value in assignments[rid]:
            pat = re.compile(r"^" + re.escape(config_depth) + re.escape(key) + r":\s")
            replaced = False
            for idx in range(start_of_row, len(out)):
                if pat.match(out[idx]):
                    out[idx] = f"{config_depth}{key}: {value}\n"
                    replaced = True
                    break
            if not replaced:
                out.append(f"{config_depth}{key}: {value}\n")
        hit.add(rid)
    return "".join(out), hit


def disable_rows(text, targets, note=()):
    """Insert `disabled: true` (preceded by `note`) after each target row's `name:` line.

    Row shape in these files is always:
        - id: <id>
          name: '<package>'
          [config: ...]
    so the flag goes immediately after `name:`, at the row's own indent + 2. A
    row that already carries the flag is left alone rather than doubled.
    """
    src = text.splitlines(keepends=True)
    out, i, n, hit = [], 0, len(src), set()
    while i < n:
        line = src[i]
        m = re.match(r"^(\s*)- id: (\S+)\s*$", line)
        if not m or m.group(2) not in targets:
            out.append(line)
            i += 1
            continue
        indent, rid = m.group(1), m.group(2)
        out.append(line)
        i += 1
        if i < n and re.match(r"^\s+name:", src[i]):
            out.append(src[i])
            i += 1
        # Already dormant upstream (tool-subagent-codex and friends)? leave it
        # exactly as upstream wrote it -- including its own comment, which sits
        # ABOVE the row and is therefore already in `out`.
        if i < n and re.match(r"^\s+disabled:\s*true", src[i]):
            out.append(src[i])
            i += 1
            hit.add(rid)
            continue
        # The note is emitted by the GENERATOR, not hand-added afterwards. A
        # comment typed into the output by hand makes the file unreproducible:
        # re-running against a bumped upstream then yields comment-only diffs on
        # every row, and the "a dsh bump is a reviewable diff" premise dies in
        # the noise.
        # Split on physical lines: a note carrying an embedded newline would
        # otherwise emit its second line UNCOMMENTED, straight into the YAML.
        # `--note 'text\nbroken: ['` is a legal shell argument and would produce a
        # parse error in the composition -- which surfaces as a broken preset
        # card, not as a failure anywhere anyone is watching.
        for entry in note:
            for line in entry.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                out.append(f"{indent}  # {line}\n" if line.strip() else f"{indent}  #\n")
        out.append(f"{indent}  disabled: true\n")
        hit.add(rid)
    return "".join(out), hit

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--disable", default="", help="comma-separated row ids")
    ap.add_argument("--drop", default="", help="comma-separated row ids to REMOVE entirely")
    ap.add_argument("--enable", default="", help="comma-separated row ids to clear `disabled` from")
    ap.add_argument("--route", default="", help="comma-separated row ids to give an explicit child route")
    ap.add_argument("--route-provider", default="", help="settings.yaml provider name for --route rows")
    ap.add_argument("--route-model", default="", help="model id for --route rows")
    ap.add_argument("--set", action="append", default=[], metavar="ID:KEY=VALUE",
                    help="append a scalar config key to a row; repeatable")
    ap.add_argument("--header", default="", help="file whose contents are prepended")
    ap.add_argument(
        "--note",
        action="append",
        default=[],
        help="comment line emitted above each inserted `disabled: true`; repeatable",
    )
    a = ap.parse_args()
    targets = {t for t in a.disable.split(",") if t}
    text = open(a.source, encoding="utf-8").read()
    missing = set()

    dropped = {t for t in a.drop.split(",") if t}
    if dropped:
        text, hit_d = drop_rows(text, dropped)
        missing |= dropped - hit_d

    routed = {t for t in a.route.split(",") if t}
    if routed:
        if not a.route_provider or not a.route_model:
            print("ERROR: --route needs --route-provider and --route-model", file=sys.stderr)
            return 1
        text, hit_r = route_rows(text, routed, a.route_provider, a.route_model)
        missing |= routed - hit_r

    enabled = {t for t in a.enable.split(",") if t}
    if enabled:
        text, hit_e = enable_rows(text, enabled)
        missing |= enabled - hit_e

    assignments = {}
    for item in getattr(a, "set"):
        try:
            rid, kv = item.split(":", 1)
            key, value = kv.split("=", 1)
        except ValueError:
            print(f"ERROR: --set expects ID:KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        assignments.setdefault(rid, []).append((key, value))
    if assignments:
        text, hit_s = set_keys(text, assignments)
        missing |= set(assignments) - hit_s

    body, hit = disable_rows(text, targets, a.note)
    missing |= targets - hit
    if missing:
        # Fail loud: a row id that no longer exists upstream means the team is
        # shipping a capability it believes it disabled.
        print(f"ERROR: rows not found in source: {sorted(missing)}", file=sys.stderr)
        return 1
    header = open(a.header, encoding="utf-8").read() if a.header else ""
    open(a.out, "w", encoding="utf-8").write(header + body)
    print(f"wrote {a.out}: {len(hit)} rows disabled")
    return 0

if __name__ == "__main__":
    sys.exit(main())
