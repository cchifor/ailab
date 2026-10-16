#!/usr/bin/env bash
# gitea-owner-merge.sh REPO PR FULL_HEAD_SHA [--approve-pin] [--execute | --dry-run] - merge an owner-gated PR in
# cchifor/REPO with a one-shot owner token. The procedure around it: docs/runbooks/owner-credentials.md.
#
# A DRY RUN BY DEFAULT. Without --execute it runs ONE pass of the credential-free gate below, prints what it would
# do, and exits: it never calls kubectl, never mints, comments, re-runs or merges, and never waits.
#
# Owner-only Gitea operations (merging a PR that touches owner-protected files, posting `approve-pin`) are
# automated (auth-hardening plan, ruling R13, 2026-10-07): no standing owner credential exists. Each run mints
# a token for the site-admin owner `chifor` inside the Gitea pod (cluster-admin `kubectl exec`), uses it for
# one operation and deletes it from the database at once.
#
# 1. THE GATE runs with NO owner credential (routine reads as workstation-bot through scripts/gitea-api.sh):
#    - the PR is open, not merged, not a draft or WIP, targets main, and its head is still FULL_HEAD_SHA;
#    - reviewer-claude's and reviewer-codex's latest non-dismissed review AT that head is APPROVED;
#    - every pattern in scripts/owner-ops/required-contexts-REPO.txt matches a context that passed (success or
#      skipped) in the COMBINED status, read page by page until an empty page (the raw /statuses list pages
#      unreliably past 250 entries), and NO context at the head failed or is pending: force_merge bypasses
#      every branch-protection check, so this gate is the only one left;
#    - the live protection (GET /branches/main, readable without admin) requires nothing the file lacks.
#    While contexts are pending or missing it waits, still without a credential, re-running the whole gate
#    on every poll (OWNER_MERGE_MAX_POLLS x OWNER_MERGE_POLL_SECONDS, default 120 x 30 s).
# 2. THE OWNER OPERATION, only with --execute and only after the gate passes. The token lives in a shell variable, goes to the fixed
#    origin only on curl's STDIN (`-q ... -H @-`), never in an argument, never printed. A trap on
#    EXIT/INT/TERM/HUP deletes it by name; the script then proves it dead (gone from the database, and its next
#    use answers 401) and lists the owner's remaining tokens by name.
#    - plain merge first; force_merge ONLY after Gitea's "Changed protected files" refusal;
#    - --approve-pin (platform's owner-ack gate): when CI / owner-ack failed and nothing else did, token 1
#      posts `approve-pin FULL_HEAD_SHA`, re-runs the failed jobs of the owner-ack run and is deleted; the
#      script waits WITHOUT a token until every context passes, then token 2 merges. If owner-ack is already
#      green, no pin is posted. A token is never held across a wait.
# Exit: 0 merged (dry run: the gate passes now); 2 bad input (or no contexts file for REPO); 3 refused by the
# gate, nothing more minted; 4 the owner operation failed (its token deleted); 5 a deletion was NOT confirmed:
# delete that token now; 6 dry run only: checks are still pending or missing (an --execute run would wait).
#   scripts/gitea-owner-merge.sh platform 2138 8f9c07439418a66197025f5685a51b948ef191af             # dry run
#   scripts/gitea-owner-merge.sh platform 2138 8f9c07439418a66197025f5685a51b948ef191af --execute
#   scripts/gitea-owner-merge.sh platform 2149 8a8daed88aa05197de65bfd2d97af00aa740f250 --approve-pin --execute
set -uo pipefail
set +x # never trace: a traced assignment would print the token

ORIGIN="https://git.chifor.me/api/v1"
ORG="cchifor"
OWNER="chifor"
KCTX="admin@ai"
HERE=$(cd "$(dirname "$0")" && pwd)
POLL_SECONDS=${OWNER_MERGE_POLL_SECONDS:-30}
MAX_POLLS=${OWNER_MERGE_MAX_POLLS:-120}

ts() { date -u +%H:%M:%SZ; }
log() { echo "[$(ts)] $*"; }
stop() { local code=$1; shift; echo "[$(ts)] STOP: $*" >&2; exit "$code"; }

