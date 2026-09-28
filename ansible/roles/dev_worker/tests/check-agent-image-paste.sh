#!/usr/bin/env bash
# Does a bracketed-pasted image path still attach as [Image #N] in Claude Code and Codex?
#
# Everything that hands files to the agents (the web terminal's paste, scripts/dw-paste.ps1, herdr)
# relies on that behaviour, and neither CLI promises it: Claude Code self-updates and Codex is only a
# version floor. Measured 2026-09-28 (Claude Code 2.1.283, codex-cli 0.153.4): a single path attaches
# in both; a paste holding TWO paths attaches each image in Claude and NOTHING in Codex — hence one
# path per paste everywhere.
#
# Runs each agent in a THROWAWAY tmux server (-L), pastes with `paste-buffer -p` (bracketed, as tmux
# does for prefix+]) and never presses Enter, so no prompt is ever sent. It does not answer startup
# dialogs (effort pickers, folder trust, update offers) — that would change the operator's settings —
# it reports NOT RUN instead; open the agent once interactively in DIR, answer, and re-run.
# Manual, on a dev-worker, after agent upgrades:
#
#   bash ansible/roles/dev_worker/tests/check-agent-image-paste.sh [DIR]   (default DIR=/workspace/c4)
#   CODEX_DIR=/workspace/c4/ailab bash .../check-agent-image-paste.sh      (per-agent: CLAUDE_DIR/CODEX_DIR)
#
# Each agent's directory must be one it already trusts. Exit: 0 all attached, 1 a check failed, 3 not run.
set -uo pipefail

DIR="${1:-/workspace/c4}"
SOCK="agentpaste$$"
T="tmux -L $SOCK -f /dev/null"
W="$(mktemp -d)"
cleanup() { $T kill-server 2>/dev/null; rm -rf "$W"; }
trap cleanup EXIT

python3 - "$W/probe.png" <<'PY'
import struct, sys, zlib
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
raw = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY

rc=0
for agent in claude codex; do
	bin="$(command -v "$agent" || echo "$HOME/.npm-global/bin/$agent")"
	[ -x "$bin" ] || { echo "NOT RUN $agent: not installed"; [ "$rc" = 0 ] && rc=3; continue; }
	dir="$DIR"
	[ "$agent" = claude ] && dir="${CLAUDE_DIR:-$DIR}"
	[ "$agent" = codex ] && dir="${CODEX_DIR:-$DIR}"
	$T new-session -d -s "$agent" -x 200 -y 50 -c "$dir" "$bin"
	sleep 12
	screen="$($T capture-pane -p -t "$agent" 2>/dev/null || true)"
	if [ -z "$screen" ] || grep -qiE "Enter to confirm|Press enter to continue|trust (this|the contents)|Update available|Use .* effort by default" <<<"$screen"; then
		echo "NOT RUN $agent: it exited or is showing a startup dialog in $dir — open it there once, answer, re-run"
		[ "$rc" = 0 ] && rc=3
		continue
	fi
	$T set-buffer -b probe "$W/probe.png"
	$T paste-buffer -p -b probe -t "$agent"
	sleep 3
	if $T capture-pane -p -t "$agent" | grep -q '\[Image #1\]'; then
		echo "PASS $agent: a bracketed-pasted image path attaches as [Image #1]"
	else
		echo "FAIL $agent: the pasted image path did not attach. Last lines:"
		$T capture-pane -p -t "$agent" | grep -v '^\s*$' | tail -5 | sed 's/^/    /'
		rc=1 # a failure outranks a not-run
	fi
	$T kill-session -t "$agent" 2>/dev/null
done
exit "$rc"
