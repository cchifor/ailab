#!/usr/bin/env bash
# Managed by ansible (role: dev_worker). tmux-resurrect `@resurrect-hook-post-save-layout` hook.
#
# Drops the claude-dashboard session from every resurrect snapshot. resurrect passes the snapshot it
# just wrote as $1 and repoints `last` at it only AFTER this hook returns (save.sh: execute_hook
# "post-save-layout" -> files_differ -> ln -fs), so filtering here is atomic: no restore can ever see
# a dashboard entry, and no half-written snapshot is ever the one `last` points at.
#
# WHY the dashboard must not be persisted. `sessions` is rebuilt from scratch by claude-dashboard at
# every boot, so a snapshot of it buys nothing — and costs correctness, because resurrect's restore
# renames windows BY INDEX and never checks that the window sitting at that index is the one that was
# saved:
#     restore.sh: tmux rename-window -t "${session_name}:${window_number}" "$window_name"
# claude-dashboard builds the dashboard as the tmux server starts, which is precisely when continuum
# fires its restore, so both write the same session and the snapshot wins the naming.
#
# That is exactly how dev-worker-4 lost `home`. The `home` window is a plain shell, so exiting it
# closes it for good; that happened on 2026-08-02, `renumber-windows on` slid the survivors down into
# indices 1..6, and the next snapshot recorded those six. At the following boot the launcher rebuilt
# all seven windows correctly and the restore then relabelled indices 1..6 from that stale snapshot —
# shifting every name one window to the left (the `home` shell was renamed "system", htop became
# "jobs", ... while index 7 kept its own name, so "cheats" appeared twice). The mislabelled result was
# saved again 15 minutes later, which is what made it permanent: it survived three reboots and two
# weeks, and `home` was never missing at all — it was window 1 wearing the name "system".
#
# With the dashboard out of the snapshot the launcher is the only writer, so closing a window costs
# that one window until the next boot instead of silently corrupting all seven forever.
#
# Note this is a snapshot filter, NOT a persistence opt-out for the user: `main` and any ad-hoc
# sessions are still saved and restored, which is the persistence the role actually advertises (see
# docs/runbooks/dev-workers.md "start a tmux pane, reboot the VM, confirm tmux-continuum restored it").
#
# The session name MUST stay equal to claude-dashboard.sh's $SESSION — a rename there with no rename
# here would silently re-arm the bug, so tests/test-tmux-persistence.sh pins the two together.
set -euo pipefail

DASHBOARD_SESSION=sessions

snapshot="${1:-}"
# No argument, or a path resurrect did not actually write, means there is nothing to filter. Exiting 0
# is deliberate: resurrect ignores the hook's status, so the only thing a failure here could achieve is
# leaving a damaged snapshot behind.
[ -n "$snapshot" ] && [ -f "$snapshot" ] || exit 0

# `pane` and `window` lines are <type>TAB<session>TAB… — the two fields we match on both sit ahead of
# any user-controlled text (pane titles, commands), so a tab inside those cannot shift them. The
# trailing `state` line is a client pointer (active + last session), not session data, and is left
# alone: restore's `switch-client` against it is harmless once the launcher has built the dashboard.
#
# Written via a temp file + mv so an interrupted or failing run leaves the snapshot untouched rather
# than truncated. Untouched degrades to the old (buggy) behaviour; truncated would feed a restore a
# snapshot missing the user's real sessions, which is strictly worse.
tmp="$snapshot.filter.$$"
trap 'rm -f "$tmp"' EXIT
awk -F'\t' -v dash="$DASHBOARD_SESSION" \
	'!(($1 == "pane" || $1 == "window") && $2 == dash)' "$snapshot" >"$tmp"
mv -f "$tmp" "$snapshot"
