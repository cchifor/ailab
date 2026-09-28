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
#     (Prometheus: any firing CNPG/Postgres replica alert) is checked before the first deletion and every
#     GATE_EVERY deletions and FAILS CLOSED (unreachable Prometheus pauses too)
#   * exit 0 = ran to completion (possibly 0 deletions); 1 = auth/listing/DELETE failure (never reported
#     as an empty success); 2 = paused by the gate
#
# jq output is piped through tr -d '\r' everywhere: harmless in the container, required for the mock
# test on Windows, where jq emits CRLF and a stray \r turns a run id into a malformed URL.
# The token is read from GITEA_TOKEN once, written to a 600 curl config on tmpfs and unset; no set -x.
set -eu
umask 077

: "${GITEA_URL:?}" "${GITEA_TOKEN:?}" "${ORG:?}"
: "${EXTRA_REPOS:=}" "${RETENTION_DAYS:=16}" "${MAX_DELETES_PER_RUN:=25}" "${DELETE_PAUSE_SECONDS:=5}"
: "${MAX_PAGES_PER_REPO:=20}" "${GATE_EVERY:=25}" "${DRY_RUN:=true}" "${WORK:=/tmp}"
: "${PROM_URL:=http://kube-prometheus-stack-prometheus.monitoring.svc:9090}"
: "${GATE_QUERY:=count(ALERTS{alertstate="firing",alertname=~"CNPG.*|PostgresReplica.*|PostgresReplicationLagHigh"})}"
: "${GATE_DISABLED:=false}"   # tests only; production keeps the gate

cfg="$WORK/curlcfg"; body="$WORK/body"
printf 'header = "Authorization: token %s"\nheader = "Accept: application/json"\n' "$GITEA_TOKEN" > "$cfg"
unset GITEA_TOKEN

api() { # method url -> prints http code, body in $body
  curl -sS --config "$cfg" --max-time 60 -o "$body" -w '%{http_code}' -X "$1" "$2" || echo 000
}
snippet() { head -c 160 "$body" 2>/dev/null | tr -d '\n'; }
now=$(date -u +%s)
cutoff=$((now - RETENTION_DAYS * 86400))

deleted=0; already_gone=0; skipped_reval=0; seen=0; deferred=""; gate_state=ok
skipset=" "   # ids revalidation rejected this execution: a re-read page lists them again
finish() {
  echo "retention: deleted=$deleted already_gone=$already_gone skipped_revalidation=$skipped_reval candidates_seen=$seen repos_deferred=${deferred:-none} gate=$gate_state dry_run=$DRY_RUN"
}

gate() { # returns 0 = deletion allowed, 1 = pause
  [ "$GATE_DISABLED" = "true" ] && return 0
  q=$(printf '%s' "$GATE_QUERY" | jq -sRr @uri | tr -d '\r')
  code=$(curl -sS --max-time 20 -o "$body" -w '%{http_code}' "$PROM_URL/api/v1/query?query=$q" || echo 000)
  if [ "$code" != 200 ]; then echo "gate: prometheus HTTP $code - pausing (fail closed)"; return 1; fi
  firing=$(jq -r '.data.result[0].value[1] // "0"' "$body" 2>/dev/null | tr -d '\r' || echo err)
  case "$firing" in
    0) return 0 ;;
    *) echo "gate: $firing replica alert(s) firing - pausing"; return 1 ;;
  esac
}

echo "retention: cutoff=$(date -u -d "@$cutoff" +%FT%TZ 2>/dev/null || date -u -r "$cutoff" +%FT%TZ) retention_days=$RETENTION_DAYS max=$MAX_DELETES_PER_RUN pause=${DELETE_PAUSE_SECONDS}s pages=$MAX_PAGES_PER_REPO dry_run=$DRY_RUN"

# --- repositories: every org repo (paginated) + EXTRA_REPOS, deduped ---------------------------------
repos=""; page=1
while :; do
  code=$(api GET "$GITEA_URL/api/v1/orgs/$ORG/repos?limit=50&page=$page")
  [ "$code" = 200 ] || { echo "list org repos: HTTP $code $(snippet)"; finish; exit 1; }
  names=$(jq -r '.[].full_name' "$body" 2>/dev/null | tr -d '\r') || { echo "list org repos: malformed JSON"; finish; exit 1; }
  [ -n "$names" ] || break
  repos="$repos