USAGE="usage: gitea-owner-merge.sh REPO PR FULL_HEAD_SHA [--approve-pin] [--execute | --dry-run]"
POS=(); PIN=""; EXECUTE=""; DRY=""
for a in "$@"; do
  case $a in
    --approve-pin) PIN=1 ;;
    --execute) EXECUTE=1 ;;
    --dry-run) DRY=1 ;;
    -*) stop 2 "unknown option $a; $USAGE" ;;
    *) POS+=("$a") ;;
  esac
done
[ ${#POS[@]} -eq 3 ] || stop 2 "$USAGE"
[ -z "$EXECUTE" ] || [ -z "$DRY" ] || stop 2 "--execute and --dry-run exclude each other"
REPO=${POS[0]}; PR=${POS[1]}; HEAD_SHA=${POS[2]}
{ [[ "$REPO" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] && [[ "$REPO" != *..* ]]; } || stop 2 "REPO must be a repository name in $ORG"
[[ "$PR" =~ ^[1-9][0-9]*$ ]] || stop 2 "PR must be a pull request number"
[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || stop 2 "give the full 40-hex (lowercase) head sha"
{ [[ "$POLL_SECONDS" =~ ^[0-9]+$ ]] && [[ "$MAX_POLLS" =~ ^[1-9][0-9]*$ ]]; } \
  || stop 2 "OWNER_MERGE_POLL_SECONDS and OWNER_MERGE_MAX_POLLS must be whole numbers"
CONTEXTS="$HERE/owner-ops/required-contexts-$REPO.txt"
[ -f "$CONTEXTS" ] || stop 2 "no $CONTEXTS: record $ORG/$REPO's required contexts first (none is still a file)"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json' >/dev/null 2>&1; then PY=$c; break; fi
done
[ -n "$PY" ] || stop 2 "no working python3 or python"
py() { "$PY" "$@" | tr -d '\r'; } # Windows Python ends lines with CRLF

WORK=$(mktemp -d) || stop 2 "mktemp failed"
NAME=""   # the live owner token's name, set BEFORE the mint so the trap can always delete it
TOKEN=""  # its value: never printed, never an argument
DB_POD=""
on_exit() {
  local rc=$?
  # A second INT/TERM/HUP must not abort the revocation (its kubectl children inherit the ignore).
  trap '' INT TERM HUP
  revoke || rc=5
  rm -rf "$WORK"
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

kube() { kubectl --context "$KCTX" "$@"; }

# get PATH OUT: a routine read as workstation-bot (gitea-api.sh refuses any other identity); the body -> OUT.
get() {
  local r
  if ! r=$(bash "$HERE/gitea-api.sh" GET "$1" 2>&1); then
    log "read failed: GET $1: $(printf '%s\n' "$r" | head -1)"
    return 1
  fi
  printf '%s\n' "$r" | sed 1d > "$2"
}

# get_pages PATH OUT: every page (limit=50) of a list endpoint until an empty one, one JSON document per line.
get_pages() {
  local p n
  : > "$2"
  for p in $(seq 1 40); do
    get "$1?limit=50&page=$p" "$WORK/page" || return 1
    n=$(py -c 'import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(len(d if isinstance(d, list) else (d.get("statuses") or [])))' "$WORK/page") || return 1
    [ "$n" -gt 0 ] || return 0
    { tr -d '\r\n' < "$WORK/page"; echo; } >> "$2"
  done
  log "more than 40 pages at $1"
  return 1
}

# The gate's judgement, from the files the reads above saved. One verdict line:
#   OK <summary> | WAIT <summary> | REFUSE <reason> | PIN <run id> <owner-ack status ids> <summary>
# PIN (pin mode only): everything else passed, owner-ack failed (and ci-gate, which aggregates it).
cat > "$WORK/gate.py" <<'PY'
import fnmatch, json, re, sys

BOTS = ("reviewer-claude", "reviewer-codex")
ACK = "CI / owner-ack*"
PIN_TOLERATED = (ACK, "CI / ci-gate*")


def verdict(word, text):
    print(f"{word} {text}")
    sys.exit(0)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def docs(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def required(path):
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f.read().replace("\r", "").split("\n")]
    return [line for line in lines if line and not line.startswith("#")]


def matches(ctx, patterns):
    return any(fnmatch.fnmatchcase(ctx, p) for p in patterns)


mode = sys.argv[1]
req = required(sys.argv[2])
if mode == "drift":
    b = load(sys.argv[3])
    live = (b.get("status_check_contexts") or []) if b.get("enable_status_check") else []
    lacking = [c for c in live if c not in req]
    if lacking:
        verdict("REFUSE", "the live protection of main requires context(s) the contexts file lacks: "
                + ", ".join(lacking) + " (update scripts/owner-ops/ first)")
    verdict("OK", f"main requires {len(live)} context(s), all in the contexts file")

head, pr, reviews, statuses = sys.argv[3], load(sys.argv[4]), docs(sys.argv[5]), docs(sys.argv[6])

if pr.get("merged"):
    verdict("REFUSE", "the PR is already merged")
if pr.get("state") != "open":
    verdict("REFUSE", f"the PR is {pr.get('state')}")
# Gitea sets draft for its own prefixes (WIP:, [WIP]); any title starting with the word WIP is refused too.
if pr.get("draft") or re.match(r"\s*[\[(]?\s*WIP\b", pr.get("title") or "", re.I):
    verdict("REFUSE", "the PR is a draft or WIP")
if (pr.get("base") or {}).get("ref") != "main":
    verdict("REFUSE", f"the PR targets {(pr.get('base') or {}).get('ref')}, not main")
cur = (pr.get("head") or {}).get("sha")
if cur != head:
    verdict("REFUSE", f"head moved: {cur} != {head}")

last = {}
for page in reviews:
    for r in page:
        login = (r.get("user") or {}).get("login")
        if login in BOTS and r.get("commit_id") == head and not r.get("dismissed"):
            if login not in last or (r.get("id") or 0) > (last[login].get("id") or 0):
                last[login] = r
rv = " ".join(f"{b}={last[b]['state'] if b in last else 'none'}" for b in BOTS)
if any(b not in last or last[b].get("state") != "APPROVED" for b in BOTS):
    verdict("REFUSE", f"both bots must approve the head: {rv}")

latest = {}
for page in statuses:
    for s in page.get("statuses") or []:
        c = s.get("context")
        if c not in latest or (s.get("id") or 0) > (latest[c].get("id") or 0):
            latest[c] = s
klass = {c: {"success": "ok", "skipped": "ok", "pending": "pending"}.get(s.get("status"), "failed")
         for c, s in latest.items()}


def worst(ctxs):
    states = [klass[c] for c in ctxs]
    if not states:
        return "missing"
    return next(w for w in ("failed", "pending", "ok") if w in states)


failed = sorted(c for c, k in klass.items() if k == "failed")
pending = sorted(c for c, k in klass.items() if k == "pending")
missing = [p for p in req if not any(fnmatch.fnmatchcase(c, p) for c in latest)]
acks = sorted(c for c in latest if fnmatch.fnmatchcase(c, ACK))
ack = worst(acks) if acks else "absent"
summary = (f"{rv}; required: " + (", ".join(f"{p}={worst([c for c in latest if fnmatch.fnmatchcase(c, p)])}"
                                             for p in req) or "none")
           + f"; owner-ack={ack}; {len(latest)} contexts")

if mode == "pin" and ack in ("failed", "pending"):
    other = [c for c in failed if not matches(c, PIN_TOLERATED)]
    if other:
        verdict("REFUSE", "failed: " + ", ".join(other) + " | " + summary)
    if pending or missing:
        verdict("WAIT", summary)
    runs = sorted({m.group(1) for c in acks
                   for m in [re.search(r"/actions/runs/(\d+)/", latest[c].get("target_url") or "")] if m})
    if len(runs) != 1:
        verdict("REFUSE", f"cannot tell the owner-ack run from its target_url ({runs}) | {summary}")
    verdict("PIN", f"{runs[0]} {','.join(str(latest[c].get('id')) for c in acks)} {summary}")

if failed:
    hint = " (owner-ack failed: pass --approve-pin if the acknowledgement is intended)" \
        if ack == "failed" and mode != "pin" else ""
    verdict("REFUSE", "failed: " + ", ".join(failed) + hint + " | " + summary)
if pending or missing:
    verdict("WAIT", summary + (" | missing: " + ", ".join(missing) if missing else ""))
if mode == "pin" and ack == "absent":
    verdict("REFUSE", "--approve-pin given, but the head has no CI / owner-ack context | " + summary)
verdict("OK", summary)
PY

# gate MODE: one credential-free pass; sets VERDICT (READFAIL when a read failed, so no verdict was reached).
gate() {
  if get "/repos/$ORG/$REPO/pulls/$PR" "$WORK/pr.json" \
    && get_pages "/repos/$ORG/$REPO/pulls/$PR/reviews" "$WORK/reviews.jsonl" \
    && get_pages "/repos/$ORG/$REPO/commits/$HEAD_SHA/status" "$WORK/status.jsonl"; then
    VERDICT=$(py "$WORK/gate.py" "$1" "$CONTEXTS" "$HEAD_SHA" "$WORK/pr.json" "$WORK/reviews.jsonl" \
      "$WORK/status.jsonl") || VERDICT="REFUSE the gate evaluation failed"
  else
    VERDICT="READFAIL a gate read failed"
  fi
}

# wait_gate MODE [STALE_ACK_IDS]: run the gate until it passes, holding NO owner token. After a pin, PIN with the
# same owner-ack status ids is the old failure not yet re-run (keep waiting); with new ids it failed again. A
# failed read is retried on the next poll (no credential is held), up to 3 in a row.
wait_gate() {
  local mode=$1 stale=${2:-} i readfails=0
  for i in $(seq 1 "$MAX_POLLS"); do
    gate "$mode"
    case $VERDICT in
      READFAIL\ *)
        readfails=$((readfails + 1))
        [ "$readfails" -lt 3 ] || stop 3 "$readfails gate reads in a row failed; nothing (more) minted"
        log "a gate read failed ($readfails in a row); retrying on the next poll" ;;
      OK\ *) return 0 ;;
      PIN\ *)
        [ -z "$stale" ] && return 0
        [ "$(printf '%s' "$VERDICT" | cut -d' ' -f3)" = "$stale" ] \
          || stop 3 "owner-ack failed again after the approve-pin: ${VERDICT#PIN }" ;;
      WAIT\ *) ;;
      *) stop 3 "${VERDICT#REFUSE }" ;;
    esac
    [[ "$VERDICT" == READFAIL\ * ]] || readfails=0
    [ $((i % 10)) -eq 1 ] && log "waiting: ${VERDICT#* }"
    [ "$i" -lt "$MAX_POLLS" ] && sleep "$POLL_SECONDS"
  done
  stop 3 "not green after $MAX_POLLS polls: ${VERDICT#* }"
}

