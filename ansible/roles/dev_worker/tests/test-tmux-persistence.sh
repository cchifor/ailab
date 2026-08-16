#!/usr/bin/env bash
# Self-contained behavioural test for the dev_worker tmux-persistence wiring (plain bash — no bats,
# no molecule, following ansible/roles/gitea_runner/tests/test-cleanup.sh).
#
# Pins the fix for the bug that ate dev-worker-4's `home` window. tmux-resurrect restores window names
# BY INDEX (restore.sh: `rename-window -t "$session:$window_number" "$window_name"`), and the
# claude-dashboard session is rebuilt from scratch at every boot, so the two write the same session and
# a snapshot whose window list has drifted relabels the launcher's freshly-built windows. On
# dev-worker-4 that slid all seven names one window to the left and stayed that way for two weeks,
# because the mislabelled result was what got saved 15 minutes later.
#
# Section [A] drives a REAL private tmux server and replays resurrect's own rename loop, so it asserts
# the actual collision rather than a paraphrase of it. It needs tmux(1): absent, it is reported as
# NOT RUN and the suite still checks everything else — but CI sets REQUIRE_TMUX=1, which makes a
# missing tmux a hard failure. That is the "no soft skips" rule of .gitea/workflows applied where it
# belongs: a developer on Windows may run a partial suite and be told so loudly; CI may not.
#
# Usage: bash ansible/roles/dev_worker/tests/test-tmux-persistence.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="$HERE/.."
FILTER="$ROLE/files/tmux-resurrect-filter.sh"
LAUNCHER="$ROLE/files/claude-dashboard.sh"
CONF_TMPL="$ROLE/templates/tmux.conf.j2"
TASKS="$ROLE/tasks/tmux.yml"
for f in "$FILTER" "$LAUNCHER" "$CONF_TMPL" "$TASKS"; do
	[ -r "$f" ] || { echo "FATAL: cannot read $f"; exit 2; }
done

WORK="$(mktemp -d)"
# Private sockets, never the user's default server — this suite is expected to run on a live
# dev-worker, where killing the wrong tmux server would destroy exactly what it is protecting.
SOCK=""
SOCKS=()
sock_seq=0
cleanup() {
	if command -v tmux >/dev/null 2>&1; then
		for s in ${SOCKS[@]+"${SOCKS[@]}"}; do tmux -L "$s" kill-server 2>/dev/null; done
	fi
	rm -rf "$WORK"
}
trap cleanup EXIT

fails=0
ok() { echo "  PASS: $1"; }
bad() {
	echo "  FAIL: $1"
	fails=$((fails + 1))
}
assert_eq() { # <got> <want> <label>
	if [ "$1" = "$2" ]; then ok "$3"; else
		bad "$3"
		echo "    got:  '$1'"
		echo "    want: '$2'"
	fi
}

# ---- the launcher's window list, DERIVED not restated ---------------------------------------------
# Hard-coding the seven names here would let claude-dashboard.sh and this suite drift apart, and a
# suite that pins names nothing ships is exactly the failure mode gitea_runner's section [M] exists to
# catch. Parse them out of the launcher in creation order instead.
WINDOWS=()
while IFS= read -r name; do WINDOWS+=("$name"); done < <(
	grep -E '^[[:space:]]*tmux[[:space:]]+(new-session|new-window)' "$LAUNCHER" |
		grep -oE '[[:space:]]-n[[:space:]]+[A-Za-z0-9_-]+' | awk '{print $NF}'
)

echo "[0] the launcher's window list parses (floor checks — a mapping that matches nothing must not pass quietly)"
if [ "${#WINDOWS[@]}" -ge 5 ]; then
	ok "0: parsed ${#WINDOWS[@]} windows from claude-dashboard.sh (${WINDOWS[*]})"
else
	bad "0: parsed only ${#WINDOWS[@]} window name(s) from claude-dashboard.sh — the -n mapping stopped matching"
	echo
	echo "$fails CHECK(S) FAILED"
	exit 1
