#!/usr/bin/env bash
# Recycle ONE airlock App sandbox through airlock's own lifecycle (teardown -> deploy), so the new
# pod picks up the current sandbox spec (e.g. the 100m CPU request from platform#1644). Never
# deletes a pod: airlock's DB would still say RUNNING and no timer recreates a `forever` sandbox.
#
#   scripts/airlock-recycle-sandbox.sh --app ryan --tenant dbe45925-7400-4157-ba89-2968bbe018b3
#
# Auth: a tenant member's Bearer token (their platform session). It is read from a hidden prompt or
# from a file descriptor (--token-fd 3 3<<<"$TOKEN" style, or 3<token-file), NEVER from an argument
# or an exported variable; it reaches curl through a --config document on stdin, so it is in no
# argv, no shell history, no log line. Error bodies are printed without any Authorization text.
#
# Contract (deployed airlock >= build 0455699f2 with APP__AIRLOCK__APP_ASYNC_LIFECYCLE_ENABLED=true):
#   POST /api/airlock/v1/apps/{id}/teardown  -> 202 AppOperationOut {operation_id,status,...}
#   POST /api/airlock/v1/apps/{id}/deploy    -> 202 AppOperationOut
#   GET  /api/airlock/v1/app-operations/{op} -> status queued|running|succeeded|failed|needs_attention
# Older builds answer 204 (teardown) / 200 AppOut (deploy) synchronously; both are handled by
# polling GET /api/airlock/v1/apps/{id} (sandbox.status) instead of an operation.
#
# Exit codes: 0 ok | 2 usage/token | 3 preflight refused (wrong tenant, auth, app not found) |
#   4 teardown failed | 5 deploy failed (the app is STOPPED: re-run with --resume-deploy) |
#   6 ambiguous POST (timeout: read the app status before doing anything) | 7 verify failed.
set -euo pipefail
set +x

BASE_URL="https://apps.strive.place"
APP="" TENANT="" TOKEN_FD="" RESUME=0 POLL_TIMEOUT=900 INTERVAL=10
KUBE_CONTEXT="admin@ai" NAMESPACE="strive-sandboxes-ailab" SKIP_KUBE=0
API="/api/airlock/v1"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --tenant) TENANT="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --token-fd) TOKEN_FD="$2"; shift 2 ;;
    --resume-deploy) RESUME=1; shift ;;
    --poll-timeout) POLL_TIMEOUT="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --kube-context) KUBE_CONTEXT="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --skip-kube-verify) SKIP_KUBE=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[ -n "$APP" ] && [ -n "$TENANT" ] || { echo "--app and --tenant are required" >&2; usage; }
case "$APP" in *[!A-Za-z0-9._-]*) echo "refusing app id with unexpected characters" >&2; exit 2 ;; esac

# The bearer must only ever travel to the trusted HTTPS edge (Gatekeeper authenticates there and
# injects X-Gatekeeper-Tenant; a raw in-cluster airlock URL would bypass that and is refused).
if [ "${AIRLOCK_RECYCLE_ALLOW_HTTP:-0}" != "1" ]; then
  case "$BASE_URL" in
    https://[A-Za-z0-9]*) ;;
    *) echo "--base-url must be an https:// origin (got: $BASE_URL)" >&2; exit 2 ;;
  esac
fi
case "$BASE_URL" in */) BASE_URL="${BASE_URL%/}" ;; esac
command -v curl >/dev/null && command -v jq >/dev/null || { echo "curl and jq are required" >&2; exit 2; }

TOKEN=""
if [ -n "$TOKEN_FD" ]; then
  IFS= read -r TOKEN <&"$TOKEN_FD" || { echo "could not read a token from fd $TOKEN_FD" >&2; exit 2; }
else
  IFS= read -rs -p "Bearer token for $BASE_URL (not echoed): " TOKEN </dev/tty; echo >&2
fi
TOKEN="${TOKEN//$'\r'/}"
[ -n "$TOKEN" ] || { echo "no token provided" >&2; exit 2; }
trap 'unset TOKEN' EXIT

BODY="$(mktemp)"; trap 'rm -f "$BODY"; unset TOKEN' EXIT
say() { printf '%s\n' "$*" >&2; }
phase() { printf 'PHASE=%s\n' "$1"; }