# mint PURPOSE: a fresh owner token into TOKEN. NAME is set FIRST, so the trap deletes the token by name even if
# the mint is interrupted or prints something that is not a token. The pods are looked up per mint (failover).
mint() {
  local gitea_pod
  [ -n "$EXECUTE" ] || stop 4 "internal error: mint without --execute"
  gitea_pod=$(kube -n gitea get pod -l app.kubernetes.io/name=gitea,app.kubernetes.io/instance=gitea \
    --field-selector=status.phase=Running -o name 2>/dev/null | head -1)
  DB_POD=$(kube -n databases get pod -l cnpg.io/cluster=infra-pg,cnpg.io/instanceRole=primary -o name 2>/dev/null \
    | head -1)
  gitea_pod=${gitea_pod#pod/}; DB_POD=${DB_POD#pod/}
  if [ -z "$gitea_pod" ] || [ -z "$DB_POD" ]; then
    log "cannot find the Gitea pod or the infra-pg primary; nothing minted"
    return 1
  fi
  NAME="owner-op-$REPO-$PR-$1-$(date -u +%Y%m%d%H%M%S)"
  TOKEN=$(kube -n gitea exec "$gitea_pod" -c gitea -- gitea admin user generate-access-token --username "$OWNER" \
    --token-name "$NAME" --scopes write:issue,write:repository --raw 2>/dev/null | tr -d '\r\n')
  if ! [[ "$TOKEN" =~ ^[0-9a-f]{40}$ ]]; then
    TOKEN=""
    log "owner token $NAME: the mint did not return a token"
    return 1
  fi
  log "owner token $NAME minted"
}

# owner_call METHOD PATH [BODY_FILE]: the HTTP status on stdout ("000" on a transport failure), the body in
# $WORK/resp. Same transport rules as gitea-api.sh: -q FIRST (no ~/.curlrc can add tracing or URLs), https only,
# no redirects, the header on stdin.
owner_call() {
  local code args=(-q --proto =https --max-redirs 0 -sS -o "$WORK/resp" -w '%{http_code}' -X "$1" -H @-)
  [ -n "$EXECUTE" ] || stop 4 "internal error: owner call without --execute"
  [ -n "${3:-}" ] && args+=(-H 'Content-Type: application/json' --data-binary "@$3")
  : > "$WORK/resp"
  code=$(printf 'Authorization: token %s\n' "$TOKEN" | curl "${args[@]}" "$ORIGIN$2") || code=000
  echo "$code"
}

psql_gitea() { kube -n databases exec -i "$DB_POD" -c postgres -- psql -U postgres -d gitea -tA -v ON_ERROR_STOP=1 -f -; }

# revoke: delete the live owner token by NAME, then prove it dead: the database no longer lists it and (when the
# value is known) its next use answers 401. Returns 1, loudly, when either proof fails.
revoke() {
  [ -n "$NAME" ] || return 0
  local name=$NAME code="(no token value)" left bad=""
  printf "delete from access_token where uid=(select id from \"user\" where name='%s') and name='%s';\n" \
    "$OWNER" "$name" | psql_gitea >/dev/null 2>&1
  left=$(printf "select coalesce(string_agg(name, ',' order by id), '') from access_token where uid=(select id from \"user\" where name='%s');\n" \
    "$OWNER" | psql_gitea 2>/dev/null | tr -d '\r') || left="<unreadable>"
  if [ -n "$TOKEN" ]; then
    code=$(owner_call GET /user)
    [ "$code" = 401 ] || bad="its next use answered HTTP $code"
  fi
  NAME=""; TOKEN=""
  case ",$left," in *",$name,"*|",<unreadable>,") bad="${bad:+$bad; }the database still lists it (or could not be read)" ;; esac
  log "owner token $name deleted: next use -> HTTP $code; $OWNER tokens now: ${left:-none}"
  if [ -n "$bad" ]; then
    echo "[$(ts)] REVOCATION NOT CONFIRMED for owner token $name: $bad. Delete it now on the infra-pg primary" \
      "(delete from access_token where name='$name';) and check GiteaOwnerTokenStanding." >&2
    return 1
  fi
}