fi
# `home` first is not decoration: it is the window the bug removed, and it is the only one the launcher
# leaves as a plain shell, so it is the one a user can close and thereby drift the snapshot.
assert_eq "${WINDOWS[0]}" "home" "0: the first window the launcher creates is 'home'"

CORRECT="${WINDOWS[*]} "
# The stale snapshot dev-worker-4 actually carried: `home` closed, `renumber-windows on` slid the rest
# down into indices 1..N-1. Replaying resurrect's rename over a correctly-built session therefore drags
# every name one window left, and the last index — absent from the snapshot — keeps its own name, so
# the final name appears twice. That is precisely what `tmux list-windows` showed on dev-worker-4.
STALE=("${WINDOWS[@]:1}")
CORRUPT="${STALE[*]} ${WINDOWS[*]: -1} "

# ---- snapshot fixtures ----------------------------------------------------------------------------
# Real resurrect format: <type>TAB<session>TAB… Window fields are
# type, session, index, :name, active, :flags, layout, automatic_rename.
snapshot_stale="$WORK/stale.txt"
{
	printf 'pane\tmain\t1\t1\t:*\t1\tdev-worker-4\t:/home/c4\t1\tbash\t:\n'
	printf 'window\tmain\t1\t:mywork\t1\t:*\tb185,80x24,0,0,99\t:\n'
	idx=1
	for w in "${STALE[@]}"; do
		printf 'pane\tsessions\t%s\t0\t:\t1\tdev-worker-4\t:/workspace/c4\t1\tbash\t:\n' "$idx"
		printf 'window\tsessions\t%s\t:%s\t0\t:\tb17d,80x24,0,0,%s\toff\n' "$idx" "$w" "$idx"
		idx=$((idx + 1))
	done
	printf 'state\tsessions\tmain\n'
} >"$snapshot_stale"

# ---- [A] the collision itself, against a real tmux server -----------------------------------------
build_dashboard() { # rebuild the launcher's session from scratch, plus a user session to protect
	# A FRESH socket each time. Reusing one and calling kill-server first read simpler but raced: the
	# following new-session reaches the still-dying server and dies with it, which left A2 asserting
	# against an empty window list and blaming the filter for a harness fault.
	sock_seq=$((sock_seq + 1))
	SOCK="devworker-test-$$-$sock_seq"
	SOCKS+=("$SOCK")
	tmux -L "$SOCK" -f "$WORK/tmux.conf" new-session -d -s sessions -n "${WINDOWS[0]}" "sleep 600"
	local w
	for w in "${WINDOWS[@]:1}"; do
		tmux -L "$SOCK" new-window -t sessions: -n "$w" "sleep 600"
	done
	tmux -L "$SOCK" new-session -d -s main -n scratch "sleep 600"
}
# resurrect's restore_window_properties(), field-for-field. `${name#:}` is its remove_first_char.
# stderr is captured rather than discarded: every window this replays over is supposed to exist, so
# any tmux complaint means the harness misfired, and discarding it is what disguised that as a product
# failure the first time round.
replay_resurrect_renames() { # <snapshot>
	: >"$WORK/replay.err"
	grep '^window' "$1" | while IFS=$'\t' read -r _type sess idx name _active _flags _layout _auto; do
		tmux -L "$SOCK" rename-window -t "$sess:$idx" "${name#:}" 2>>"$WORK/replay.err"
	done
}
assert_replay_clean() { # <label>
	if [ -s "$WORK/replay.err" ]; then
		bad "$1 — the replay itself errored (harness fault, not a product result)"
		sed 's/^/    /' "$WORK/replay.err"
	else
		ok "$1"
	fi
}
win_list() { tmux -L "$SOCK" list-windows -t "$1" -F '#{window_name}' 2>/dev/null | tr '\n' ' '; }

echo "[A] resurrect's rename-by-index vs. the code-generated dashboard (real tmux)"
if ! command -v tmux >/dev/null 2>&1; then
	if [ "${REQUIRE_TMUX:-0}" = 1 ]; then
		bad "A: tmux(1) not found and REQUIRE_TMUX=1 — the section that pins the actual bug did not run"
	else
		echo "  NOT RUN: tmux(1) not found. Sections [B]/[C] still run; CI sets REQUIRE_TMUX=1 to require this."
	fi
