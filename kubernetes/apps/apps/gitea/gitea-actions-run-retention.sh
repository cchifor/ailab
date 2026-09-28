#!/bin/sh
# gitea-actions-run-retention: delete COMPLETED Gitea Actions runs older than RETENTION_DAYS, paced.
#
# Runs inside the gitea-actions-run-retention CronJob (kubernetes/apps/apps/gitea/actions-run-retention.yaml,
# which holds the why). Plain POSIX sh + curl + jq, so scripts/tests/gitea-actions-run-retention-mock.py can
# run it against a fake Gitea and a fake Prometheus.
#
# Contract (plans/2026-09-28-gitea-actions-retention-and-cnpg-reclone-plan.md):
#   * repos = every repo of ORG (paginated) + EXTRA_REPOS, deduped, walked in rotating order
#   * candidates = status "completed", completed_at non-null and older than one cutoff computed once;
#     found by walking each repo's completed-run listing (id DESC) from the LAST page towards page 1 for at
#     most MAX_PAGES_PER_REPO pages - no early stop, ineligible tail runs are skipped
#   * every candidate is re-read (GET run) right before DELETE and skipped unless still eligible
#   * MAX_DELETES_PER_RUN is GLOBAL; DELETE_PAUSE_SECONDS between deletions; the replication gate
#     (Prometheus: any firing CNPG/Postgres replica alert) is checked immediately before the FIRST
#     deletion and after every GATE_EVERY completed deletions (so the final batch is checked too) and
#     FAILS CLOSED: unreachable Prometheus or an unexpected response shape pauses as well
#   * exit 0 = ran to completion (possibly 0 deletions); 1 = auth/listing/JSON/DELETE failure (never
#     reported as an empty success); 2 = paused by the gate
#
# jq never sits in a pipeline: `jq | tr` would report tr's status and hide a malformed body, so jqr()
# runs jq to a file first and strips CRs afterwards (harmless in the container, required for the mock
# test on Windows, where jq emits CRLF and a stray \r turns a run id into a malformed URL).
# The token is read from GITEA_TOKEN once, written to a 600 curl config on tmpfs and unset; no set -x.
# shellcheck disable=SC2016  # $c inside single quotes is a jq variable, not a shell one
set -eu
umask 077

: "${GITEA_URL:?}" "${GITEA_TOKEN:?}" "${ORG:?}"
: "${EXTRA_REPOS:=}" "${RETENTION_DAYS:=16}" "${MAX_DELETES_PER_RUN:=25}" "${DELETE_PAUSE_SECONDS:=5}"
: "${MAX_PAGES_PER_REPO:=20}" "${GATE_EVERY:=25}" "${DRY_RUN:=true}" "${WORK:=/tmp}"
: "${PROM_URL:=http://kube-prometheus-stack-prometheus.monitoring.svc:9090}"
# single-quoted literal on purpose: inside ${VAR:=...} the quotes and the closing brace of the selector
# would be eaten by the shell (found by the codex impl review)
[ -n "${GATE_QUERY:-}" ] || GATE_QUERY='count(ALERTS{alertstate="firing",alertname=~"CNPG.*|PostgresReplica.*|PostgresReplicationLagHigh"})'
: "${RETENTION_NOW:=}"   # tests only: a fixed epoch for the cutoff (deterministic boundary cases)

cfg="$WORK/curlcfg"; body="$WORK/body"
printf 'header = "Authorization: token %s"\nheader = "Accept: application/json"\n' "$GITEA_TOKEN" > "$cfg"
unset GITEA_TOKEN

api() { # method url -> prints http code, body in $body (000 = transport failure)
  curl -sS --config "$cfg" --max-time 60 -o "$body" -w '%{http_code}' -X "$1" "$2" || echo 000
}
jqr() { # jq -r with jq's OWN status preserved, CRs stripped afterwards
  jq -r "$@" > "$WORK/jq.out" || return 1
  tr -d '\r' < "$WORK/jq.out"
}
snippet() { head -c 160 "$body" 2>/dev/null | tr -d '\n\r'; }
now=${RETENTION_NOW:-$(date -u +%s)}
cutoff=$((now - RETENTION_DAYS * 86400))

