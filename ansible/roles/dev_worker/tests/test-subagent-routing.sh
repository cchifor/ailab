#!/usr/bin/env bash
# Test for tasks/subagents.yml and files/agent-usage-report (plans/2026-11-13-agent-subagent-model-routing-plan.md).
#
# Converges the role's REAL subagents.yml through ansible-playbook into a temporary home (a throwaway role holding
# that task file, the role defaults and the files it copies; owner/group stripped, since the per-user accounts do
# not exist here), then checks:
#   1. each files/subagents/*.md: frontmatter parses; name = file name; description 1..400 chars; model and effort
#      valid; the managed marker present; scout carries only read-only tools (no Bash, Edit, Write, NotebookEdit);
#   2. a converge installs both agents, the report and both policy blocks; a user's own agent, the openbao block,
#      the operator's notes and a hand-set Codex [agents] key are untouched; no Codex role while roles are off;
#   3. the second run reports changed=0;
#   4. dropping bulk-worker removes only bulk-worker.md; [] removes both; the user's own agent survives;
#   5. the policy off removes both blocks and leaves the openbao block byte-identical;
#   6. Codex roles on with the variables "": only [agents.scout] is added, config.toml and scout.toml parse as
#      TOML; with a managed default set, turning the roles off removes that default, the role and its file;
#   7. the managed-settings template never sets CLAUDE_CODE_SUBAGENT_MODEL (plan D3);
#   8. agent-usage-report counts launches, de-duplicates a response by message.id, honours the window and reads
#      agentType from .meta.json.
#
# Linux only. Needs ansible-playbook (with community.general) and python3 >= 3.11 with PyYAML (CI: the converge
# step's venv, .gitea/workflows/dev-worker-scripts.yaml); a missing one FAILS the suite, it never skips.
# Usage: bash ansible/roles/dev_worker/tests/test-subagent-routing.sh   (exit 0 = pass)
set -uo pipefail
ROLE="$(cd "$(dirname "$0")/.." && pwd)"
command -v ansible-playbook >/dev/null 2>&1 || { echo "FATAL: ansible-playbook not found"; exit 2; }
python3 -c 'import sys, tomllib, yaml; sys.exit(sys.version_info < (3, 11))' 2>/dev/null ||
	{ echo "FATAL: python3 >= 3.11 with PyYAML is needed"; exit 2; }
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "  PASS: $1"; }
bad() { echo "  FAIL: $1"; fails=$((fails + 1)); [ -z "${2:-}" ] || printf '    %s\n' "$2"; }
H="$T/home"

# ---- 1. the agent definitions -------------------------------------------------------------------------------
out="$(python3 - "$ROLE/files/subagents" <<'PY'
import pathlib, sys, yaml
problems, names = [], []
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.md")):
    text = f.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        problems.append(f"{f.name}: no frontmatter"); continue
    head = text.split("---\n", 2)[1]
    try:
        fm = yaml.safe_load(head)
    except yaml.YAMLError as e:
        problems.append(f"{f.name}: frontmatter does not parse: {e}"); continue
    names.append(f.stem)
    if "# Managed by ansible (role: dev_worker, subagent routing)" not in head:
        problems.append(f"{f.name}: managed marker missing")
    if fm.get("name") != f.stem:
        problems.append(f"{f.name}: name {fm.get('name')!r} != file name")
    d = fm.get("description") or ""
    if not (0 < len(d) <= 400):
        problems.append(f"{f.name}: description length {len(d)}")
    if fm.get("model") not in ("haiku", "sonnet", "opus", "fable", "inherit"):
        problems.append(f"{f.name}: model {fm.get('model')!r}")
    if "effort" in fm and fm["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        problems.append(f"{f.name}: effort {fm['effort']!r}")
    if f.stem == "scout":
        tools = {t.strip() for t in str(fm.get("tools") or "").split(",") if t.strip()}
        if not tools or not tools <= {"Read", "Grep", "Glob"}:
            problems.append(f"scout.md: tools must be read-only (Read, Grep, Glob), got {sorted(tools) or 'all (omitted)'}")
if sorted(names) != ["bulk-worker", "scout"]:
    problems.append(f"agent files: {names}")
print("ok" if not problems else "; ".join(problems))
PY
)"
[ "$out" = ok ] && ok "agent definitions: frontmatter, marker, model/effort, scout read-only" || bad "agent definitions" "$out"