else
	# base-index/renumber-windows mirror tmux.conf.j2 (pinned in [C]) — the whole index arithmetic
	# depends on them. automatic-rename is off so nothing but the replay can touch a window name.
	printf 'set -g base-index 1\nset -g renumber-windows on\nsetw -g automatic-rename off\n' >"$WORK/tmux.conf"

	# A1 is the NON-VACUITY check: with the dashboard still in the snapshot the corruption must
	# reproduce. If this ever goes green-by-passing, A2 below proves nothing.
	build_dashboard
	assert_eq "$(win_list sessions)" "$CORRECT" "A1a: the launcher builds the correct window list"
	replay_resurrect_renames "$snapshot_stale"
	assert_replay_clean "A1b: every rename in the replay landed on a window that exists"
	assert_eq "$(win_list sessions)" "$CORRUPT" "A1c: an unfiltered stale snapshot corrupts it (the dev-worker-4 failure, reproduced)"

	# A2: the fix. Same snapshot, same replay — but filtered first, so the restore has no dashboard
	# window to rename and the launcher stays the only writer.
	cp "$snapshot_stale" "$WORK/filtered.txt"
	bash "$FILTER" "$WORK/filtered.txt"
	build_dashboard
	replay_resurrect_renames "$WORK/filtered.txt"
	assert_replay_clean "A2a: the filtered replay ran without error"
	assert_eq "$(win_list sessions)" "$CORRECT" "A2b: filtered snapshot leaves the dashboard intact"

	# A3: the fix must not be "turn persistence off". The user's own sessions are the entire point of
	# shipping resurrect+continuum (docs/runbooks/dev-workers.md), so their windows must STILL restore.
	assert_eq "$(win_list main)" "mywork " "A3: a real user session is still restored from the filtered snapshot"
fi

# ---- [B] the filter's transformation ---------------------------------------------------------------
echo "[B] snapshot filtering"
cp "$snapshot_stale" "$WORK/b.txt"
bash "$FILTER" "$WORK/b.txt"
assert_eq "$(grep -c '	sessions	' "$WORK/b.txt")" "1" "B1: only the non-session-data line mentioning the dashboard survives"
assert_eq "$(grep -c '^state	sessions	main$' "$WORK/b.txt")" "1" "B2: the state line (a client pointer, not session data) is kept"
assert_eq "$(grep -cE '^(pane|window)	sessions	' "$WORK/b.txt")" "0" "B3: every dashboard pane/window line is gone"

# Byte-exact preservation of everything else. A filter that rewrote tabs or dropped a trailing field
# would corrupt layouts on restore in a way no window-name assertion above would notice.
grep -E '^(pane|window)	main	' "$snapshot_stale" >"$WORK/want-main.txt"
grep -E '^(pane|window)	main	' "$WORK/b.txt" >"$WORK/got-main.txt"
if cmp -s "$WORK/want-main.txt" "$WORK/got-main.txt"; then
	ok "B4: other sessions' lines are preserved byte-for-byte"
else
	bad "B4: other sessions' lines were altered"
	diff "$WORK/want-main.txt" "$WORK/got-main.txt" | sed 's/^/    /'
fi

# Idempotent, and a no-op on a snapshot that never had a dashboard.
cp "$WORK/b.txt" "$WORK/b2.txt"
bash "$FILTER" "$WORK/b2.txt"
if cmp -s "$WORK/b.txt" "$WORK/b2.txt"; then ok "B5: re-filtering is a no-op (idempotent)"; else bad "B5: re-filtering changed the snapshot"; fi

