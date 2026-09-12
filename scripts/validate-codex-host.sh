#!/usr/bin/env bash
# The per-host half of validate-codex-fleet.sh (shipped to each host with ansible's `script`
# module, run as root). Prints ONE line — "<host> OK ..." or "<host> FAIL ..." — and exits 1 on
# FAIL. Runnable by hand on a host too: sudo scripts/validate-codex-host.sh [user].
#
# What it checks, as the user codex runs as on this host (dev workers: c4; reviewers: codexrun):
#   1. ~/.codex/auth.json exists and is parseable — and whether it is the OpenBao-rendered
#      projection (refresh_token EMPTY; the whole point of docs/runbooks/openbao-dev-workers.md
#      § "The shared codex login") or a hand-copied login that still carries a refresh token;
#   2. how many days the access token has left;
#   3. a REAL `codex exec` round-trip to the API with a fixed prompt, checking the reply.
# --dangerously-bypass-approvals-and-sandbox: the prompt uses no tools, and the dev workers'
# bubblewrap cannot set up its namespace anyway (the known bwrap loopback failure).
set -u
h=$(hostname -s)
case "$h" in
  reviewer-*) u="${1:-codexrun}" ;;
  *)          u="${1:-c4}" ;;
esac
home=$(getent passwd "$u" | cut -d: -f6)
f="$home/.codex/auth.json"
if [ ! -s "$f" ]; then echo "$h FAIL user=$u no $f"; exit 1; fi

auth=$(python3 - "$f" <<'PY'
import base64, json, sys, time
d = json.load(open(sys.argv[1])); t = d.get("tokens") or {}
if not t.get("access_token"):
    # `codex login --with-api-key` shape: no ChatGPT tokens at all, just OPENAI_API_KEY.
    print("API-KEY(not the shared login)" if d.get("OPENAI_API_KEY") else "UNKNOWN-SHAPE"); sys.exit(0)
p = t["access_token"].split(".")[1]; p += "=" * (-len(p) % 4)
left = (json.loads(base64.urlsafe_b64decode(p))["exp"] - time.time()) / 86400
kind = "projection(no-refresh-token)" if t.get("refresh_token", "") == "" else "HAS-REFRESH-TOKEN(hand-copied)"
print(f"{kind} access_token_left={left:.1f}d last_refresh={str(d.get('last_refresh',''))[:10]}")
PY
) || { echo "$h FAIL user=$u unreadable $f"; exit 1; }

# A LOGIN shell for the user: codex is an npm global under the user's own prefix (~/.npm-global/bin
# on the dev workers, /usr/bin on the reviewers), reachable through the user's profile PATH, not
# root's.
out=$(cd / && sudo -n -u "$u" -H bash -lc 'timeout 150 codex exec --skip-git-repo-check \
        --dangerously-bypass-approvals-and-sandbox -m gpt-6-astra -c "model_reasoning_effort=\"low\"" \
        "Reply with exactly: OK" </dev/null' 2>&1); rc=$?
last=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -1)
if [ "$rc" -eq 0 ] && [ "$last" = "OK" ]; then
  echo "$h OK   user=$u $auth codex=$(sudo -n -u "$u" -H bash -lc 'codex --version' 2>/dev/null | awk '{print $NF}')"
else
  err=$(printf '%s\n' "$out" | grep -iE 'error|401|expired|revoked|unauthorized' | head -1 | cut -c1-140)
  echo "$h FAIL user=$u $auth rc=$rc ${err:-$last}"
  exit 1
fi
