#!/usr/bin/env bash
# Test for the per-user npm shadow guard in tasks/claude_code.yml, together with the Claude Code
# probe + install it sits in front of.
#
# WHY: Claude Code self-updates with the FIRST `npm` on the user's PATH, and the role puts the per-user
# npm prefix's bin/ first. An npm installed INTO that prefix therefore replaces the system npm for every
# self-update. 2026-11-01, dev-worker-3: such an npm (12.2.0, out of band) blocked the update's
# `postinstall: node install.cjs` (npm >= 12 runs no install script outside allowScripts), so the
# 500-byte placeholder stayed as bin/claude.exe, npm still exited 0, and claude failed with
# "claude native binary not installed". Nothing went red; the next `claude` was the first signal.
#
# Runs the role's REAL guard, probe and install tasks (in file order) through ansible-playbook against
# two temp homes, `become` dropped (no root here):
#   home1 = dev-worker-3 that day: a shadowing npm in the prefix and the placeholder as claude.exe;
#   home2 = a healthy worker: native claude, no npm in the prefix.
# `npm` is a stub on PATH (the "system npm": installs a native claude into $npm_config_prefix), and
# the shadowing npm is a stub that installs the placeholder, as npm 12 effectively did. home1's bin/
# is put FIRST on the PATH the tasks run with: the worst case, where the role's own install resolves
# npm through the user's PATH (in production ansible's PATH lacks the prefix). There the guard's
# position is load-bearing: run after the install, the shadow would reinstall the placeholder.
# Checks: one run heals home1 with the system npm and removes the shadow; home2 is not reinstalled;
# claude/codex otherwise untouched; the second run reports changed=0 and installs nothing.
#
# Linux only. Needs ansible-playbook and python3 with PyYAML (CI: the converge step's venv,
# .gitea/workflows/dev-worker-scripts.yaml); a missing one FAILS the suite, it never skips.
#
# Usage: bash ansible/roles/dev_worker/tests/test-npm-prefix-guard.sh   (exit 0 = pass)
set -uo pipefail
ROLE="$(cd "$(dirname "$0")/.." && pwd)"
TASKS="$ROLE/tasks/claude_code.yml"
DEFAULTS="$ROLE/defaults/main.yml"
command -v ansible-playbook >/dev/null 2>&1 || { echo "FATAL: ansible-playbook not found"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 not found"; exit 2; }
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "  PASS: $1"; }
bad() { echo "  FAIL: $1"; fails=$((fails + 1)); [ -z "${2:-}" ] || printf '    %s\n' "$2"; }
GUARD='Remove a per-user npm that would shadow the system npm'
PROBE='Probe installed Claude Code per user'
INSTALL='Install Claude Code per user (only if absent)'

# ---- extract guard + probe + install, in file order, into a playbook --------------------------------
found="$(python3 - "$TASKS" "$DEFAULTS" "$T" "$GUARD" "$PROBE" "$INSTALL" <<'PY'
import os, sys
import yaml

tasks_path, defaults_path, t, guard, probe, install = sys.argv[1:]
tasks = yaml.safe_load(open(tasks_path)) or []
picked = []
for task in tasks:
    name = task.get("name", "")
    if name.startswith(guard) or name in (probe, install):
        task = dict(task)
        task.pop("become", None)
        task.pop("become_user", None)
        picked.append(task)
seq = ["guard" if p["name"].startswith(guard) else p["name"] for p in picked]
if sorted(seq) != sorted(["guard", probe, install]):
    print("missing:" + "|".join(seq))
elif seq[0] != "guard":
    print("order:" + "|".join(seq))
else:
    print("ok")
defaults = yaml.safe_load(open(defaults_path))
users = [{"name": "u%d" % n, "home": os.path.join(t, "home%d" % n)} for n in (1, 2)]
play = [{
    "hosts": "localhost",
    "connection": "local",
    "gather_facts": False,
    "vars": {
        "dev_worker_users": users,
        "dev_worker_npm_dir": defaults["dev_worker_npm_dir"],
        "dev_worker_claude_code_pkg": defaults["dev_worker_claude_code_pkg"],
    },
    "tasks": picked,
}]
yaml.safe_dump(play, open(os.path.join(t, "play.yml"), "w"))
open(os.path.join(t, "npm_dir"), "w").write(defaults["dev_worker_npm_dir"])
PY
)" || { echo "FATAL: could not read $TASKS / $DEFAULTS"; exit 2; }

# A wrong order is reported but the run goes on: the checks below then show what it breaks.
case "$found" in
ok) ok "guard, probe and install found, guard first" ;;
order:*) bad "the guard is not ahead of the probe and the install" "task order: ${found#order:}" ;;
*) bad "expected one '$GUARD...' task plus the probe and the install" "found: ${found#missing:}"; echo; echo "failed=$fails"; exit 1 ;;
esac