# ---- the throwaway role and play -----------------------------------------------------------------------------
mkdir -p "$T/roles/dev_worker/tasks" "$T/roles/dev_worker/defaults" "$T/roles/dev_worker/files" "$T/bin"
cp "$ROLE/defaults/main.yml" "$T/roles/dev_worker/defaults/main.yml"
cp -r "$ROLE/files/subagents" "$ROLE/files/agent-usage-report" "$T/roles/dev_worker/files/"
python3 - "$ROLE/tasks/subagents.yml" "$T/roles/dev_worker/tasks/main.yml" <<'PY'
import sys, yaml
tasks = yaml.safe_load(open(sys.argv[1]))
for t in tasks:
    for k, v in t.items():
        if isinstance(v, dict):
            for drop in ("owner", "group"):
                v.pop(drop, None)
yaml.safe_dump(tasks, open(sys.argv[2], "w"), sort_keys=False)
PY
cat >"$T/play.yml" <<EOF
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    dev_worker_users: [{name: tester, home: "$H"}]
    dev_worker_agent_usage_report_path: "$T/bin/agent-usage-report"
    # The role defaults keep the fleet off while the canary runs; this play is the canary's settings.
    dev_worker_claude_subagents: [scout, bulk-worker]
    dev_worker_delegation_policy_enabled: true
  roles: [dev_worker]
EOF
run() { ANSIBLE_ROLES_PATH="$T/roles" ANSIBLE_NOCOLOR=1 ANSIBLE_LOCALHOST_WARNING=0 ANSIBLE_INVENTORY_UNPARSED_WARNING=0 \
	ansible-playbook -i localhost, "$T/play.yml" "$@" 2>&1; }
changed() { sed -n 's/^localhost[[:space:]]*:.*[[:space:]]changed=\([0-9][0-9]*\)[[:space:]].*/\1/p' <<<"$1" | tail -1; }
block() { sed -n "/<!-- BEGIN ANSIBLE MANAGED - $2 -->/,/<!-- END ANSIBLE MANAGED - $2 -->/p" "$1"; }

# ---- fixtures: a worker that already has its own agent, an openbao block, notes and a Codex config ---------
mkdir -p "$H/.claude/agents" "$H/.codex"
printf '%s\n' '---' 'name: mine' 'description: my own agent' 'model: sonnet' '---' 'Mine.' >"$H/.claude/agents/mine.md"
cp "$H/.claude/agents/mine.md" "$T/mine.orig"
printf '%s\n' '# my notes' '' '<!-- BEGIN ANSIBLE MANAGED - openbao credentials -->' '## Credentials (OpenBao)' 'use cred' \
	'<!-- END ANSIBLE MANAGED - openbao credentials -->' >"$H/.claude/CLAUDE.md"
block "$H/.claude/CLAUDE.md" "openbao credentials" >"$T/openbao.orig"
printf '%s\n' '# codex notes' >"$H/.codex/AGENTS.md"
printf '%s\n' 'model = "gpt-6-astra"' '' '[features]' 'api_key_model_discovery = false' '' '[agents]' \
	'default_subagent_model = "hand-set"' >"$H/.codex/config.toml"

# ---- 2. first converge ---------------------------------------------------------------------------------------
out="$(run)"; rc=$?
[ "$rc" -eq 0 ] && ok "first converge succeeds" || bad "first converge failed (rc=$rc)" "$out"
if cmp -s "$ROLE/files/subagents/scout.md" "$H/.claude/agents/scout.md" &&
	cmp -s "$ROLE/files/subagents/bulk-worker.md" "$H/.claude/agents/bulk-worker.md"; then
	ok "scout.md and bulk-worker.md installed as shipped"
else
	bad "estate agents missing or different" "$(ls -la "$H/.claude/agents" 2>&1)"
