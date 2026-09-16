#!/bin/bash
# Bootstrap for the daily fleet converge. THIS is the file Windows Task Scheduler runs
# (task "ailab-fleet-converge" -> wsl.exe -e bash -lc "~/.ailab-converge/fleet-converge-daily.sh");
# it is installed to that path, and it is deliberately the only piece of the converge that does
# NOT self-update. Keep it tiny and boring: every line of real logic belongs in
# scripts/fleet-converge-daily.sh, which this fetches fresh on every run.
#
# WHY A BOOTSTRAP AND NOT JUST SCHEDULING THE REPO'S SCRIPT DIRECTLY: the converge script lives
# inside the clone it updates, and it updates that clone with `git reset --hard`. bash reads a
# script incrementally, by byte offset, as it executes — so a script that rewrites its own file
# mid-run can resume at the wrong offset and execute garbage. Hence: update the clone, copy the
# current converge script OUT to a stable path, and exec that copy. Git never touches the file
# bash is reading.
#
# WHAT THIS FIXES (2026-09-16). From 2026-09-03 to 2026-09-16 the scheduled path held a FROZEN
# COPY of the converge script rather than a bootstrap. It predated the reviewers step added on
# 09-06, so `ansible-playbook reviewers.yml` never ran: the reviewer VMs converged NOWHERE for
# 13 days while both the repo and the runbook said they converged daily. Every reviewbot change
# merged in that window sat undeployed until somebody ran the playbook by hand.
#
# It was invisible because the same frozen copy also predated two guards that were added in the
# same commit as the reviewers step:
#   * no `set -o pipefail`, so `ansible-playbook … | tail` reported the exit status of `tail`
#     and a failed converge looked identical to a clean one;
#   * no `exit "$rc"`, so Task Scheduler's LastTaskResult was 0 no matter what happened.
# Three fixes shipped to main, none of them ever ran. Proof at the time: zero occurrences of
# `reviewer-1`/`reviewer-2` anywhere in ~/.ailab-converge/converge.log, against two dev-worker
# PLAY RECAPs per run.
#
# The failure mode this closes is therefore not "the script was wrong" — it was right, in git,
# for ten days — but "the scheduled copy was not the script". A bootstrap cannot drift that way,
# because it has no logic to drift.
set -euo pipefail

BASE="$HOME/.ailab-converge"
REPO="$BASE/repo"
SRC="$REPO/scripts/fleet-converge-daily.sh"
RUN="$BASE/.run-converge.sh"

# FAIL CLOSED, like every other gate in this repo: a missing clone or a missing script must be a
# non-zero exit that Task Scheduler shows, never a silent skip that looks like a clean converge.
[ -d "$REPO/.git" ] || { echo "bootstrap: $REPO is not a git clone" >&2; exit 1; }

git -C "$REPO" fetch --quiet origin main
git -C "$REPO" reset --hard --quiet origin/main

[ -f "$SRC" ] || { echo "bootstrap: $SRC missing on origin/main" >&2; exit 1; }

# `install` writes a NEW inode rather than truncating in place, so this is safe even against a
# copy that some other run is still executing.
install -m 0755 "$SRC" "$RUN"

# exec, so the converge script's exit status IS this script's exit status and reaches Task
# Scheduler unaltered — the signal whose absence hid the 13-day outage above.
exec "$RUN"
