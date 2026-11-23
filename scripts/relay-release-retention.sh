#!/usr/bin/env bash
# relay-release-retention.sh [--keep-newest N] [--keep vX.Y.Z]... [--execute PLAN-SHA256]
# Owner-approved retention of Relay's large release bundles (docs/runbooks/relay.md, "Release asset retention").
#   1. Fetch every release of cchifor/relay (all pages) through scripts/gitea-api.sh.
#   2. Plan with scripts/relay_release_retention.py and print it, with its PLAN-SHA256. Nothing is deleted.
#   3. Only with --execute <sha>, and only if the freshly computed plan has exactly that sha256 (the list the owner
#      approved), delete each planned asset, then re-read every touched release and confirm the asset is gone.
# Plain bash on purpose: gitea-api.sh resolves its credential only in Git Bash, not from a Python subprocess, and it
# returns an empty body when MSYS_NO_PATHCONV is set at all, so this script never sets it.
set -uo pipefail
unset MSYS_NO_PATHCONV
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${RELAY_RETENTION_API:-$ROOT/scripts/gitea-api.sh}"   # the override is for scripts/tests only
REPO="cchifor/relay"
PY=python3; command -v python3 >/dev/null 2>&1 && python3 -c 'import json' >/dev/null 2>&1 || PY=python
args=(); execute=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-newest|--keep) [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }; args+=("$1" "$2"); shift 2 ;;
    --execute) [ $# -ge 2 ] || { echo "--execute needs the PLAN-SHA256" >&2; exit 2; }; execute="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$execute" in ''|[0-9a-f]*) ;; *) echo "--execute takes a sha256" >&2; exit 2 ;; esac
[ -z "$execute" ] || [ "${#execute}" = 64 ] || { echo "--execute takes a full 64-hex sha256" >&2; exit 2; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
# GET one page -> its JSON array on stdout; a non-200 or an empty/unparsable body is an error, never "no releases".
get_page() {
  local out; out="$(bash "$API" GET "/repos/$REPO/releases?limit=50&page=$1" 2>&1)" || { echo "$out" >&2; return 1; }
  # The status line by expansion, not `printf | head -1`: on a large body head closes the pipe, printf gets SIGPIPE
  # and pipefail turns a 200 into a failure (seen on the first real run).
  [ "${out%%$'\n'*}" = "HTTP 200" ] || { printf '%s\n' "${out:0:300}" >&2; return 1; }
  printf '%s\n' "$out" | sed 1d
}
page=1; : > "$W/pages"
while :; do
  body="$(get_page "$page")" || { echo "STOP: could not read releases page $page" >&2; exit 1; }
  n="$(printf '%s' "$body" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null)" \
    || { echo "STOP: page $page is not a JSON array (empty body?)" >&2; exit 1; }
  printf '%s\n' "$body" >> "$W/pages"
  [ "$n" -lt 50 ] && break
  page=$((page + 1)); [ "$page" -le 40 ] || { echo "STOP: more than 40 pages" >&2; exit 1; }
done
"$PY" -c '
import json, sys
rel = []
for line in open(sys.argv[1], encoding="utf-8").read().split("\n"):
    line = line.strip()
    if line:
        rel.extend(json.loads(line))
ids = [r["id"] for r in rel]
assert len(ids) == len(set(ids)), "a release appeared on two pages"
json.dump(rel, open(sys.argv[2], "w", encoding="utf-8"))
print("releases read:", len(rel))' "$W/pages" "$W/releases.json" || { echo "STOP: could not assemble the release list" >&2; exit 1; }

"$PY" "$ROOT/scripts/relay_release_retention.py" plan --releases "$W/releases.json" --json-out "$W/plan.json" "${args[@]}" \
  | tee "$W/plan.txt" || exit 1
sha="$(sed -n 's/^PLAN-SHA256 //p' "$W/plan.txt")"
[ -n "$execute" ] || { echo "DRY RUN: nothing deleted. To delete exactly this list: $0 ${args[*]} --execute $sha"; exit 0; }
[ "$sha" = "$execute" ] || { echo "STOP: the plan changed (now $sha, approved $execute); nothing deleted" >&2; exit 1; }

echo "EXECUTING plan $sha"
fail=0
while IFS=$'\t' read -r rid aid name; do
  out="$(bash "$API" DELETE "/repos/$REPO/releases/$rid/assets/$aid" 2>&1)"
  code="${out%%$'\n'*}"
  case "$code" in 'HTTP 204'|'HTTP 200') echo "deleted $name ($aid)";; *) echo "FAILED $name ($aid): $code" >&2; fail=1;; esac
done < <("$PY" -c '
import json, sys
for i in json.load(open(sys.argv[1], encoding="utf-8")):
    print("%d\t%d\t%s" % (i["release_id"], i["asset_id"], i["name"]))' "$W/plan.json")

# Re-read each touched release: none of the planned asset ids may remain; everything else stays.
"$PY" -c '
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(sorted({str(i["release_id"]) for i in plan})))' "$W/plan.json" > "$W/rids"
for rid in $(cat "$W/rids"); do
  out="$(bash "$API" GET "/repos/$REPO/releases/$rid" 2>&1)"
  [ "${out%%$'\n'*}" = "HTTP 200" ] || { echo "STOP: cannot re-read release $rid" >&2; fail=1; continue; }
  printf '%s\n' "$out" | sed 1d > "$W/rel-$rid.json"
  "$PY" -c '
import json, sys
rel = json.load(open(sys.argv[1], encoding="utf-8")); plan = json.load(open(sys.argv[2], encoding="utf-8"))
left = {a["id"] for a in rel.get("assets") or []} & {i["asset_id"] for i in plan if i["release_id"] == rel["id"]}
print("release %s (%s): %d assets remain, %d planned still present" % (rel["id"], rel["tag_name"], len(rel.get("assets") or []), len(left)))
sys.exit(1 if left else 0)' "$W/rel-$rid.json" "$W/plan.json" || fail=1
done
[ "$fail" = 0 ] && echo "DONE: plan $sha executed and verified" || { echo "INCOMPLETE: see the lines above" >&2; exit 1; }
