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
#   home2 = a healthy worker: native claude, no npm in the prefix;
#   home3 = dev-worker-3 on 2026-11-05: a working claude that a bare `npm install -g` put back at 2.1.197
#           (the last release whose engines accept Node 20), below the version floor.
# The synced-skill override tasks that follow the install run in the same play: home1 has no ~/.claude,
# home2 has its own settings and its own choice for one synced skill (kept), home3 has `{}`.
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
INSTALL='Install or upgrade Claude Code per user (absent, or below the version floor)'
# The synced-skill override tasks that follow the install, run in the same play (section 3 below).
SKILLS='Ensure the per-user Claude config directory (synced-skill overrides)|Read each user'"'"'s Claude settings.json (synced-skill overrides)|Turn off the unused claude.ai-synced skills per user'

# ---- extract guard + probe + install, in file order, into a playbook --------------------------------
found="$(python3 - "$TASKS" "$DEFAULTS" "$T" "$GUARD" "$PROBE" "$INSTALL" "$SKILLS" <<'PY'
import os, sys
import yaml

tasks_path, defaults_path, t, guard, probe, install, skills = sys.argv[1:]
skill_names = skills.split("|")
tasks = yaml.safe_load(open(tasks_path)) or []
picked = []
guard_become_user = ""
for task in tasks:
    name = task.get("name", "")
    if name.startswith(guard) or name in (probe, install) or name in skill_names:
        task = dict(task)
        if name.startswith(guard) and task.get("become") is True:
            guard_become_user = str(task.get("become_user", ""))
        task.pop("become", None)
        task.pop("become_user", None)
        # The temp homes belong to whoever runs the test, not to the per-user accounts u1..u3.
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if isinstance(task.get(module), dict):
                task[module] = {k: v for k, v in task[module].items() if k not in ("owner", "group")}
        picked.append(task)
core = [p for p in picked if p["name"] not in skill_names]
seq = ["guard" if p["name"].startswith(guard) else p["name"] for p in core]
found_skills = [p["name"] for p in picked if p["name"] in skill_names]
if sorted(seq) != sorted(["guard", probe, install]) or found_skills != skill_names:
    status = "missing:" + "|".join(seq + found_skills)
elif seq[0] != "guard":
    status = "order:" + "|".join(seq)
else:
    status = "ok"
print(status + ";become_user=" + guard_become_user)
defaults = yaml.safe_load(open(defaults_path))
users = [{"name": "u%d" % n, "home": os.path.join(t, "home%d" % n)} for n in (1, 2, 3)]
play = [{
    "hosts": "localhost",
    "connection": "local",
    "gather_facts": False,
    "vars": {
        "dev_worker_users": users,
        "dev_worker_npm_dir": defaults["dev_worker_npm_dir"],
        "dev_worker_claude_code_pkg": defaults["dev_worker_claude_code_pkg"],
        "dev_worker_claude_code_min_version": defaults["dev_worker_claude_code_min_version"],
        "dev_worker_claude_synced_skills_off": defaults["dev_worker_claude_synced_skills_off"],
    },
    "tasks": picked,
}]
yaml.safe_dump(play, open(os.path.join(t, "play.yml"), "w"))
open(os.path.join(t, "npm_dir"), "w").write(defaults["dev_worker_npm_dir"])
PY
)" || { echo "FATAL: could not read $TASKS / $DEFAULTS"; exit 2; }

# A wrong order is reported but the run goes on: the checks below then show what it breaks.
status="${found%%;*}"
case "$status" in
ok) ok "guard, probe and install found, guard first" ;;
order:*) bad "the guard is not ahead of the probe and the install" "task order: ${status#order:}" ;;
*) bad "expected one '$GUARD...' task plus the probe and the install" "found: ${status#missing:}"; echo; echo "failed=$fails"; exit 1 ;;
esac
# `become` is stripped for the run below (no root here), so check it statically: the guard must act
# as the prefix's owner, never as the play's root (a planted symlink must not reach the system npm).
# shellcheck disable=SC2016 # a literal Jinja expression, not a shell expansion
if [ "${found#*;become_user=}" = '{{ item.0.name }}' ]; then
	ok "the guard runs as the prefix's owner (become_user: item.0.name)"
