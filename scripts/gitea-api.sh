#!/usr/bin/env bash
# gitea-api.sh METHOD PATH [BODY_FILE] - call the Gitea API as the workstation's routine identity.
#
# Auth-hardening plan B3 (owner decision D10): the workstation holds ONE non-admin Gitea credential,
# workstation-bot, in the git credential helper. This script is the supported way to use it:
#   - the origin is fixed (https://git.chifor.me/api/v1); PATH is an API path validated against a strict
#     character set, so it cannot name another host, inject shell or carry spaces;
#   - the token is read from `git credential fill` inside this process and handed to curl on STDIN
#     (`-H @-`), never as an argument, so it is not visible in the process list; no -v, no tracing;
#   - no redirects (`--max-redirs 0`, `--proto =https`), so the header cannot be replayed elsewhere,
#     and `-q` first, so no ~/.curlrc can add tracing or URLs; the credential lookup never prompts;
#   - before the call, GET /user must answer login=workstation-bot with is_admin=false, read from the
#     parsed JSON's top level (python3 or python; without one it refuses); any other identity (an owner
#     or admin token in the helper) is refused, and nothing else is sent.
# Run marker (worker-identities plan, Phase 1 item 4): every session on the workstation shares this one
# credential, so a comment or review names the RUN that wrote it. With AILAB_RUN_ID set, a comment or
# review write (POST issues/N/comments, PATCH issues/comments/ID, POST pulls/N/reviews[/ID]) gets
# `\n<!-- worker: workstation run: <id> -->` appended to its JSON `body`. Unset or empty: no marker, never
# a guessed id. Sent as the ORIGINAL file, byte for byte: every other endpoint, a body that already
# carries a worker marker ANYWHERE (an edit of a marked comment keeps its one original attribution), and a
# command body - one whose whole text is `approve-pin <sha>`, which the owner-ack gate counts only as a
# one-line body.
# Prints `HTTP <status>` and the response body; exits 1 on a 4xx/5xx, 2 on bad input, 3 when no
# credential resolves, 4 when the identity guard refuses, 5 when curl fails at the transport level
# (DNS, TLS, connection), whatever curl's own exit code was.
#   scripts/gitea-api.sh GET /repos/cchifor/ailab/pulls?state=open
#   scripts/gitea-api.sh POST /repos/cchifor/ailab/issues/1/comments body.json
set -euo pipefail

ORIGIN="https://git.chifor.me/api/v1"
EXPECTED_USER="workstation-bot"
PATH_RE='^/[A-Za-z0-9/_.,-]+(\?[A-Za-z0-9=&_.,-]*)?$'

die() { echo "gitea-api: $2" >&2; exit "$1"; }

[ $# -ge 2 ] && [ $# -le 3 ] || die 2 "usage: gitea-api.sh METHOD PATH [BODY_FILE]"
method=$1
path=$2
body=${3:-}
case "$method" in GET|POST|PUT|PATCH|DELETE) ;; *) die 2 "method must be GET, POST, PUT, PATCH or DELETE";; esac
[[ "$path" =~ $PATH_RE ]] || die 2 "PATH must be an API path such as /repos/cchifor/ailab (letters, digits, / _ . , - and a simple query)"
case "$path" in *//*|*..*) die 2 "PATH must not contain // or ..";; esac
[ -z "$body" ] || [ -f "$body" ] || die 2 "body file not found: $body"