deleted=0; already_gone=0; skipped_reval=0; seen=0; deferred=""; gate_state=ok; gated_once=0
skipset=" "   # ids revalidation rejected this execution: a re-read page lists them again
finish() {
  echo "retention: deleted=$deleted already_gone=$already_gone skipped_revalidation=$skipped_reval candidates_seen=$seen repos_deferred=${deferred:-none} gate=$gate_state dry_run=$DRY_RUN"
}
fail() { echo "$1"; finish; exit 1; }
pause() { gate_state=paused; finish; exit 2; }

gate() { # returns 0 = deletion allowed, 1 = pause (fail closed on anything unexpected)
  q=$(printf '%s' "$GATE_QUERY" | jq -sRr @uri | tr -d '\r') || return 1
  code=$(curl -sS --max-time 20 -o "$body" -w '%{http_code}' "$PROM_URL/api/v1/query?query=$q" || echo 000)
  if [ "$code" != 200 ]; then echo "gate: prometheus HTTP $code - pausing (fail closed)"; return 1; fi
  # accept only a success envelope with an instant vector: empty vector = no alert, one sample = its value
  firing=$(jqr 'if .status == "success" and .data.resultType == "vector"
                then (if (.data.result | length) == 0 then "0" else (.data.result[0].value[1] | tostring) end)
                else "invalid" end' "$body") || firing=invalid
  case "$firing" in
    0) return 0 ;;
    ''|invalid) echo "gate: unexpected prometheus response '$(snippet)' - pausing (fail closed)"; return 1 ;;
    *) echo "gate: $firing replica alert(s) firing - pausing"; return 1 ;;
  esac
}
gate_or_pause() { gate || pause; }

echo "retention: cutoff=$(date -u -d "@$cutoff" +%FT%TZ 2>/dev/null || date -u -r "$cutoff" +%FT%TZ) retention_days=$RETENTION_DAYS max=$MAX_DELETES_PER_RUN pause=${DELETE_PAUSE_SECONDS}s pages=$MAX_PAGES_PER_REPO gate_every=$GATE_EVERY dry_run=$DRY_RUN"

# --- repositories: every org repo (paginated) + EXTRA_REPOS, deduped ---------------------------------
repos=""; page=1
while :; do
  code=$(api GET "$GITEA_URL/api/v1/orgs/$ORG/repos?limit=50&page=$page")
  [ "$code" = 200 ] || fail "list org repos: HTTP $code $(snippet)"
  names=$(jqr '.[].full_name' "$body") || fail "list org repos: malformed JSON '$(snippet)'"
  [ -n "$names" ] || break
  repos="$repos
