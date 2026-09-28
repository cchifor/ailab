#!/usr/bin/env bash
# The terminal-title contract between the dev-worker role and the workstation paste helpers.
#
# The role's tmux sets the title to "#h [<user>@<ip>] #S:#W"; dw-paste-windows.ps1, -macos.lua and
# -linux.sh find a dev-worker window, and where to upload, by that [user@ip] marker. Nothing fails
# loudly if either side drifts: the helpers just stop recognising the window and Ctrl+Shift+V pastes
# the laptop path again. So this pins both sides. CI: .gitea/workflows/dev-worker-scripts.yaml.
#
# Usage: bash scripts/dw-paste/test-marker.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/../.."
CONF="$ROOT/ansible/roles/dev_worker/templates/tmux.conf.j2"
TASKS="$ROOT/ansible/roles/dev_worker/tasks/tmux.yml"
FAILS=0
ok() { echo "  ok: $1"; }
bad() { echo "  FAIL: $1"; FAILS=$((FAILS + 1)); }

WANT='#h [#{client_user}@{{ ansible_default_ipv4.address }}] #S:#W'
grep -q '^set -g set-titles on$' "$CONF" && ok "tmux.conf.j2 turns set-titles on" || bad "tmux.conf.j2 must 'set -g set-titles on'"
grep -qF "set -g set-titles-string \"$WANT\"" "$CONF" && ok "tmux.conf.j2 title carries the [user@ip] marker" || bad "tmux.conf.j2 set-titles-string drifted from: $WANT"
grep -qF "      - \"$WANT\"" "$TASKS" && ok "tmux.yml pushes the same title to running servers" || bad "tmux.yml's live title differs from tmux.conf.j2"

TITLE='dev-worker-3 [c4@192.168.0.10] main:claude'
NOPE='dev-worker-3 main:claude'

# Linux helper: its own MARKER variable, with bash's regex engine.
eval "$(grep -m1 '^MARKER=' "$HERE/dw-paste-linux.sh")"
if [[ "$TITLE" =~ $MARKER ]] && [ "${BASH_REMATCH[1]}@${BASH_REMATCH[2]}" = c4@192.168.0.10 ]; then ok "linux helper extracts c4@192.168.0.10"; else bad "linux helper MARKER does not match the title"; fi
[[ "$NOPE" =~ $MARKER ]] && bad "linux helper matches a title without the marker" || ok "linux helper ignores titles without the marker"
bash -n "$HERE/dw-paste-linux.sh" && ok "dw-paste-linux.sh parses" || bad "dw-paste-linux.sh has a syntax error"

# Windows helper: its $MarkerRegex, with Python's engine (same syntax for these constructs).
python3 - "$HERE/dw-paste-windows.ps1" "$TITLE" "$NOPE" <<'PY' && ok "windows helper extracts c4@192.168.0.10 and ignores unmarked titles" || bad "windows helper \$MarkerRegex does not match the contract"
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
rx = re.search(r"^\$MarkerRegex = '([^']+)'", src, re.M).group(1)
m = re.search(rx, sys.argv[2])
sys.exit(0 if m and f"{m.group(1)}@{m.group(2)}" == "c4@192.168.0.10" and not re.search(rx, sys.argv[3]) else 1)
PY

# macOS helper: no Lua on the runners, so pin the pattern text itself.
grep -qF '%[([%l_][%w_.-]*)@(%d+%.%d+%.%d+%.%d+)%]' "$HERE/dw-paste-macos.lua" && ok "macos helper pattern unchanged" || bad "macos helper's marker pattern changed: re-check it against the title"

echo
[ "$FAILS" = 0 ] && { echo PASS; exit 0; }
echo "FAILED: $FAILS"; exit 1