# api METHOD PATH [JSON]  -> prints the HTTP code; body lands in $BODY. curl exit 28 = timeout.
api() {
  local method="$1" path="$2" data="${3:-}" code
  set +e
  code=$(printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/json"\n' "$TOKEN" \
    | curl -sS --config - --max-redirs 0 --max-time 60 -X "$method" \
        ${data:+-H 'Content-Type: application/json' --data "$data"} \
        -o "$BODY" -w '%{http_code}' "$BASE_URL$API$path")
  local rc=$?
  set -e
  [ $rc -eq 0 ] || { say "curl failed (exit $rc) on $method $path"; return $rc; }
  printf '%s' "$code"
}
# redact: the exact token (literal split/join, not a regex) and any "Bearer <…>" — the token reaches
# jq through ITS environment only (never argv); jq is a short-lived child of this shell.
redact() { AIRLOCK_TOKEN_REDACT="$TOKEN" jq -Rr 'split(env.AIRLOCK_TOKEN_REDACT) | join("<redacted>") | gsub("Bearer [A-Za-z0-9._~+/=-]+"; "Bearer <redacted>")'; }
# show_error LABEL CODE: prints only selected, redacted diagnostics; a non-JSON body (an edge error
# page) is shown as redacted text, never the raw response.
show_error() {
  local msg
  msg=$( (jq -r '[.detail, .message, .error.message, .error.code] | map(select(. != null) | tostring) | join(" | ")' "$BODY" 2>/dev/null || cat "$BODY") | tr -d '
' | cut -c1-300 | redact)
  say "$1: HTTP $2 ${msg:-<no body>}"
}

# poll_operation OP_ID -> 0 on succeeded, 1 otherwise
poll_operation() {
  local op="$1" deadline=$((SECONDS + POLL_TIMEOUT)) code status
  while :; do
    code=$(api GET "/app-operations/$op") || return 1
    [ "$code" = 200 ] || { show_error "operation $op" "$code"; return 1; }
    status=$(jq -r '.status // ""' "$BODY")
    say "  operation $op: $status ($(jq -r '.phase // ""' "$BODY"))"
    case "$status" in
      succeeded) return 0 ;;
      failed|needs_attention) say "  error: $(jq -r '[.error.code, .error.message] | map(select(. != null) | tostring) | join(": ")' "$BODY" 2>/dev/null | cut -c1-300 | redact)"; return 1 ;;
    esac
    [ $SECONDS -lt $deadline ] || { say "  timed out after ${POLL_TIMEOUT}s"; return 1; }
    sleep "$INTERVAL"
  done
}

# poll_app WANT -> waits until the app's sandbox is (running|gone)
poll_app() {
  local want="$1" deadline=$((SECONDS + POLL_TIMEOUT)) code st
  while :; do
    code=$(api GET "/apps/$APP") || return 1
    [ "$code" = 200 ] || { show_error "app $APP" "$code"; return 1; }
    st=$(jq -r '.sandbox.status // "none"' "$BODY")
    say "  app $APP: status=$(jq -r '.status' "$BODY") sandbox=$st"
    case "$want:$st" in
      running:RUNNING|running:running) return 0 ;;
      gone:none|gone:STOPPED|gone:stopped|gone:TERMINATED|gone:terminated) return 0 ;;
    esac
    [ $SECONDS -lt $deadline ] || { say "  timed out after ${POLL_TIMEOUT}s"; return 1; }
    sleep "$INTERVAL"
  done
}

# ---- preflight: identity, tenant, current state -------------------------------------------------
phase preflight
code=$(api GET "/apps/$APP") || exit 3
case "$code" in
  200) ;;
  401|403) show_error "preflight" "$code"; say "not authenticated / not a member — nothing was changed"; exit 3 ;;
  404) say "app $APP not found for this session — nothing was changed"; exit 3 ;;
  *) show_error "preflight" "$code"; exit 3 ;;
esac
got_tenant=$(jq -r '.tenant_id // ""' "$BODY")
[ "$got_tenant" = "$TENANT" ] || { say "app $APP belongs to tenant '$got_tenant', expected '$TENANT' — refusing"; exit 3; }
app_status=$(jq -r '.status // ""' "$BODY"); sbx_status=$(jq -r '.sandbox.status // "none"' "$BODY")
say "app $APP tenant ok; status=$app_status sandbox=$sbx_status lifecycle=$(jq -r '.lifecycle_mode // ""' "$BODY")"
# The App row stays ACTIVE across teardown/deploy (ARCHIVED/DELETING/… are different lifecycles);
# the SANDBOX state is what teardown changes. Resume is only valid once teardown has completed.
[ "$app_status" = ACTIVE ] || { say "app is $app_status, not ACTIVE — refusing"; exit 3; }
if [ "$RESUME" = 1 ]; then
  case "$sbx_status" in
    none|STOPPED|FAILED) ;;
    *) say "--resume-deploy requires a torn-down sandbox (none/STOPPED/FAILED); it is $sbx_status — run without --resume-deploy, or wait for the teardown to finish. Nothing was changed"; exit 3 ;;
  esac