$names"; page=$((page + 1))
done
for r in $EXTRA_REPOS; do repos="$repos
$r"; done
repos=$(printf '%s\n' "$repos" | grep -v '^$' | sort -u)
nrepos=$(printf '%s\n' "$repos" | wc -l | tr -d ' ')
[ "$nrepos" -gt 0 ] || fail "no repositories found"
# rotating start so one big backlog cannot starve the others forever
hour=$(date -u +%H); hour=${hour#0}; hour=${hour:-0}
start=$(( hour % nrepos ))
ordered=$(printf '%s\n' "$repos" | awk -v s="$start" '{a[NR]=$0} END{for(i=0;i<NR;i++) print a[((i+s)%NR)+1]}')
echo "retention: repos ($nrepos, start index $start): $(printf '%s' "$ordered" | tr '\n' ' ')"

budget_left() { [ "$deleted" -lt "$MAX_DELETES_PER_RUN" ]; }

for repo in $ordered; do
  if ! budget_left; then deferred="$deferred$repo,"; continue; fi
  code=$(api GET "$GITEA_URL/api/v1/repos/$repo/actions/runs?status=completed&limit=50&page=1")
  case "$code" in
    200) ;;
    401|403) fail "$repo: list HTTP $code $(snippet)" ;;
    *) echo "$repo: list HTTP $code $(snippet) - skipping repo"; continue ;;
  esac
  total=$(jqr '.total_count // 0' "$body") || fail "$repo: malformed JSON '$(snippet)'"
  case "$total" in ''|*[!0-9]*) fail "$repo: total_count '$total' is not a number" ;; esac
  [ "$total" -gt 0 ] || continue
  page=$(( (total + 49) / 50 )); pages_read=0
  while [ "$page" -ge 1 ] && [ "$pages_read" -lt "$MAX_PAGES_PER_REPO" ] && budget_left; do
    code=$(api GET "$GITEA_URL/api/v1/repos/$repo/actions/runs?status=completed&limit=50&page=$page")
    case "$code" in
      200) ;;
      401|403) fail "$repo: page $page HTTP $code $(snippet)" ;;
      *) echo "$repo: page $page HTTP $code $(snippet) - skipping rest of repo"; break ;;
    esac
    pages_read=$((pages_read + 1))
    ids=$(jqr --argjson c "$cutoff" '[.workflow_runs[]? | select(.status == "completed" and .completed_at != null and (.completed_at | fromdateiso8601) < $c) | .id] | unique | .[]' "$body") \
      || fail "$repo: page $page malformed JSON '$(snippet)'"
    progressed=0
    for id in $ids; do
      budget_left || break
      case "$skipset" in *" $id "*) continue ;; esac
      seen=$((seen + 1))
      if [ "$DRY_RUN" = "true" ]; then
        [ "$seen" -le 20 ] && echo "would delete $repo run $id"
        deleted=$((deleted + 1)); continue
      fi
      # revalidate right before the delete: still completed, still older than the cutoff (reruns move completed_at)
      code=$(api GET "$GITEA_URL/api/v1/repos/$repo/actions/runs/$id")
      case "$code" in
        200) ok=$(jqr --argjson c "$cutoff" 'if .status == "completed" and .completed_at != null and (.completed_at | fromdateiso8601) < $c then "yes" else "no" end' "$body") \
               || fail "$repo run $id: malformed JSON on revalidation '$(snippet)'"
             if [ "$ok" != "yes" ]; then echo "$repo run $id: no longer eligible - skipping"; skipped_reval=$((skipped_reval + 1)); skipset="$skipset$id "; continue; fi ;;
        404) echo "$repo run $id: already gone"; already_gone=$((already_gone + 1)); continue ;;
        401|403) fail "$repo run $id: GET HTTP $code $(snippet)" ;;
        *) echo "$repo run $id: GET HTTP $code $(snippet) - skipping"; skipped_reval=$((skipped_reval + 1)); skipset="$skipset$id "; continue ;;
      esac
      # the gate: immediately before the FIRST mutation (scanning may have taken a while) ...
      if [ "$gated_once" = 0 ]; then gate_or_pause; gated_once=1; fi
      code=$(api DELETE "$GITEA_URL/api/v1/repos/$repo/actions/runs/$id")
      case "$code" in
        204|200) deleted=$((deleted + 1)); progressed=1; echo "deleted $repo run $id" ;;
        404) echo "$repo run $id: already gone"; already_gone=$((already_gone + 1)); continue ;;
        401|403) fail "$repo run $id: DELETE HTTP $code $(snippet)" ;;
        *) deleted=$((deleted + 1)); fail "$repo run $id: DELETE HTTP $code $(snippet) - outcome uncertain, counted, stopping" ;;
      esac
      # ... and after every GATE_EVERY completed deletions, the final batch included
      if [ $((deleted % GATE_EVERY)) -eq 0 ]; then gate_or_pause; fi
      sleep "$DELETE_PAUSE_SECONDS"
    done
    # a deletion shifts the pages: re-read the same page number; otherwise move towards page 1
    if [ "$progressed" = 0 ]; then page=$((page - 1)); fi
  done
  if ! budget_left && [ "$page" -ge 1 ]; then deferred="$deferred$repo(partial),"; fi
done
finish
exit 0