else
	bad "the guard does not become the prefix's owner" "become_user: '${found#*;become_user=}'"
fi

# ---- fixtures (set -e: a broken fixture must fail here, not as a confusing assertion later) ------------
set -e
NPM_DIR="$(cat "$T/npm_dir")"
P1="$T/home1/$NPM_DIR"
P2="$T/home2/$NPM_DIR"
P3="$T/home3/$NPM_DIR"
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
		'[ -n "$npm_config_prefix" ] || { echo "stub npm: npm_config_prefix unset - did the install task stop exporting it?" >&2; exit 99; }' \
		'd="$npm_config_prefix/lib/node_modules/@anthropic-ai/claude-code/bin"; mkdir -p "$d" "$npm_config_prefix/bin"' \
		"$( [ "$2" = native ] && echo 'printf "%s\n" "#!/bin/sh" "echo \"2.1.316 (Claude Code)\"" >"$d/claude.exe"' \
			|| echo 'printf "%s\n" "echo \"Error: claude native binary not installed.\" >&2" "exit 1" >"$d/claude.exe"' )" \
		'chmod +x "$d/claude.exe"; ln -sfn ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$npm_config_prefix/bin/claude"' \
		>"$3"
	chmod +x "$3"
}
mkdir -p "$T/sysbin"
install_stub system native "$T/sysbin/npm"
for p in "$P1" "$P2" "$P3"; do
	mkdir -p "$p/bin" "$p/lib/node_modules/@anthropic-ai/claude-code/bin" "$p/lib/node_modules/@openai/codex/bin"
	echo codex >"$p/lib/node_modules/@openai/codex/bin/codex.js"
	ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe "$p/bin/claude"
	ln -s ../lib/node_modules/@openai/codex/bin/codex.js "$p/bin/codex"
done
placeholder "$P1/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
native "$P2/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
# home3 = dev-worker-3 on 2026-11-05: a WORKING claude that a bare `npm install -g` put back at 2.1.197.
printf '%s\n' '#!/bin/sh' 'echo "2.1.197 (Claude Code)"' >"$P3/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
chmod +x "$P3/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
# settings.json: home1 has no ~/.claude at all (a fresh worker); home2 has the user's own keys and an own
# choice for one synced skill, which must survive; home3 has an empty object.
mkdir -p "$T/home2/.claude" "$T/home3/.claude"
printf '%s\n' '{"model": "opus", "skillOverrides": {"anthropic-skills:pdf": "on", "my-skill": "off"}}' >"$T/home2/.claude/settings.json"
echo '{}' >"$T/home3/.claude/settings.json"
mkdir -p "$P1/lib/node_modules/npm/bin"
install_stub shadow placeholder "$P1/lib/node_modules/npm/bin/npm-cli.js"
echo npx12 >"$P1/lib/node_modules/npm/bin/npx-cli.js"
ln -s ../lib/node_modules/npm/bin/npm-cli.js "$P1/bin/npm"
ln -s ../lib/node_modules/npm/bin/npx-cli.js "$P1/bin/npx"
set +e

run() { PATH="$P1/bin:$T/sysbin:$PATH" ANSIBLE_NOCOLOR=1 ANSIBLE_LOCALHOST_WARNING=0 \
	ANSIBLE_INVENTORY_UNPARSED_WARNING=0 ansible-playbook -i localhost, "$T/play.yml" "$@" 2>&1; }
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
expected="$(printf '%s\n' "system $P1 install -g @anthropic-ai/claude-code@latest" "system $P3 install -g @anthropic-ai/claude-code@latest")"
if [ "$(cat "$LOG")" = "$expected" ]; then
	ok "two installs, home1's and home3's, at @latest by the system npm (home2 left alone, the shadow never used)"
