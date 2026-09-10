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
    body, hit = disable_rows(text, targets, a.note)
    missing = targets - hit
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
