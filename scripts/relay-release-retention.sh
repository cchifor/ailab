#!/usr/bin/env bash
# relay-release-retention.sh [--keep-newest N>=5] [--keep vX.Y.Z]... [--execute PLAN-SHA256]
# Owner-approved retention of Relay's large release bundles (docs/runbooks/relay.md, "Release asset retention").
#   1. Fetch every release of cchifor/relay through scripts/gitea-api.sh, page after page until an EMPTY page.
#   2. Plan with scripts/relay_release_retention.py and print it: every asset, by ids and JSON-escaped name, and the
#      PLAN-SHA256. Nothing is deleted.
#   3. Only with --execute <sha>, and only if the freshly computed plan has exactly that sha256 (the list the owner
#      approved), delete each planned asset by its numeric ids, then re-read every touched release and require its
#      assets to be exactly the pre-execution inventory minus the plan.
# Plain bash on purpose: gitea-api.sh resolves its credential only in Git Bash, not from a Python subprocess, and it
# returns an empty body when MSYS_NO_PATHCONV is set at all, so this script never sets it.
set -uo pipefail
unset MSYS_NO_PATHCONV
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${RELAY_RETENTION_API:-$ROOT/scripts/gitea-api.sh}"   # the override is for scripts/tests only
REPO="cchifor/relay"
PLANNER="$ROOT/scripts/relay_release_retention.py"
PY=python3; command -v python3 >/dev/null 2>&1 && python3 -c 'import json' >/dev/null 2>&1 || PY=python
args=(); execute=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-newest|--keep) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }; args+=("$1" "$2"); shift 2 ;;
    --execute) [ $# -ge 2 ] || { echo "--execute needs the PLAN-SHA256" >&2; exit 2; }; execute="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [ -n "$execute" ] && ! [[ "$execute" =~ ^[0-9a-f]{64}$ ]]; then echo "--execute takes a full 64-hex sha256" >&2; exit 2; fi

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
# The status line by expansion, not `printf | head -1`: on a large body head closes the pipe, printf gets SIGPIPE and
# pipefail turns a 200 into a failure (seen on the first real run).
status() { printf '%s' "${1%%$'\n'*}"; }
body() { case "$1" in *$'\n'*) printf '%s\n' "${1#*$'\n'}";; *) printf '';; esac; }

# 1. every page until an empty one: a server page cap below the requested limit must not end the listing early
page=1; : > "$W/pages"
while :; do
  out="$(bash "$API" GET "/repos/$REPO/releases?limit=50&page=$page" 2>&1)" || { echo "STOP: page $page: $(status "$out")" >&2; exit 1; }
  [ "$(status "$out")" = "HTTP 200" ] || { echo "STOP: page $page: $(status "$out")" >&2; exit 1; }
  n="$(body "$out" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d, list); print(len(d))' 2>/dev/null)" \
    || { echo "STOP: page $page is not a JSON array (empty body?)" >&2; exit 1; }
  [ "$n" -gt 0 ] || break
  body "$out" | tr -d '\n' >> "$W/pages"; printf '\n' >> "$W/pages"
  page=$((page + 1)); [ "$page" -le 60 ] || { echo "STOP: more than 60 pages" >&2; exit 1; }
done
"$PY" - "$W/pages" "$W/releases.json" <<'PY' || { echo "STOP: could not assemble the release list" >&2; exit 1; }
import json, sys
rel = []
for line in open(sys.argv[1], encoding="utf-8"):
    if line.strip():
        rel.extend(json.loads(line))
ids = [r["id"] for r in rel]
assert len(ids) == len(set(ids)), "a release appeared on two pages"
json.dump(rel, open(sys.argv[2], "w", encoding="utf-8"))
print("releases read:", len(rel))
PY

# 2. the plan
"$PY" "$PLANNER" plan --releases "$W/releases.json" --json-out "$W/plan.json" "${args[@]}" | tee "$W/plan.txt"
[ "${PIPESTATUS[0]}" = 0 ] || { echo "STOP: no plan" >&2; exit 1; }
sha="$(sed -n 's/^PLAN-SHA256 //p' "$W/plan.txt")"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || { echo "STOP: the planner printed no PLAN-SHA256" >&2; exit 1; }
[ -n "$execute" ] || { echo "DRY RUN: nothing deleted. To delete exactly this list: $0 ${args[*]} --execute $sha"; exit 0; }
[ "$sha" = "$execute" ] || { echo "STOP: the plan changed (now $sha, approved $execute); nothing deleted" >&2; exit 1; }

# 3. delete by numeric ids only: an asset name never reaches this loop, so no name can inject a line
echo "EXECUTING plan $sha"
"$PY" -c '
import json, sys
for i in json.load(open(sys.argv[1], encoding="utf-8")):
    print("%d\t%d" % (int(i["release_id"]), int(i["asset_id"])))' "$W/plan.json" > "$W/ids" || { echo "STOP: cannot read the plan ids" >&2; exit 1; }
planned="$(grep -c . "$W/ids")"
fail=0; done_n=0
while IFS=$'\t' read -r rid aid; do
  [[ "$rid" =~ ^[0-9]+$ && "$aid" =~ ^[0-9]+$ ]] || { echo "STOP: a non-numeric id in the plan" >&2; exit 1; }
  out="$(bash "$API" DELETE "/repos/$REPO/releases/$rid/assets/$aid" 2>&1)"
  case "$(status "$out")" in
    'HTTP 204'|'HTTP 200') done_n=$((done_n + 1)); echo "deleted release $rid asset $aid" ;;
    *) echo "FAILED release $rid asset $aid: $(status "$out")" >&2; fail=1 ;;
  esac
done < "$W/ids"
[ "$done_n" = "$planned" ] || { echo "INCOMPLETE: $done_n of $planned deletions succeeded" >&2; fail=1; }

# 4. verify each touched release against the pre-execution inventory
for rid in $(cut -f1 "$W/ids" | sort -un); do
  out="$(bash "$API" GET "/repos/$REPO/releases/$rid" 2>&1)"
  [ "$(status "$out")" = "HTTP 200" ] || { echo "VERIFY FAILED: cannot re-read release $rid: $(status "$out")" >&2; fail=1; continue; }
  body "$out" > "$W/after-$rid.json"
  "$PY" "$PLANNER" verify --before "$W/releases.json" --plan "$W/plan.json" --after "$W/after-$rid.json" || fail=1
done
[ "$fail" = 0 ] && echo "DONE: plan $sha executed and verified" || { echo "INCOMPLETE: see the lines above" >&2; exit 1; }
