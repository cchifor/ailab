#!/bin/bash
# kit/codex-arm.sh KIT RUN TARGET HOME_MODE WS PROMPT_FILE [PROMPT2_FILE] - one Codex CLI session for the gate, on the
# worker, through the recording tap (kit/tap.mjs on $TAP_PORT). TARGET picks the arm:
#   chatgpt  the host's own ChatGPT login (built-in openai provider, base URL overridden to the tap)
#   router   the llm-router provider (the worker key, auth command codex-router-key)
#   fwd      the same provider entry, to the tap's plain forwarder (the control arm; the key stops at the tap)
#   vllm     the model server's own /v1/responses (provider `server`, no credential)
# HOME_MODE: `user` = the real ~/.codex (the login is used in place, never copied); `kit` = $KIT/codex-home (generated
# config, no login). Env: MODEL, EFFORT, CAP (minutes), TAP_PORT, SERVER_URL (vllm). With PROMPT2_FILE the session is
# resumed for a second turn. Writes $KIT/runs/$RUN/{events*.jsonl,last*.md,times.jsonl,session_id}.
set -uo pipefail
export PATH=$HOME/.npm-global/bin:$PATH
kit=$1; run=$2; target=$3; mode=$4; ws=$5; p1=$6; p2=${7:-}
R=$kit/runs/$run; mkdir -p "$R"
home=$HOME/.codex; [ "$mode" = kit ] && home=$kit/codex-home
base="http://127.0.0.1:${TAP_PORT}"
case $target in
  chatgpt) ov=(-c 'model_provider="openai"' -c "openai_base_url=\"$base/t/chatgpt/$run/backend-api/codex\"");;
  router)  ov=(-c 'model_provider="llm-router"' -c "model_providers.llm-router.base_url=\"$base/t/router/$run/v1\"");;
  fwd)     ov=(-c 'model_provider="llm-router"' -c "model_providers.llm-router.base_url=\"$base/t/fwd/$run/v1\"");;
  vllm)    ov=(-c 'model_provider="server"' -c "model_providers.server.base_url=\"$base/t/vllm/$run/v1\"");;
  llama)   ov=(-c 'model_provider="server"' -c "model_providers.server.base_url=\"$base/t/llama/$run/v1\"");;
  *) echo "bad target" >&2; exit 2;;
esac
common=(--skip-git-repo-check --json -c "model=\"$MODEL\"" -c "model_reasoning_effort=\"${EFFORT:-medium}\"" "${ov[@]}")
t0=$(date +%s.%N)
CODEX_HOME=$home timeout --kill-after=30s "${CAP:-10}m" codex exec "${common[@]}" -C "$ws" -s workspace-write \
  --output-last-message "$R/last.md" "$(cat "$p1")" </dev/null >"$R/events.jsonl" 2>"$R/stderr.log"
rc=$?
python3 -c 'import json,sys
for l in open(sys.argv[1]):
    e=json.loads(l)
    if e.get("type")=="thread.started": print(e["thread_id"]); break' "$R/events.jsonl" > "$R/session_id" 2>/dev/null
if [ -n "$p2" ] && [ -s "$R/session_id" ]; then
  CODEX_HOME=$home timeout --kill-after=30s "${CAP:-10}m" codex exec "${common[@]}" --output-last-message "$R/last.2.md" \
    resume "$(cat "$R/session_id")" "$(cat "$p2")" </dev/null >"$R/events.2.jsonl" 2>"$R/stderr.2.log"
  rc=$((rc + $?))
fi
t1=$(date +%s.%N)
printf '{"rc":%d,"start":%s,"end":%s}\n' "$rc" "$t0" "$t1" > "$R/times.json"