else
	bad "unexpected npm calls" "$(cat "$LOG")"
fi
v="$("$P3/bin/claude" --version 2>&1)"
if [ "$v" = "2.1.316 (Claude Code)" ]; then ok "home3's stale 2.1.197 is upgraded past the floor"; else bad "home3's claude was not upgraded" "$v"; fi
if [ "$("$P2/bin/claude" --version 2>&1)" = "2.1.316 (Claude Code)" ] &&
	[ "$(cat "$P1/bin/codex")" = codex ] && [ "$(cat "$P2/bin/codex")" = codex ]; then
	ok "home2's claude and both codex installs untouched"
else
	bad "home2's claude or a codex install was damaged"
fi

# ---- 3. synced-skill overrides: added where the user has no choice, the user's own keys kept --------------
# settings_check HOME: prints "ok" or what is wrong with that home's settings.json.
settings_check() { python3 - "$1/.claude/settings.json" "$DEFAULTS" <<'PY'
import json, sys
import yaml
s = json.load(open(sys.argv[1]))
off = yaml.safe_load(open(sys.argv[2]))["dev_worker_claude_synced_skills_off"]
o = s.get("skillOverrides", {})
problems = [k for k in off if k != "anthropic-skills:pdf" and o.get(k) != "off"]
if "anthropic-skills:docs" in o:
    problems.append("docs must be left alone")
print("ok" if not problems else "not off: " + ", ".join(problems))
PY
}
if [ "$(settings_check "$T/home1")" = ok ] && [ "$(settings_check "$T/home3")" = ok ] &&
	[ "$(settings_check "$T/home2")" = ok ]; then
	ok "every synced skill in the list is off in all three homes (home1 had no ~/.claude at all)"
else
	bad "synced skills not turned off" "home1: $(settings_check "$T/home1"); home2: $(settings_check "$T/home2"); home3: $(settings_check "$T/home3")"
fi
h2="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); o=s["skillOverrides"]; print(s.get("model"), o.get("anthropic-skills:pdf"), o.get("my-skill"))' "$T/home2/.claude/settings.json")"
if [ "$h2" = "opus on off" ]; then
	ok "home2 keeps its own keys and its own choice for a synced skill"
else
	bad "home2's own settings were changed" "model, pdf, my-skill = $h2"
fi

# ---- 2. idempotent -------------------------------------------------------------------------------------
: >"$LOG"
out="$(run)"; rc=$?
if [ "$rc" -eq 0 ] && [ "$(changed "$out")" = 0 ] && [ ! -s "$LOG" ]; then
	ok "second run changes nothing and installs nothing"
else
	bad "second run: rc=$rc changed='$(changed "$out")' npm calls: $(tr '\n' ';' <"$LOG")" "$out"
fi

# ---- 4. a settings.json that exists but cannot be read stops the run and is left alone -------------------
# Only `file not found` means "no settings yet". Here settings.json is a symlink to itself (ELOOP; even root
# cannot read it): with every read failure ignored, the merge "succeeded" by replacing the link with a fresh
# file holding only the overrides. It must fail instead, and leave the link in place.
mkdir -p "$T/home4/.claude"
ln -s settings.json "$T/home4/.claude/settings.json"
out="$(run -e "{\"dev_worker_users\": [{\"name\": \"u4\", \"home\": \"$T/home4\"}]}")"; rc=$?
if [ "$rc" -ne 0 ] && grep -qi 'unable to slurp' <<<"$out" && [ -L "$T/home4/.claude/settings.json" ]; then
	ok "an unreadable settings.json fails the run and is left untouched"
else
	bad "an unreadable settings.json did not stop the run (rc=$rc, still a link: $([ -L "$T/home4/.claude/settings.json" ] && echo yes || echo no))" "$out"
fi

echo
echo "failed=$fails"
[ "$fails" -eq 0 ]