# mark_body FILE OUT RUN_ID: 0 = OUT holds FILE's JSON with the marker appended to `body`; 10 = send
# FILE unchanged (no string `body`, a command body, or already marked); anything else = refuse (not a
# JSON object, or no working Python). Only a payload that is marked is re-serialised.
mark_body() {
  local py
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import json' >/dev/null 2>&1; then
      "$py" -c 'import json, re, sys
src, dst, run = sys.argv[1:4]
try:
    with open(src, "rb") as f:
        d = json.loads(f.read().decode("utf-8"))
except Exception:
    sys.exit(2)
if not isinstance(d, dict):
    sys.exit(2)
b = d.get("body")
if not isinstance(b, str) or re.fullmatch(r"approve-pin [0-9A-Fa-f]{7,40}", b.strip()):
    sys.exit(10)
if re.search(r"<!-- worker: [^>]* -->", b):
    sys.exit(10)
d["body"] = b + "\n<!-- worker: workstation run: " + run + " -->"
with open(dst, "w", encoding="utf-8") as f:
    json.dump(d, f)' "$@"
      return
    fi
  done
  return 1
}

send=$body
marked=""
MARK_RE='^/repos/[^/]+/[^/]+/(issues/[0-9]+/comments|issues/comments/[0-9]+|pulls/[0-9]+/reviews(/[0-9]+)?)$'
RUN_ID_RE='^[A-Za-z0-9._:-]{1,128}$'
if [ -n "${AILAB_RUN_ID:-}" ] && [ -n "$body" ] && [[ "$method" =~ ^(POST|PATCH)$ ]] \
    && [[ "${path%%\?*}" =~ $MARK_RE ]]; then
  [[ "$AILAB_RUN_ID" =~ $RUN_ID_RE ]] || die 2 "AILAB_RUN_ID must match $RUN_ID_RE (it is written into an HTML comment)"
  marked=$(mktemp)
  trap 'rm -f "$marked"' EXIT
  rc=0
  mark_body "$body" "$marked" "$AILAB_RUN_ID" || rc=$?
  case "$rc" in
    0) send=$marked;;
    10) ;;
    *) die 2 "cannot add the run marker: the body file is not a JSON object (or no Python): $body";;
  esac
fi

# Never prompt: a missing credential is an error here, not a login dialog. A failed lookup must reach
# the exit-3 diagnostic rather than end the script silently under `set -e`.
token=$(printf 'protocol=https\nhost=git.chifor.me\n\n' \
  | GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git credential fill 2>/dev/null \
  | sed -n 's/^password=//p') || token=""
[ -n "$token" ] || die 3 "no credential for git.chifor.me in the git credential helper"

out=$(mktemp)
trap 'rm -f "$out" ${marked:+"$marked"}' EXIT

# call METHOD PATH [BODY_FILE] -> prints the status code; the body lands in $out.
# `-q` must stay FIRST: it stops curl reading ~/.curlrc, which could add -v/--trace (printing the
# header) or extra URLs (sending it elsewhere).
call() {
  local args=(-q --proto =https --max-redirs 0 -sS -o "$out" -w '%{http_code}' -X "$1" -H @-)
  if [ -n "${3:-}" ]; then
    args+=(-H 'Content-Type: application/json' --data-binary "@$3")
  fi
  printf 'Authorization: token %s\n' "$token" | curl "${args[@]}" "$ORIGIN$2"
}

# identity_ok FILE: parse the /user JSON and require the TOP-LEVEL login to be exactly $EXPECTED_USER
# and is_admin to be the boolean false. Text matching would accept the same fields nested deeper.
# Fails closed: invalid JSON, a non-object, or no working Python interpreter all refuse.
identity_ok() {
  local py
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import json' >/dev/null 2>&1; then
      "$py" -c 'import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(d, dict) and d.get("login") == sys.argv[2] and d.get("is_admin") is False else 1)' \
        "$1" "$EXPECTED_USER"
      return
    fi
  done
  return 1
}

code=$(call GET /user) || die 5 "curl transport failure on GET /user"
if [ "$code" != 200 ] || ! identity_ok "$out"; then
  die 4 "refusing: the credential is not the non-admin $EXPECTED_USER (GET /user -> HTTP $code)"
fi

code=$(call "$method" "$path" "$send") || die 5 "curl transport failure on $method $path"
echo "HTTP $code"
cat "$out"
echo
case "$code" in 2??|3??) exit 0;; *) exit 1;; esac