# Argument handling. These are the paths where a careless `>` would truncate a good snapshot — the one
# outcome strictly worse than not filtering at all, since `last` would then point at an empty file.
cp "$snapshot_stale" "$WORK/untouched.txt"
before="$(cat "$WORK/untouched.txt")"
bash "$FILTER" >/dev/null 2>&1
rc_noarg=$?
bash "$FILTER" "$WORK/does-not-exist.txt" >/dev/null 2>&1
rc_missing=$?
assert_eq "$rc_noarg" "0" "B6: no argument exits 0 (resurrect ignores hook status; never fail loudly here)"
assert_eq "$rc_missing" "0" "B7: a nonexistent snapshot exits 0"
assert_eq "$(cat "$WORK/untouched.txt")" "$before" "B8: ...and neither path touches an unrelated snapshot"
if [ ! -e "$WORK/does-not-exist.txt" ]; then ok "B9: a missing snapshot is not created"; else bad "B9: the filter created a snapshot that resurrect never wrote"; fi

# ---- [C] wiring + drift ----------------------------------------------------------------------------
# The filter is only ever reached through three couplings, each of which fails SILENTLY when broken:
# the session name it matches, the hook name resurrect calls, and the path the hook names.
echo "[C] wiring (each of these breaks silently — nothing goes red at runtime)"
filter_session="$(sed -n 's/^DASHBOARD_SESSION=\(.*\)$/\1/p' "$FILTER" | head -1)"
launcher_session="$(sed -n 's/^SESSION=\(.*\)$/\1/p' "$LAUNCHER" | head -1)"
assert_eq "$filter_session" "$launcher_session" "C1: the filter matches the session claude-dashboard.sh actually creates"

conf_hook="$(sed -n "s/.*set -g @resurrect-hook-post-save-layout[[:space:]]*'\([^']*\)'.*/\1/p" "$CONF_TMPL" | head -1)"
if [ -n "$conf_hook" ]; then
	ok "C2: tmux.conf.j2 registers @resurrect-hook-post-save-layout ($conf_hook)"
else
	bad "C2: tmux.conf.j2 does not register @resurrect-hook-post-save-layout — nothing would ever run the filter"
fi

# post-save-layout, NOT post-save-all: only the former is handed the snapshot path, and only it runs
# before resurrect repoints `last` at that file (save.sh). post-save-all would filter a snapshot a
# restore could already have read.
if grep -q '@resurrect-hook-post-save-all' "$CONF_TMPL"; then
	bad "C3: the hook must be post-save-layout (gets the snapshot path, runs before 'last' is repointed), not post-save-all"
else
	ok "C3: the pre-'last' hook is the one used"
fi

task_dest="$(grep -A6 'src: tmux-resurrect-filter.sh' "$TASKS" | sed -n 's/^[[:space:]]*dest:[[:space:]]*\(.*\)$/\1/p' | head -1)"
# Non-emptiness is asserted separately and FIRST. Comparing the two extractions alone passes when BOTH
# are empty — i.e. when the hook is unregistered and the file uninstalled, the total-failure case.
if [ -z "$task_dest" ]; then
	bad "C4: tmux.yml has no 'src: tmux-resurrect-filter.sh' task with a dest — the filter is never installed"
else
	assert_eq "$task_dest" "$conf_hook" "C4: tmux.yml installs the filter at the path tmux.conf.j2 invokes"
fi

task_mode="$(grep -A6 'src: tmux-resurrect-filter.sh' "$TASKS" | sed -n 's/^[[:space:]]*mode:[[:space:]]*"\(.*\)"$/\1/p' | head -1)"
case "$task_mode" in
*[1357]) ok "C5: the filter is installed executable ($task_mode)" ;;
*) bad "C5: the filter is installed mode '$task_mode' — the hook is exec'd, so it must be executable" ;;
esac

# [A]'s index arithmetic — and the real bug — both assume the shipped base-index. At base-index 0 the
# stale snapshot's indices would land on different windows entirely.
if grep -qE '^set -g base-index 1$' "$CONF_TMPL"; then
	ok "C6: tmux.conf.j2 still ships base-index 1 (what [A] and the snapshot indices assume)"
else
	bad "C6: tmux.conf.j2 no longer ships 'base-index 1' — the snapshot index mapping in [A] is stale"
fi

echo
if [ "$fails" -eq 0 ]; then
	echo "ALL PASS"
	exit 0
else
	echo "$fails CHECK(S) FAILED"
	exit 1
fi
