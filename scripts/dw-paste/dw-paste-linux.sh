#!/usr/bin/env bash
# dw-paste for Linux desktops: bound to Ctrl+Shift+V, it uploads a clipboard image (screenshot) or
# copied files to the dev-worker the focused terminal is attached to and pastes their paths into the
# focused tmux pane, where Claude Code / Codex attach them. Anything else still pastes normally.
#
# How it knows the worker: the dev-worker tmux sets the terminal title to
# "<host> [<user>@<ip>] <session>:<window>" (ansible role dev_worker, tmux.conf.j2). On X11 the
# focused window's title is read with xdotool; Wayland does not let a program read another window's
# title, so there the worker comes from DW_PASTE_TARGET or ~/.config/dw-paste/target (user@ip).
#
# Because the desktop owns Ctrl+Shift+V while this is bound, this script also does the ordinary
# paste: it copies the clipboard to the PRIMARY selection and presses Shift+Insert, which terminals
# and most applications treat as paste. Uploaded paths are pasted the same way, one path per paste
# (Codex only attaches an image when a paste holds exactly one path), each followed by a typed space;
# never Enter. The clipboard is restored afterwards.
#
# Install:
#   X11:     sudo apt install xdotool xclip          Wayland: sudo apt install wl-clipboard wtype
#   GNOME:   bash scripts/dw-paste/dw-paste-linux.sh --install-gnome   (binds Ctrl+Shift+V)
#   others:  bind Ctrl+Shift+V to "<repo>/scripts/dw-paste/dw-paste-linux.sh" in the desktop's
#            keyboard-shortcut settings (KDE: System Settings > Shortcuts > Custom Shortcuts).
#   --self-test user@ip   upload the clipboard and print the remote paths (no keystrokes)
# Needs ssh/scp with key auth to the worker and its host key accepted (ssh to it once).
# Log: ~/.cache/dw-paste.log. Docs: scripts/dw-paste/README.md.
set -uo pipefail

MAX_BYTES=$((64 * 1024 * 1024))
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/dw-paste.log"
MARKER='\[([a-z_][a-z0-9_.-]*)@([0-9]{1,3}(\.[0-9]{1,3}){3})\]'
TERMINALS='gnome-terminal-server|gnome-terminal|konsole|kitty|alacritty|wezterm-gui|xfce4-terminal|tilix|terminator|xterm|urxvt|foot|ghostty|ptyxis'
mkdir -p "$(dirname "$LOG")"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
notify() { command -v notify-send >/dev/null && notify-send -a dw-paste "dw-paste" "$*"; log "$*"; }

WAYLAND=0
[ "${XDG_SESSION_TYPE:-}" = wayland ] && WAYLAND=1

if [ "${1:-}" = --install-gnome ]; then
	self="$(readlink -f "$0")"
	base=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
	path="$base/dw-paste/"
	existing="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
	case "$existing" in
	*"$path"*) ;;
	"@as []" | "[]") gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$path']" ;;
	*) gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "${existing%]}, '$path']" ;;
	esac
	schema="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$path"
	gsettings set "$schema" name 'dw-paste'
	gsettings set "$schema" command "$self"
	gsettings set "$schema" binding '<Control><Shift>v'
	echo "bound Ctrl+Shift+V to $self (GNOME custom shortcut 'dw-paste')"
	exit 0
fi