fi
cmp -s "$T/mine.orig" "$H/.claude/agents/mine.md" && ok "the user's own agent is untouched" || bad "mine.md changed or removed"
[ -x "$T/bin/agent-usage-report" ] && ok "agent-usage-report installed, executable" || bad "agent-usage-report not installed"
n1="$(grep -c 'BEGIN ANSIBLE MANAGED - delegation policy' "$H/.claude/CLAUDE.md")"
n2="$(grep -c 'BEGIN ANSIBLE MANAGED - delegation policy' "$H/.codex/AGENTS.md")"
if [ "$n1" = 1 ] && [ "$n2" = 1 ] && grep -q '^# my notes$' "$H/.claude/CLAUDE.md" && grep -q '^# codex notes$' "$H/.codex/AGENTS.md" &&
	grep -q 'the `scout` agent' "$H/.claude/CLAUDE.md" && ! grep -q 'spawn the `scout` agent' "$H/.codex/AGENTS.md"; then
	ok "one policy block in each instruction file, notes kept, no Codex scout line while roles are off"
else
	bad "policy blocks wrong (claude=$n1 codex=$n2)" "$(cat "$H/.codex/AGENTS.md")"
fi
block "$H/.claude/CLAUDE.md" "openbao credentials" | cmp -s - "$T/openbao.orig" && ok "the openbao block is byte-identical" ||
	bad "the openbao block changed"
cfg="$(python3 -c 'import sys, tomllib; c = tomllib.load(open(sys.argv[1], "rb")); a = c.get("agents", {}); print(a.get("default_subagent_model"), "scout" in a)' "$H/.codex/config.toml" 2>&1)"
[ "$cfg" = "hand-set False" ] && ok "Codex config: hand-set [agents] key kept, no scout role while roles are off" ||
	bad "Codex config changed while roles are off" "$cfg"

# ---- 3. idempotent -------------------------------------------------------------------------------------------
out="$(run)"; rc=$?
[ "$rc" -eq 0 ] && [ "$(changed "$out")" = 0 ] && ok "second converge changes nothing" ||
	bad "second converge: rc=$rc changed='$(changed "$out")'" "$out"

# ---- 4. the agent list ---------------------------------------------------------------------------------------
out="$(run -e '{"dev_worker_claude_subagents": ["scout"]}')"; rc=$?
if [ "$rc" -eq 0 ] && [ -f "$H/.claude/agents/scout.md" ] && [ ! -e "$H/.claude/agents/bulk-worker.md" ] &&
	cmp -s "$T/mine.orig" "$H/.claude/agents/mine.md"; then
	ok "dropping bulk-worker removes only bulk-worker.md"
else
	bad "dropping bulk-worker (rc=$rc)" "$(ls -A "$H/.claude/agents") $out"
fi
out="$(run -e '{"dev_worker_claude_subagents": []}')"; rc=$?
left="$(ls -A "$H/.claude/agents" | tr '\n' ' ')"
[ "$rc" -eq 0 ] && [ "$left" = "mine.md " ] && ok "[] removes both estate agents; the user's own agent survives" ||
	bad "[] left: $left (rc=$rc)" "$out"

# ---- 5. the policy off ---------------------------------------------------------------------------------------
out="$(run -e '{"dev_worker_delegation_policy_enabled": false}')"; rc=$?
if [ "$rc" -eq 0 ] && ! grep -q 'delegation policy' "$H/.claude/CLAUDE.md" "$H/.codex/AGENTS.md" &&
	block "$H/.claude/CLAUDE.md" "openbao credentials" | cmp -s - "$T/openbao.orig"; then
	ok "policy off removes both blocks; the openbao block is byte-identical"
else
	bad "policy off (rc=$rc)" "$out"
fi

# ---- 6. Codex roles ------------------------------------------------------------------------------------------
out="$(run -e '{"dev_worker_codex_subagent_roles_enabled": true}')"; rc=$?
cfg="$(python3 - "$H/.codex/config.toml" "$H/.codex/agents/scout.toml" <<'PY'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
a = c.get("agents", {})
s = tomllib.load(open(sys.argv[2], "rb"))
keys = sorted(k for k in a if k != "scout")
print(a.get("default_subagent_model"), keys, a.get("scout", {}).get("config_file"), "model" in s, s.get("model_reasoning_effort"),
      bool(s.get("developer_instructions")))
PY
)"
if [ "$rc" -eq 0 ] && [ "$cfg" = "hand-set ['default_subagent_model'] agents/scout.toml False low True" ] &&
	grep -q 'spawn the `scout` agent' "$H/.codex/AGENTS.md"; then
	ok "roles on with \"\" variables: only [agents.scout] added; config.toml and scout.toml parse; no model written"
