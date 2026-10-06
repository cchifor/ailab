#!/usr/bin/env bash
# Behavioural test for the window-size-follows-terminal block in tmux.conf.j2 (plain bash, following
# tests/test-tmux-persistence.sh).
#
# The bug (dev-worker-3, 2026-10-04): relay-connector attaches as a control client with
# ignore-size and implements the browser's "Fit pane" with `resize-window -x -y`. tmux sets the
# session's window-size to `manual` and never resets it. Every window in the session stays at the
# browser's size, and the user's bigger terminal is filled with dots.
#
# This drives a PRIVATE tmux server (never the user's) loaded with the template's block, verbatim:
# a control client in the role of relay, plus a real pty client in the role of the user's terminal.
#
# Usage: bash ansible/roles/dev_worker/tests/test-tmux-window-size.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF_TMPL="$HERE/../templates/tmux.conf.j2"
TASKS="$HERE/../tasks/tmux.yml"
[ -r "$CONF_TMPL" ] || { echo "FATAL: cannot read $CONF_TMPL"; exit 2; }

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  ok       %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL     %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  NOT RUN  %s\n' "$1"; SKIP=$((SKIP+1)); }

WORK="$(mktemp -d)"
SOCK="wsz-test-$$"
T=(tmux -L "$SOCK")
cleanup() { command -v tmux >/dev/null 2>&1 && "${T[@]}" kill-server 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

sed -n '/^# BEGIN window-size-follows-terminal$/,/^# END window-size-follows-terminal$/p' "$CONF_TMPL" |
	grep -v '^#' >"$WORK/block.conf"

echo "[A] template wiring"
[ -s "$WORK/block.conf" ] && ok "tmux.conf.j2 carries the marked window-size block" ||
	bad "tmux.conf.j2 has no '# BEGIN/END window-size-follows-terminal' block"
for h in client-attached client-resized client-focus-in client-session-changed; do
	grep -q "^set-hook -g $h .*client_control_mode.*set-option -u window-size" "$WORK/block.conf" &&
		ok "hook $h clears the session override, control clients excluded" || bad "hook $h missing or unguarded"
done
grep -q '^set -g focus-events on$' "$CONF_TMPL" && ok "focus-events on (client-focus-in needs it)" ||
	bad "focus-events is not on: client-focus-in would never fire"
grep -q 'BEGIN window-size-follows-terminal' "$TASKS" && ok "tmux.yml pushes the block into running servers" ||
	bad "tmux.yml does not push the block live: running servers keep the bug until restart"

echo "[B] behaviour (private tmux server)"
if ! command -v tmux >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
	skip "tmux or python3 absent — behaviour not exercised"
else
	export TERM=xterm-256color
	"${T[@]}" -f "$WORK/block.conf" new-session -d -s sessions -x 120 -y 40 "sleep 300"
	relay() { (echo "resize-window -x $1 -y $2"; sleep 1) | "${T[@]}" -C attach -t sessions -f ignore-size >/dev/null 2>&1; }
	wsz() { "${T[@]}" show -t sessions -v window-size 2>/dev/null; }
	win() { "${T[@]}" display -p -t sessions '#{window_width}x#{window_height}'; }

	relay 90 25
	[ "$(wsz)" = manual ] && [ "$(win)" = 90x25 ] &&
		ok "relay Fit (control client) still resizes and leaves 'manual' while no terminal is attached" ||
		bad "relay Fit with no terminal: window-size=[$(wsz)] win=$(win), expected manual 90x25"

	# The user's terminal: a real pty client at 150x45 that resizes to 140x42 on request.
	python3 - "$SOCK" "$WORK" <<'PY' &
import fcntl, os, pty, select, signal, struct, sys, termios, time
sock, work = sys.argv[1], sys.argv[2]
pid, fd = pty.fork()
if pid == 0:
    os.execvp("tmux", ["tmux", "-L", sock, "attach", "-t", "sessions"])
def size(c, r): fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", r, c, 0, 0))
size(150, 45)
end, resized = time.time() + 20, False
while time.time() < end and not os.path.exists(work + "/stop"):
    r, _, _ = select.select([fd], [], [], 0.2)
    try:
        if r: os.read(fd, 65536)
    except OSError:
        break
    if not resized and os.path.exists(work + "/resize"):
        size(140, 42); os.kill(pid, signal.SIGWINCH); resized = True
os.kill(pid, signal.SIGTERM)
PY
	PYPID=$!
	for _ in $(seq 1 25); do [ "$(win)" = 150x44 ] && break; sleep 0.2; done
	[ -z "$(wsz)" ] && [ "$(win)" = 150x44 ] &&
		ok "a real terminal attaching clears 'manual' and the window follows it (150x44)" ||
		bad "after terminal attach: window-size=[$(wsz)] win=$(win), expected unset 150x44 — the reported bug"

	relay 70 20
	[ "$(wsz)" = manual ] && [ "$(win)" = 70x20 ] &&
		ok "relay Fit still works while the terminal is attached (70x20)" ||
		bad "relay Fit with terminal attached: window-size=[$(wsz)] win=$(win)"

	touch "$WORK/resize"
	for _ in $(seq 1 25); do [ "$(win)" = 140x41 ] && break; sleep 0.2; done
	[ -z "$(wsz)" ] && [ "$(win)" = 140x41 ] &&
		ok "the terminal's next resize takes the size back (140x41)" ||
		bad "after terminal resize: window-size=[$(wsz)] win=$(win), expected unset 140x41"
	touch "$WORK/stop"; wait "$PYPID" 2>/dev/null
fi

echo
echo "passed=$PASS failed=$FAIL not-run=$SKIP"
[ "$FAIL" -eq 0 ]