MODE=strict; [ -n "$PIN" ] && MODE=pin
if [ -n "$EXECUTE" ]; then RUNKIND="EXECUTE"; else RUNKIND="DRY RUN"; fi
log "$RUNKIND: gate for $ORG/$REPO#$PR at $HEAD_SHA${PIN:+ (--approve-pin)}, with no owner credential"
get "/repos/$ORG/$REPO/branches/main" "$WORK/branch.json" || stop 3 "cannot read the live protection of main"
VERDICT=$(py "$WORK/gate.py" drift "$CONTEXTS" "$WORK/branch.json") || VERDICT="REFUSE the drift check failed"
case $VERDICT in OK\ *) log "protection: ${VERDICT#OK }" ;; *) stop 3 "${VERDICT#REFUSE }" ;; esac

# The dry run: one gate pass and the plan. Nothing below this block runs without --execute.
if [ -z "$EXECUTE" ]; then
  gate "$MODE"
  merge_plan="mint an owner token, merge $HEAD_SHA (plain merge; force_merge only after Gitea's \"Changed protected files\" refusal), delete the token and prove it answers 401"
  case $VERDICT in
    OK\ *)
      log "DRY RUN: the gate passes: ${VERDICT#OK }"
      [ -n "$PIN" ] && log "DRY RUN: owner-ack is already green: no approve-pin needed"
      log "DRY RUN: would merge now: $merge_plan" ;;
    PIN\ *)
      read -r _ RUN _ <<< "$VERDICT"
      log "DRY RUN: the gate passes except owner-ack: ${VERDICT#PIN * * }"
      log "DRY RUN: would mint an owner token, post 'approve-pin $HEAD_SHA', re-run the failed jobs of run $RUN, and delete the token"
      log "DRY RUN: would then wait with no token until every context passes, and merge: $merge_plan" ;;
    WAIT\ *)
      log "DRY RUN: would wait (up to $MAX_POLLS polls of ${POLL_SECONDS}s, no credential) for: ${VERDICT#WAIT }"
      exit 6 ;;
    READFAIL\ *) stop 3 "${VERDICT#READFAIL }" ;;
    *) stop 3 "${VERDICT#REFUSE }" ;;
  esac
  log "DRY RUN: nothing minted, posted or merged. Re-run with --execute to do it."
  exit 0
