#!/usr/bin/env bash
# Test for the per-user npm shadow guard in tasks/claude_code.yml.
#
# WHY: Claude Code self-updates with the FIRST `npm` on the user's PATH, and the role puts the per-user
# npm prefix's bin/ first. An npm installed INTO that prefix therefore replaces the system npm for every
# self-update. 2026-11-01, dev-worker-3: such an npm (12.2.0, out of band) blocked the update's
# `postinstall: node install.cjs` (npm >= 12 runs no install script outside allowScripts), so the
# 500-byte placeholder stayed as bin/claude.exe, npm still exited 0, and claude failed with
# "claude native binary not installed". Nothing went red; the next `claude` was the first signal.
#
# Runs the role's REAL guard task through ansible-playbook against temp homes (`become` dropped: no
# root here) and checks: a shadowing npm/npx/lib is removed; claude, codex and other packages are
# untouched; a clean prefix is left alone; a second run changes nothing; and the guard is ordered
# BEFORE the Claude Code probe, so one role run also heals a host already left with the placeholder
# (it exits 1, the probe fails, the install re-runs with the system npm).
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

# ---- extract the guard into a one-task playbook; report its position ------------------------------
order="$(python3 - "$TASKS" "$DEFAULTS" "$T" "$GUARD" "$PROBE" <<'PY'
import os, sys
import yaml

tasks_path, defaults_path, t, guard, probe = sys.argv[1:]
tasks = yaml.safe_load(open(tasks_path)) or []
names = [task.get("name", "") for task in tasks]
guards = [i for i, n in enumerate(names) if n.startswith(guard)]
if len(guards) != 1:
    print("guard-count=%d" % len(guards))
    sys.exit(0)
probes = [i for i, n in enumerate(names) if n == probe]
print("before-probe" if probes and guards[0] < probes[0] else "after-probe")
task = dict(tasks[guards[0]])
task.pop("become", None)
task.pop("become_user", None)
npm_dir = yaml.safe_load(open(defaults_path))["dev_worker_npm_dir"]
users = [{"name": "u%d" % n, "home": os.path.join(t, "home%d" % n)} for n in (1, 2)]
play = [{
    "hosts": "localhost",
    "connection": "local",
    "gather_facts": False,
    "vars": {"dev_worker_users": users, "dev_worker_npm_dir": npm_dir},
    "tasks": [task],
}]
yaml.safe_dump(play, open(os.path.join(t, "play.yml"), "w"))
open(os.path.join(t, "npm_dir"), "w").write(npm_dir)
PY
)" || { echo "FATAL: could not read $TASKS"; exit 2; }

case "$order" in
before-probe) ok "the guard runs before '$PROBE' (one role run heals a placeholder bin)" ;;
after-probe) bad "the guard runs after '$PROBE'" "a host left with the placeholder would need two role runs" ;;
*) bad "expected exactly one task named '$GUARD...'" "$order"; echo; echo "failed=$fails"; exit 1 ;;
esac

# ---- fixtures: home1 is dev-worker-3 on 2026-11-01, home2 is a clean worker ------------------------
NPM_DIR="$(cat "$T/npm_dir")"
P1="$T/home1/$NPM_DIR"
P2="$T/home2/$NPM_DIR"
for p in "$P1" "$P2"; do
	mkdir -p "$p/bin" "$p/lib/node_modules/@anthropic-ai/claude-code/bin" "$p/lib/node_modules/@openai/codex/bin"
	echo native >"$p/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
	echo codex >"$p/lib/node_modules/@openai/codex/bin/codex.js"
	ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$p/bin/claude"
	ln -s ../lib/node_modules/@openai/codex/bin/codex.js "$p/bin/codex"
done
mkdir -p "$P1/lib/node_modules/npm/bin"
echo npm12 >"$P1/lib/node_modules/npm/bin/npm-cli.js"
echo npx12 >"$P1/lib/node_modules/npm/bin/npx-cli.js"
ln -s ../lib/node_modules/npm/bin/npm-cli.js "$P1/bin/npm"
ln -s ../lib/node_modules/npm/bin/npx-cli.js "$P1/bin/npx"

run() { ANSIBLE_NOCOLOR=1 ANSIBLE_LOCALHOST_WARNING=0 ANSIBLE_INVENTORY_UNPARSED_WARNING=0 \
	ansible-playbook -i localhost, "$T/play.yml" 2>&1; }
changed() { sed -n 's/.*localhost[[:space:]]*:.* changed=\([0-9]*\).*/\1/p' <<<"$1"; }

# ---- 1. first run removes the shadowing npm, nothing else -----------------------------------------
out="$(run)"; rc=$?
if [ "$rc" -eq 0 ]; then ok "first run succeeds"; else bad "first run failed (rc=$rc)" "$out"; fi
if [ ! -e "$P1/bin/npm" ] && [ ! -L "$P1/bin/npm" ] && [ ! -e "$P1/bin/npx" ] && [ ! -L "$P1/bin/npx" ]; then
	ok "bin/npm and bin/npx are gone"
else
	bad "bin/npm or bin/npx survived"
fi
if [ ! -e "$P1/lib/node_modules/npm" ]; then ok "lib/node_modules/npm is gone"; else bad "lib/node_modules/npm survived"; fi
for p in "$P1" "$P2"; do
	if [ "$(cat "$p/bin/claude" 2>/dev/null)" = native ] && [ "$(cat "$p/bin/codex" 2>/dev/null)" = codex ]; then
		ok "claude and codex untouched in ${p#"$T"/}"
	else
		bad "claude or codex damaged in ${p#"$T"/}"
	fi
done
if [ "$(changed "$out")" != 0 ]; then ok "first run reports a change"; else bad "first run reported changed=0" "$out"; fi

# ---- 2. idempotent ---------------------------------------------------------------------------------
out="$(run)"; rc=$?
if [ "$rc" -eq 0 ] && [ "$(changed "$out")" = 0 ]; then ok "second run changes nothing"; else bad "second run: rc=$rc changed=$(changed "$out")" "$out"; fi

echo
echo "failed=$fails"
[ "$fails" -eq 0 ]
