#!/bin/bash
# kit/claude-direct.sh KIT RUN SPEC [tap] - the Claude seat's direct arm, on the worker: Claude Code (the router's
# validated version, $KIT/cc) in docker, the host's own Claude login (the ACCESS token as an env var only: the refresh
# token never leaves the host), flags mirroring the router's sdk-resume launch (no built-in tools, empty config dir, no
# settings or hooks, --system-prompt, ENABLE_TOOL_SEARCH=false).
# SPEC (JSON): { model, system, turns: [..], mode: "chat"|"agentic", tools: [...] (chat: inert stub), fixture_dir, max_turns }
#   chat:    one -p call per turn; a multi-turn conversation continues its own session (--resume)
#   agentic: one -p call; Claude Code runs its own loop over the executing workspace tools (kit/mcp-tools.mjs)
# With a 4th argument, Anthropic-bound requests go through the recording tap (Layer a).
set -uo pipefail
kit=$1; run=$2; spec=$3; R=$kit/runs/$run
rm -rf "$R"; mkdir -p "$R/home" "$R/ws"; chmod 700 "$R" "$R/home"
j() { python3 -c 'import json,sys;v=json.load(open(sys.argv[1])).get(sys.argv[2]);print(json.dumps(v) if isinstance(v,(list,dict)) else (v if v is not None else ""))' "$spec" "$1"; }
model=$(j model); system=$(j system); mode=$(j mode); fixture=$(j fixture_dir); maxt=$(j max_turns)
[ -n "$fixture" ] && { cp -r "$fixture/." "$R/ws/"; git -C "$R/ws" init -q -b main && git -C "$R/ws" add -A && git -C "$R/ws" -c user.name=b -c user.email=b@l commit -q -m base; }
export CLAUDE_CODE_OAUTH_TOKEN=$(python3 -c 'import json;print(json.load(open("/home/c4/.claude/.credentials.json"))["claudeAiOauth"]["accessToken"])')
tapenv=(); [ -n "${4:-}" ] && tapenv=(-e "ANTHROPIC_BASE_URL=http://127.0.0.1:${TAP_PORT}/t/anthropic/$run")
if [ "$mode" = agentic ]; then
  mcp='{"mcpServers":{"tools":{"type":"stdio","command":"node","args":["/kit/mcp-tools.mjs","/ws","/out/tools.jsonl"]}}}'
  allowed='mcp__tools__*'; turns=1
else
  python3 -c 'import json,sys;json.dump({"tools":json.load(open(sys.argv[1])).get("tools") or []},open(sys.argv[2],"w"))' "$spec" "$R/tools.json"
  if [ "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["tools"]))' "$R/tools.json")" -gt 0 ]; then
    mcp='{"mcpServers":{"tools":{"type":"stdio","command":"node","args":["/kit/mcp-stub.mjs","/out/tools.json"]}}}'
  else mcp='{"mcpServers":{}}'; fi
  allowed='mcp__tools__*'; turns=$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["turns"]))' "$spec")
fi
start=$(date +%s.%N)
for t in $(seq 1 "$turns"); do
  prompt=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["turns"][int(sys.argv[2])-1])' "$spec" "$t")
  persist=--no-session-persistence; resume=()
  [ "$turns" -gt 1 ] && persist=--verbose
  [ "$t" -gt 1 ] && resume=(--resume "$(cat "$R/session_id")")
  t0=$(date +%s.%N)
  timeout 900 docker run --rm --network host --memory 1g -e CLAUDE_CODE_OAUTH_TOKEN -e HOME=/h -e CLAUDE_CONFIG_DIR=/h -e CI=true \
    -e DISABLE_AUTOUPDATER=1 -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 -e ENABLE_TOOL_SEARCH=false "${tapenv[@]}" \
    -v "$kit:/kit:ro" -v "$CC_DIR:/cc:ro" -v "$R/home:/h" -v "$R/ws:/ws" -v "$R:/out" --user "$(id -u):$(id -g)" -w /h node:22-slim \
    /cc/node_modules/@anthropic-ai/claude-code-linux-x64/claude -p --model "$model" --output-format stream-json --verbose \
    --tools '' --strict-mcp-config --mcp-config "$mcp" --allowedTools "$allowed" --disable-slash-commands $persist \
    --setting-sources '' --settings '{"disableAllHooks":true}' --max-turns "${maxt:-1}" --system-prompt "$system" "${resume[@]}" "$prompt" \
    </dev/null 2>"$R/stderr$t.log" | python3 -u "$kit/stamp.py" "$t0" "$R/turn$t.jsonl"
  python3 -c 'import json,sys
for l in open(sys.argv[1]):
    try: e=json.loads(l)
    except Exception: continue
    if e.get("session_id"): print(e["session_id"]); break' "$R/turn$t.jsonl" > "$R/session_id"
done
end=$(date +%s.%N)
unset CLAUDE_CODE_OAUTH_TOKEN
printf '{"start":%s,"end":%s}\n' "$start" "$end" > "$R/times.json"