fi

if [ -n "$PIN" ]; then
  wait_gate pin
  if [[ "$VERDICT" == PIN\ * ]]; then
    read -r _ RUN ACK_IDS _ <<< "$VERDICT"
    log "gate passed except owner-ack (run $RUN): ${VERDICT#PIN * * }"
    mint pin || stop 4 "owner token mint failed"
    printf '{"body":"approve-pin %s"}' "$HEAD_SHA" > "$WORK/pin.json"
    code=$(owner_call POST "/repos/$ORG/$REPO/issues/$PR/comments" "$WORK/pin.json")
    [ "$code" = 201 ] || stop 4 "the approve-pin comment was refused: HTTP $code $(head -c 200 "$WORK/resp")"
    log "approve-pin $HEAD_SHA posted"
    code=$(owner_call POST "/repos/$ORG/$REPO/actions/runs/$RUN/rerun-failed-jobs")
    case $code in 2??) ;; *) stop 4 "the re-run of run $RUN was refused: HTTP $code $(head -c 200 "$WORK/resp")" ;; esac
    log "re-ran the failed jobs of run $RUN -> HTTP $code"
    revoke || exit 5
    wait_gate pin "$ACK_IDS"
  else
    log "owner-ack is already green: no approve-pin needed"
  fi
else
  wait_gate strict