else
	bad "roles on (rc=$rc): $cfg" "$out"
fi
out="$(run -e '{"dev_worker_codex_subagent_roles_enabled": true, "dev_worker_codex_subagent_reasoning_effort": "low"}')"; rc=$?
out="$(run -e '{"dev_worker_codex_subagent_roles_enabled": false, "dev_worker_codex_subagent_reasoning_effort": "low"}')"; rc2=$?
cfg="$(python3 -c 'import sys, tomllib; a = tomllib.load(open(sys.argv[1], "rb")).get("agents", {}); print(sorted(a), a.get("default_subagent_model"))' "$H/.codex/config.toml" 2>&1)"
if [ "$rc" -eq 0 ] && [ "$rc2" -eq 0 ] && [ "$cfg" = "['default_subagent_model'] hand-set" ] && [ ! -e "$H/.codex/agents/scout.toml" ]; then
	ok "roles off: the managed default, the role and its file are removed; the hand-set key stays"
else
	bad "roles off after on (rc=$rc/$rc2): $cfg" "$out"
fi

# ---- 7. no fleet-wide subagent model (plan D3) ---------------------------------------------------------------
# Comment lines may name it (to say it is not set); any other line is a setting.
set_lines="$(grep -rn 'CLAUDE_CODE_SUBAGENT_MODEL' "$ROLE/templates" "$ROLE/tasks" "$ROLE/defaults" | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#')"
if [ -z "$set_lines" ]; then
	ok "no CLAUDE_CODE_SUBAGENT_MODEL setting in the managed settings, templates, tasks or defaults"
else
	bad "CLAUDE_CODE_SUBAGENT_MODEL is set somewhere in the role" "$set_lines"
fi

# ---- 8. agent-usage-report -----------------------------------------------------------------------------------
P="$T/projects/-proj"
mkdir -p "$P/s1/subagents"
python3 - "$P" <<'PY'
import json, sys
p = sys.argv[1]
def asst(ts, mid, model, content=None, inp=100, out=10, side=False):
    return json.dumps({"type": "assistant", "timestamp": ts, "isSidechain": side, "message": {
        "id": mid, "model": model, "content": content or [], "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": out}}})
call = lambda typ, **kw: [{"type": "tool_use", "name": "Agent", "input": dict(subagent_type=typ, prompt="x", **kw)}]
main = [
    asst("2026-11-10T10:00:00Z", "m1", "claude-opus-5-5", call("scout")),
    asst("2026-11-10T10:00:00Z", "m1", "claude-opus-5-5", call("scout")),          # the same response, a 2nd line
    asst("2026-11-10T10:05:00Z", "m2", "claude-opus-5-5", call("general-purpose", model="opus")),
    asst("2026-10-01T10:00:00Z", "m0", "claude-opus-5-5", call("Explore")),          # outside the window
]
open(f"{p}/s1.jsonl", "w").write("\n".join(main) + "\n")
open(f"{p}/s1/subagents/agent-a1.jsonl", "w").write(asst("2026-11-10T10:01:00Z", "x1", "claude-haiku-5-5", inp=300, out=30, side=True) + "\n")
json.dump({"agentType": "scout"}, open(f"{p}/s1/subagents/agent-a1.meta.json", "w"))
PY
got="$(python3 "$ROLE/files/agent-usage-report" --root "$T/projects" --days 14 --now 2026-11-13T00:00:00Z --json 2>&1 | python3 -c '
import json, sys
s = json.load(sys.stdin)
launch = {(l["subagent_type"], l["model"]): l["count"] for l in s["launches"]}
print(launch.get(("scout", "-")), launch.get(("general-purpose", "opus")), ("Explore", "-") in launch,
      s["ran_by_agent_type"], s["tokens"]["main"]["claude-opus-5-5"]["input_tokens"],
      s["tokens"]["subagent"]["claude-haiku-5-5"]["input_tokens"], s["subagent_share_all_tokens"], s["main_sessions"])' 2>&1)"
expected="1 1 False {'scout': 1} 200 300 0.6 1"
[ "$got" = "$expected" ] && ok "agent-usage-report: launches, de-duplication, window, agentType, share" ||
	bad "agent-usage-report" "got:      $got"$'\n'"    expected: $expected"

echo
echo "failed=$fails"
[ "$fails" -eq 0 ]