# ---------------------------------------------------------------- clipboard primitives
clip_types() { if [ "$WAYLAND" = 1 ]; then wl-paste --list-types 2>/dev/null; else xclip -selection clipboard -t TARGETS -o 2>/dev/null; fi; }
clip_get() { if [ "$WAYLAND" = 1 ]; then wl-paste --no-newline --type "$1" 2>/dev/null; else xclip -selection clipboard -t "$1" -o 2>/dev/null; fi; }
clip_set() { # <type|text> <file>  — both CLIPBOARD and PRIMARY, so Shift+Insert pastes it everywhere
	# "text" = let the tool offer its standard text targets (UTF8_STRING/STRING/...): terminals ask
	# for those, and a selection offered only as text/plain pastes nothing into xterm.
	local t=(); [ "$1" != text ] && t=(-t "$1")
	local wt=(); [ "$1" != text ] && wt=(--type "$1")
	if [ "$WAYLAND" = 1 ]; then
		wl-copy "${wt[@]}" <"$2"
		wl-copy --primary "${wt[@]}" <"$2"
	else
		xclip -selection clipboard "${t[@]}" -i "$2"
		xclip -selection primary "${t[@]}" -i "$2"
	fi
}
# wtype needs the virtual-keyboard protocol, which wlroots compositors (sway, Hyprland, ...) have and
# GNOME's Mutter does not: there it fails, and the failure must be visible rather than a silent no-op.
press_paste() {
	if [ "$WAYLAND" = 1 ]; then wtype -M shift -k Insert -m shift; else xdotool key --clearmodifiers shift+Insert; fi ||
		{ notify "could not send the paste keystroke (on GNOME Wayland wtype is unsupported: use an X11 session or the web terminal)"; return 1; }
}
type_space() { if [ "$WAYLAND" = 1 ]; then wtype ' '; else xdotool type --clearmodifiers ' '; fi; }
focused_window() { xdotool getactivewindow 2>/dev/null || xdotool getwindowfocus 2>/dev/null; }

# The ordinary paste this script stands in for: the clipboard, via PRIMARY + Shift+Insert. With no
# text on the clipboard there is nothing to paste — and pressing Shift+Insert anyway would paste
# whatever was last SELECTED (PRIMARY), possibly a command with a newline. So do nothing.
plain_paste() {
	local t f
	t="$(clip_types | grep -m1 -E '^(text/plain;charset=utf-8|UTF8_STRING|text/plain)$' || true)"
	[ -n "$t" ] || return 0
	f="$(mktemp)"
	clip_get "$t" >"$f"
	if [ "$WAYLAND" = 1 ]; then wl-copy --primary <"$f"; else xclip -selection primary -i "$f"; fi
	rm -f "$f"
	press_paste
}

# ---------------------------------------------------------------- which worker
target_of_focus() {
	if [ -n "${DW_PASTE_TARGET:-}" ]; then echo "$DW_PASTE_TARGET"; return; fi
	if [ "$WAYLAND" = 1 ]; then
		[ -r "$HOME/.config/dw-paste/target" ] && head -1 "$HOME/.config/dw-paste/target"
		return
	fi
	local win title pid exe
	# getactivewindow needs a window manager (_NET_ACTIVE_WINDOW); fall back to the keyboard focus.
	win="$(focused_window)" || return
	title="$(xdotool getwindowname "$win" 2>/dev/null)"
	pid="$(xdotool getwindowpid "$win" 2>/dev/null)"
	# The executable's name, not `ps -o comm=`: the kernel truncates comm to 15 characters, so
	# gnome-terminal-server would show as "gnome-terminal-" and never match.
	exe="$( [ -n "$pid" ] && basename "$(readlink -f "/proc/$pid/exe" 2>/dev/null)" 2>/dev/null)"
	[[ "$exe" =~ ^($TERMINALS)$ ]] || return
	[[ "$title" =~ $MARKER ]] && echo "${BASH_REMATCH[1]}@${BASH_REMATCH[2]}"
}

# ---------------------------------------------------------------- staging + upload
safe_name() {
	local name="$1" stem ext
	if [[ "$name" == *.* ]]; then stem="${name%.*}"; ext="${name##*.}"; else stem="$name"; ext=""; fi
	stem="$(printf '%s' "$stem" | sed -E 's/[^A-Za-z0-9._-]+/_/g; s/^[._-]+//' | cut -c1-64)"
	ext="$(printf '%s' "$ext" | tr -cd 'A-Za-z0-9' | tr 'A-Z' 'a-z' | cut -c1-9)"
	printf '%s.%s' "${stem:-paste}" "${ext:-bin}"
}