fi
log "gate passed: ${VERDICT#OK }"

mint merge || stop 4 "owner token mint failed"
printf '{"Do":"merge","head_commit_id":"%s"}' "$HEAD_SHA" > "$WORK/merge.json"
code=$(owner_call POST "/repos/$ORG/$REPO/pulls/$PR/merge" "$WORK/merge.json")
log "plain merge -> HTTP $code $(head -c 200 "$WORK/resp")"
if [ "$code" = 405 ] && grep -q 'Changed protected files' "$WORK/resp"; then
  printf '{"Do":"merge","head_commit_id":"%s","force_merge":true}' "$HEAD_SHA" > "$WORK/force.json"
  code=$(owner_call POST "/repos/$ORG/$REPO/pulls/$PR/merge" "$WORK/force.json")
  log "force_merge after Gitea's protected-files refusal -> HTTP $code $(head -c 200 "$WORK/resp")"
fi
revoke || exit 5
[ "$code" = 200 ] || stop 4 "the merge was refused: HTTP $code"

get "/repos/$ORG/$REPO/pulls/$PR" "$WORK/pr.json" || stop 4 "merged, but the PR could not be read back"
M=$(py -c 'import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
print(p.get("merge_commit_sha") or "" if p.get("merged") else "")' "$WORK/pr.json")
[ -n "$M" ] || stop 4 "the merge answered 200 but the PR does not read as merged"
log "MERGED $ORG/$REPO#$PR as $M"