# ---- fixtures ----------------------------------------------------------------------------------------
NPM_DIR="$(cat "$T/npm_dir")"
P1="$T/home1/$NPM_DIR"
P2="$T/home2/$NPM_DIR"
LOG="$T/npm.log"
: >"$LOG"
# The real 2.1.316 placeholder starts like this and has NO shebang (an exec-format error under execve).
placeholder() { printf '%s\n' 'echo "Error: claude native binary not installed." >&2' 'exit 1' >"$1"; chmod +x "$1"; }
native() { printf '%s\n' '#!/bin/sh' 'echo "2.1.316 (Claude Code)"' >"$1"; chmod +x "$1"; }
# install_stub NAME KIND: an npm whose `install -g` lays down claude-code with a KIND claude.exe.
install_stub() {
	printf '%s\n' '#!/bin/sh' \
		"echo \"$1 \$npm_config_prefix \$*\" >>'$LOG'" \
		'[ "$1" = install ] || exit 0' \
		'd="$npm_config_prefix/lib/node_modules/@anthropic-ai/claude-code/bin"; mkdir -p "$d" "$npm_config_prefix/bin"' \
		"$( [ "$2" = native ] && echo 'printf "%s\n" "#!/bin/sh" "echo \"2.1.316 (Claude Code)\"" >"$d/claude.exe"' \
			|| echo 'printf "%s\n" "echo \"Error: claude native binary not installed.\" >&2" "exit 1" >"$d/claude.exe"' )" \
		'chmod +x "$d/claude.exe"; ln -sfn ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$npm_config_prefix/bin/claude"' \
		>"$3"
	chmod +x "$3"
}
mkdir -p "$T/sysbin"
install_stub system native "$T/sysbin/npm"
for p in "$P1" "$P2"; do
	mkdir -p "$p/bin" "$p/lib/node_modules/@anthropic-ai/claude-code/bin" "$p/lib/node_modules/@openai/codex/bin"
	echo codex >"$p/lib/node_modules/@openai/codex/bin/codex.js"
	ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$p/bin/claude"
	ln -s ../lib/node_modules/@openai/codex/bin/codex.js "$p/bin/codex"
done
placeholder "$P1/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
native "$P2/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
mkdir -p "$P1/lib/node_modules/npm/bin"
install_stub shadow placeholder "$P1/lib/node_modules/npm/bin/npm-cli.js"
echo npx12 >"$P1/lib/node_modules/npm/bin/npx-cli.js"
ln -s ../lib/node_modules/npm/bin/npm-cli.js "$P1/bin/npm"
ln -s ../lib/node_modules/npm/bin/npx-cli.js "$P1/bin/npx"

run() { PATH="$P1/bin:$T/sysbin:$PATH" ANSIBLE_NOCOLOR=1 ANSIBLE_LOCALHOST_WARNING=0 \
	ANSIBLE_INVENTORY_UNPARSED_WARNING=0 ansible-playbook -i localhost, "$T/play.yml" 2>&1; }
# The recap's changed= count for localhost; empty when there is no recap line (the callers reject that).
changed() { sed -n 's/^localhost[[:space:]]*:.*[[:space:]]changed=\([0-9][0-9]*\)[[:space:]].*/\1/p' <<<"$1" | tail -1; }

# ---- 1. one run heals home1 with the system npm and removes the shadow --------------------------------
out="$(run)"; rc=$?
if [ "$rc" -eq 0 ]; then ok "first run succeeds"; else bad "first run failed (rc=$rc)" "$out"; fi
n="$(changed "$out")"
if [[ "$n" =~ ^[1-9][0-9]*$ ]]; then ok "first run reports changes (changed=$n)"; else bad "first run: no changed>0 in the recap (got '${n}')" "$out"; fi
if [ ! -e "$P1/bin/npm" ] && [ ! -L "$P1/bin/npm" ] && [ ! -e "$P1/bin/npx" ] && [ ! -L "$P1/bin/npx" ] &&
	[ ! -e "$P1/lib/node_modules/npm" ]; then
	ok "the shadowing npm (bin/npm, bin/npx, lib/node_modules/npm) is gone"
else
	bad "part of the shadowing npm survived"
fi
v="$("$P1/bin/claude" --version 2>&1)"
if [ "$v" = "2.1.316 (Claude Code)" ]; then ok "home1's claude is healed in one run"; else bad "home1's claude still broken" "$v"; fi
if [ "$(cat "$LOG")" = "system $P1 install -g @anthropic-ai/claude-code" ]; then
	ok "exactly one install, home1's, by the system npm (home2 left alone, the shadow never used)"
else
	bad "unexpected npm calls" "$(cat "$LOG")"
fi
if [ "$("$P2/bin/claude" --version 2>&1)" = "2.1.316 (Claude Code)" ] &&
	[ "$(cat "$P1/bin/codex")" = codex ] && [ "$(cat "$P2/bin/codex")" = codex ]; then
	ok "home2's claude and both codex installs untouched"
else
	bad "home2's claude or a codex install was damaged"
fi

# ---- 2. idempotent -------------------------------------------------------------------------------------
: >"$LOG"
out="$(run)"; rc=$?
if [ "$rc" -eq 0 ] && [ "$(changed "$out")" = 0 ] && [ ! -s "$LOG" ]; then
	ok "second run changes nothing and installs nothing"
else
	bad "second run: rc=$rc changed='$(changed "$out")' npm calls: $(tr '\n' ';' <"$LOG")" "$out"
fi

echo
echo "failed=$fails"
[ "$fails" -eq 0 ]