# Stage the clipboard's files (or its image) into $STAGE; prints nothing, fills STAGED[].
STAGED=()
stage_clipboard() {
	local stamp types uri path n=0
	stamp="$(date +%Y%m%d-%H%M%S)"
	types="$(clip_types)"
	if grep -qx 'text/uri-list' <<<"$types"; then
		while IFS= read -r uri; do
			uri="${uri%$'\r'}"
			[[ "$uri" == file://* ]] || continue
			path="$(printf '%b' "$(printf '%s' "${uri#file://}" | sed 's/%\([0-9A-Fa-f][0-9A-Fa-f]\)/\\x\1/g')")"
			[ -f "$path" ] || continue
			if [ "$(stat -c %s "$path")" -gt "$MAX_BYTES" ]; then log "skipping $path (over 64 MB)"; continue; fi
			n=$((n + 1))
			cp -- "$path" "$STAGE/dw-paste-$stamp-$(printf '%04x' $((RANDOM)))-$(safe_name "$(basename "$path")")"
		done < <(clip_get text/uri-list)
	fi
	if [ "$n" = 0 ] && grep -qx 'image/png' <<<"$types"; then
		clip_get image/png >"$STAGE/dw-paste-$stamp-$(printf '%04x' $((RANDOM)))-screenshot.png"
	fi
	mapfile -t STAGED < <(find "$STAGE" -maxdepth 1 -type f -name 'dw-paste-*' | sort)
}

upload() { # <user@ip> -> prints remote paths
	local target="$1" user="${1%@*}" dir
	dir="/workspace/$user/pastes"
	scp -q -o BatchMode=yes -o ConnectTimeout=8 "${STAGED[@]}" "$target:$dir/" 2>>"$LOG" || return 1
	for f in "${STAGED[@]}"; do printf '%s/%s\n' "$dir" "$(basename "$f")"; done
}

# ---------------------------------------------------------------- main
STAGE="$(mktemp -d)"
chmod 700 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT # screenshots hold credentials: never keep the copies

if [ "${1:-}" = --self-test ]; then
	[[ "${2:-}" =~ ^[a-z_][a-z0-9_.-]*@[0-9.]+$ ]] || { echo "usage: $0 --self-test user@ip" >&2; exit 2; }
	stage_clipboard
	[ "${#STAGED[@]}" -gt 0 ] || { echo "the clipboard holds neither an image nor files" >&2; exit 1; }
	upload "$2" | sed "s|^|uploaded -> $2:|"
	exit "${PIPESTATUS[0]}"
fi

target="$(target_of_focus)"
if [ -z "$target" ]; then plain_paste; exit 0; fi
stage_clipboard
if [ "${#STAGED[@]}" = 0 ]; then plain_paste; exit 0; fi

# Keep the clipboard to put back afterwards (image or file list).
SAVED_TYPE="$(clip_types | grep -m1 -xE 'text/uri-list|image/png' || true)"
[ -n "$SAVED_TYPE" ] && clip_get "$SAVED_TYPE" >"$STAGE/.saved"

START_WIN=""
[ "$WAYLAND" = 1 ] || START_WIN="$(focused_window)"
log "upload ${#STAGED[@]} file(s) -> $target"
if ! remote="$(upload "$target")"; then notify "upload to $target failed (see $LOG)"; exit 1; fi
while IFS= read -r p; do
	# The upload took a moment: if focus moved (another window, another worker's terminal), pasting
	# now would type into the wrong place. Hand the paths over on the clipboard instead. (X11 only:
	# Wayland does not expose the focused window.)
	if [ "$WAYLAND" = 0 ] && { [ "$(focused_window)" != "$START_WIN" ] || [ "$(target_of_focus)" != "$target" ]; }; then
		paste -sd' ' <<<"$remote" >"$STAGE/.paths"
		clip_set text "$STAGE/.paths"
		notify "focus changed during the upload; nothing pasted. The worker path(s) are on your clipboard."
		exit 0
	fi
	printf '%s' "$p" >"$STAGE/.path"
	clip_set text "$STAGE/.path"
	sleep 0.05
	press_paste || exit 1
	sleep 0.25
	type_space
done <<<"$remote"
log "pasted $(tr '\n' ' ' <<<"$remote")"
sleep 0.2
[ -n "$SAVED_TYPE" ] && clip_set "$SAVED_TYPE" "$STAGE/.saved"
exit 0