$names"; page=$((page + 1))
done
for r in $EXTRA_REPOS; do repos="$repos
$r"; done
repos=$(printf '%s\n' "$repos" | grep -v '^$' | sort -u)
nrepos=$(printf '%s\n' "$repos" | wc -l | tr -d ' ')
[ "$nrepos" -gt 0 ] || { echo "no repositories found"; finish; exit 1; }
# rotating start so one big backlog cannot starve the others forever
hour=$(date -u +%H); hour=${hour#0}; hour=${hour:-0}
start=$(( hour % nrepos ))
ordered=$(printf '%s\n' "$repos" | awk -v s="$start" '{a[NR]=$0} END{for(i=0;i<NR;i++) print a[((i+s)%NR)+1]}')
echo "retention: repos ($nrepos, start index $start): $(printf '%s' "$ordered" | tr '\n' ' ')"

# --- initial gate ----------------------------------------------------------------------------------
if [ "$DRY_RUN" != "true" ] && ! gate; then gate_state=paused; finish; exit 2; fi

budget_left() { [ "$deleted" -lt "$MAX_DELETES_PER_RUN" ]; }

for repo in $ordered; do
  if ! budget_left; then deferred="$deferred$repo,"; continue; fi
  code=$(api GET "$GITEA_URL/api/v1/repos/$repo/actions/runs?status=completed&limit=50&page=1")
  case "$code" in
    200) ;;
    401|403) echo "$repo: list HTTP $code $(snippet)"; finish; exit 1 ;;
    *) echo "$repo: list HTTP $code $(snippet) - skipping repo"; continue ;;
  esac
  total=$(jq -r '.total_count // 0' "$body" 2>/dev/null | tr -d '\r') || { echo "$repo: malformed JSON"; finish; exit 1; }
  [ "$total" -gt 0 ] || continue
  page=$(( (total + 49) / 50 )); pages_read=0; repo_done=0
  while [ "$page" -ge 1 ] && [ "$pages_read" -lt "$MAX_PAGES_PER_REPO" ] && budget_left; do
    code=$(api GET "$GITEA_URL/api/v1/repos/$repo/actions/runs?status=completed&limit=50&page=$page")
    case "$code" in
      200) ;;
      401|403) echo "$repo: page $page HTTP $code $(snippet)"; finish; exit 1 ;;
      *) echo "$repo: page $page HTTP $code $(snippet) - skipping rest of repo"; break ;;
    esac
    pages_read=$((pages_read + 1))
    ids=$(jq -r --argjson c "$cutoff" '[.workflow_runs[]? | select(.status=="completed" and .completed_at!=null and (.completed_at|fromdateiso8601) < $c) | .id] | unique | .[]' "$body" 2>/dev/null | tr -d '\r') \
      || { echo "$repo: page $page malformed JSON"; finish; exit 1; }
    progressed=0
    for id in $ids; do
      budget_left || break
      case "$skipset" in *" $id "*) continue ;; esac
      seen=$((seen + 1))
      if [ "$DRY_RUN" = "true" ]; then
        [ "$seen" -le 20 ] && echo "would delete $repo run $id"
        deleted=$((deleted + 1)); continue
      fi
      if [ "$deleted" -gt 0 ] && [ $((deleted % GATE_EVERY)) -eq 0 ] && ! gate; then gate_state=paused; finish; exit 2; fi
      # revalidate right before the delete: still completed, still older than the cutoff (reruns move completed_at)
      code=$(api GET "$GITEA_URL/api/v1/repos/$repo/actions/runs/$id")
      case "$code" in
        200) ok=$(jq -r --argjson c "$cutoff" 'if .status=="completed" and .completed_at!=null and (.completed_at|fromdateiso8601) < $c then "yes" else "no" end' "$body" 2>/dev/null | tr -d '\r' || echo err)
             if [ "$ok" != "yes" ]; then echo "$repo run $id: no longer eligible ($ok) - skipping"; skipped_reval=$((skipped_reval + 1)); skipset="$skipset$id "; continue; fi ;;
        404) echo "$repo run $id: already gone"; already_gone=$((already_gone + 1)); continue ;;
        401|403) echo "$repo run $id: GET HTTP $code $(snippet)"; finish; exit 1 ;;
        *) echo "$repo run $id: GET HTTP $code $(snippet) - skipping"; skipped_reval=$((skipped_reval + 1)); skipset="$skipset$id "; continue ;;
      esac
      code=$(api DELETE "$GITEA_URL/api/v1/repos/$repo/actions/runs/$id")
      case "$code" in
        204|200) deleted=$((deleted + 1)); progressed=1; echo "deleted $repo run $id" ;;
        404) echo "$repo run $id: already gone"; already_gone=$((already_gone + 1)) ;;
        401|403) echo "$repo run $id: DELETE HTTP $code $(snippet)"; finish; exit 1 ;;
        *) deleted=$((deleted + 1)); echo "$repo run $id: DELETE HTTP $code $(snippet) - outcome uncertain, counted, stopping"; finish; exit 1 ;;
      esac
      sleep "$DELETE_PAUSE_SECONDS"
    done
    # a deletion shifts the pages: re-read the same page number; otherwise move towards page 1
    if [ "$progressed" = 0 ]; then page=$((page - 1)); fi
  done
  budget_left || repo_done=1
  [ "$repo_done" = 1 ] && [ "$page" -ge 1 ] && deferred="$deferred$repo(partial),"
done
finish
exit 0
