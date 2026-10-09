#!/usr/bin/env bash
# The per-host half of validate-codex-fleet.sh (shipped to each host with ansible's `script`
# module, run as root). Prints ONE line — "<host> OK ..." or "<host> FAIL ..." — and exits 1 on
# FAIL. Runnable by hand on a host too: sudo scripts/validate-codex-host.sh [user].
#
# What it checks, as the user codex runs as on this host (dev workers: c4; reviewers: codexrun):
#   1. ~/.codex/auth.json exists and is parseable — and whether it is the OpenBao-rendered
#      projection (refresh_token EMPTY; the whole point of docs/runbooks/openbao-dev-workers.md
#      § "The shared codex login") or a login made on the host that carries a refresh token
#      (host-owned: the dev workers since 2026-09-29, § "Host-owned codex logins") — and whether
#      that matches what the host's /etc/openbao-agent/agent.hcl says it should hold (FAIL if not);
#   2. how many days the access token has left;
#   3. a REAL `codex exec` round-trip to the API with a fixed prompt, checking the reply.
# --dangerously-bypass-approvals-and-sandbox: the prompt uses no tools, so there is nothing to sandbox.
# (The dev workers' old bwrap loopback failure is fixed by the dev_worker role's codex_sandbox.yml.)
set -u
h=$(hostname -s)
case "$h" in
  reviewer-*) u="${1:-codexrun}" ;;
  *)          u="${1:-c4}" ;;
esac
home=$(getent passwd "$u" | cut -d: -f6)
f="$home/.codex/auth.json"

# A user whose codex talks to the LLM router (dev workers since 2026-10-09: top-level `model_provider`
# naming the role's [model_providers.*] table) authenticates with the router key the provider's `auth`
# command prints, not with auth.json: check that command yields a key (the value is discarded), then
# the same real round-trip below, which goes through the router.
auth=""
provider=$(awk -F'"' '/^\[/{exit} /^model_provider[[:space:]]*=/{print $2; exit}' "$home/.codex/config.toml" 2>/dev/null)
if [ -n "$provider" ] && [ "$provider" != "openai" ]; then
  # CODEX_ROUTER_NO_CACHE=1: a LIVE vault read (the helper's cached fallback would hide a dead cred path).
  if ! kerr=$(sudo -n -u "$u" -H env CODEX_ROUTER_NO_CACHE=1 /usr/local/bin/codex-router-key 2>&1 >/dev/null); then
    echo "$h FAIL user=$u router($provider) no key from the vault: $(printf '%s' "$kerr" | head -1 | cut -c1-160)"; exit 1
  fi
  auth="router($provider)"
elif [ ! -s "$f" ]; then echo "$h FAIL user=$u no $f"; exit 1; fi

if [ -z "$auth" ]; then
auth=$(python3 - "$f" <<'PY'
import base64, json, sys, time
d = json.load(open(sys.argv[1])); t = d.get("tokens") or {}
if not t.get("access_token"):
    # `codex login --with-api-key` shape: no ChatGPT tokens at all, just OPENAI_API_KEY.
    print("API-KEY(not the shared login)" if d.get("OPENAI_API_KEY") else "UNKNOWN-SHAPE"); sys.exit(0)
p = t["access_token"].split(".")[1]; p += "=" * (-len(p) % 4)
left = (json.loads(base64.urlsafe_b64decode(p))["exp"] - time.time()) / 86400
kind = "projection(no-refresh-token)" if t.get("refresh_token", "") == "" else "host-owned(refresh-token)"
print(f"{kind} access_token_left={left:.1f}d last_refresh={str(d.get('last_refresh',''))[:10]}")
PY
) || { echo "$h FAIL user=$u unreadable $f"; exit 1; }

# What this host is CONFIGURED to hold: the bao agent renders the login only when its agent.hcl has a
# template for this user's auth.json (dev workers: dev_worker_codex_host_owned_login=false; reviewers:
# pr_reviewer_enable_openbao). Otherwise the login is one the operator made on the host. A mismatch
# FAILs even when the live exec below would pass: a stale projection on a host-owned worker keeps
# answering only while the app-server daemon holds a login in memory (dev-worker-3, 2026-09-29).
if grep -qs "destination *= *\"$f\"" /etc/openbao-agent/agent.hcl; then want=projection; else want=host-owned; fi
case "$auth" in
  "$want"*|API-KEY*|UNKNOWN-SHAPE*) ;;
  *) echo "$h FAIL user=$u $auth expected=$want"; exit 1 ;;
esac
fi

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