fi
# airlock LABELS its pods with airlock.strive.io/tenant and airlock.strive.io/sandbox-id (a new value
# per deploy) and carries the app id as an ANNOTATION, so: label selector on the tenant, then filter
# the app id in jq. Prints the pod object or nothing.
find_pods() {
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods -l "airlock.strive.io/tenant=$TENANT" -o json 2>/dev/null     | jq --arg app "$APP" '[.items[] | select(.metadata.annotations["airlock.strive.io/app-id"] == $app)]'
}
old_uid=""
if [ "$SKIP_KUBE" = 0 ]; then
  old_uid=$(find_pods | jq -r 'sort_by(.metadata.creationTimestamp) | last | .metadata.uid // ""')
  say "current pod uid: ${old_uid:-none}"
fi

# ---- teardown -------------------------------------------------------------------------------------
if [ "$RESUME" = 0 ]; then
  phase teardown
  set +e; code=$(api POST "/apps/$APP/teardown"); rc=$?; set -e
  if [ $rc -eq 28 ]; then say "teardown POST timed out: it MAY have started. Read GET $API/apps/$APP; if the sandbox is gone, re-run with --resume-deploy. Not retrying."; exit 6; fi
  [ $rc -eq 0 ] || exit 4
  case "$code" in
    202) op=$(jq -r '.operation_id' "$BODY"); say "  accepted, operation $op"; poll_operation "$op" || { say "teardown did not succeed — NOT deploying"; exit 4; } ;;
    200|204) say "  synchronous teardown answered $code; waiting for the sandbox to be gone"; poll_app gone || exit 4 ;;
    401|403) show_error "teardown" "$code"; exit 3 ;;
    *) show_error "teardown" "$code"; exit 4 ;;
  esac
else
  phase teardown-skipped
  say "resume: skipping teardown; the app must already be STOPPED (sandbox=$sbx_status)"
fi

# ---- deploy --------------------------------------------------------------------------------------
phase deploy
set +e; code=$(api POST "/apps/$APP/deploy"); rc=$?; set -e
if [ $rc -eq 28 ]; then say "deploy POST timed out: it MAY have started. Read GET $API/apps/$APP; re-run with --resume-deploy only if the sandbox is still gone."; exit 6; fi
[ $rc -eq 0 ] || { say "the app is torn down; re-run with --resume-deploy once the API is reachable"; exit 5; }
case "$code" in
  202) op=$(jq -r '.operation_id' "$BODY"); say "  accepted, operation $op"; poll_operation "$op" || { say "deploy did not succeed; the app is STOPPED — fix and re-run with --resume-deploy"; exit 5; } ;;
  200) say "  synchronous deploy answered 200; waiting for the sandbox to run"; poll_app running || exit 5 ;;
  401|403) show_error "deploy" "$code"; say "session expired AFTER teardown: the app is STOPPED. Re-authenticate and re-run with --resume-deploy"; exit 5 ;;
  *) show_error "deploy" "$code"; say "the app is STOPPED — re-run with --resume-deploy after fixing the cause"; exit 5 ;;
esac

# ---- verify --------------------------------------------------------------------------------------
phase verify
[ "$SKIP_KUBE" = 1 ] && { say "kube verify skipped"; exit 0; }
deadline=$((SECONDS + POLL_TIMEOUT))
while :; do
  # newest pod for this app that is not the pre-recycle one (the old pod may linger Terminating)
  pod_json=$(find_pods | jq --arg old "$old_uid" '[.[] | select(.metadata.uid != $old)] | sort_by(.metadata.creationTimestamp) | last // empty')
  new_uid=$(printf '%s' "${pod_json:-null}" | jq -r '.metadata.uid // ""')
  ready=$(printf '%s' "${pod_json:-null}" | jq -r '[.status.conditions[]? | select(.type=="Ready") | .status] | first // "Unknown"')
  if [ -n "$new_uid" ] && [ "$new_uid" != "$old_uid" ] && [ "$ready" = True ]; then break; fi
  [ $SECONDS -lt $deadline ] || { say "no new Ready pod for $APP within ${POLL_TIMEOUT}s (uid=$new_uid ready=$ready)"; exit 7; }
  sleep "$INTERVAL"
done
printf '%s' "$pod_json" | jq -r '"pod \(.metadata.name) uid \(.metadata.uid) node \(.spec.nodeName) qos \(.status.qosClass)",
  (.spec.containers[] | "  container \(.name): requests \(.resources.requests // {} | tojson) limits \(.resources.limits // {} | tojson)")'
phase finished
