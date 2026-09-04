#!/usr/bin/env python3
"""Print the `rule_files:` entries of a promtool test fixture, one per line.

WHY THIS EXISTS: `promtool test rules` only WARNS when a `rule_files:` entry matches no file —
it does not fail. A fixture that names a rules file which was never extracted (a typo, a
renamed manifest) therefore evaluates ZERO rules, and any test in it whose expectation is "no
alerts" passes vacuously. That is a gate that checks nothing, which this repo has already
shipped once (agentforge-rules.yaml pinned to a job with zero targets). scripts/rules-lint.sh
runs this first and refuses to invoke promtool unless every reference resolves.

FAIL CLOSED: exits 1 if it cannot parse a NONEMPTY list, so `rule_files:` at EOF, written as
inline YAML, or absent entirely is an error rather than an empty happy path. Stdlib only — the
CI runner is not guaranteed PyYAML for this script's sake (rules-lint.sh's own header).

    python3 scripts/promtest-refs.py <fixture.yaml>
"""
import re
import sys

# LF only, on every platform. rules-lint.sh consumes this output directly (no `tr` in a pipe,
# which would put the extractor's exit status behind `set -o pipefail`), so a CRLF here would
# turn every reference into a filename with a trailing CR and fail the gate for the wrong
# reason.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(newline="\n")

# The block runs from `rule_files:` to the next column-0 key, or to end of file.
BLOCK = re.compile(r"^rule_files:[ \t]*$(.*?)(?=^\S|\Z)", re.M | re.S)
ITEM = re.compile(r"^\s*-\s*(\S+)\s*$")


def references(text):
    m = BLOCK.search(text)
    if not m:
        return []
    return [i.group(1) for i in (ITEM.match(line) for line in m.group(1).splitlines()) if i]


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: promtest-refs.py <promtool-test-fixture.yaml>\n")
        return 2
    with open(argv[1], encoding="utf-8") as f:
        refs = references(f.read())
    if not refs:
        sys.stderr.write(
            f"{argv[1]}: no `rule_files:` list entries parsed. A fixture that loads no rules "
            f"passes vacuously, so this is refused rather than skipped.\n")
        return 1
    sys.stdout.write("\n".join(refs) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
