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
#   - before the call, GET /user must answer login=workstation-bot with is_admin=false; any other
#     identity (an owner or admin token in the helper) is refused, and nothing else is sent.
# Prints `HTTP <status>` and the response body; exits 1 on a 4xx/5xx, 2 on bad input, 3 when no
# credential resolves, 4 when the identity guard refuses.
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

# Never prompt: a missing credential is an error here, not a login dialog. A failed lookup must reach
# the exit-3 diagnostic rather than end the script silently under `set -e`.
token=$(printf 'protocol=https\nhost=git.chifor.me\n\n' \
  | GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git credential fill 2>/dev/null \
  | sed -n 's/^password=//p') || token=""
[ -n "$token" ] || die 3 "no credential for git.chifor.me in the git credential helper"

out=$(mktemp)
trap 'rm -f "$out"' EXIT

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

code=$(call GET /user)
if [ "$code" != 200 ] \
  || ! grep -q "\"login\":\"$EXPECTED_USER\"" "$out" \
  || ! grep -q '"is_admin":false' "$out"; then
  die 4 "refusing: the credential is not the non-admin $EXPECTED_USER (GET /user -> HTTP $code)"
fi

code=$(call "$method" "$path" "$body")
echo "HTTP $code"
cat "$out"
echo
case "$code" in 2??|3??) exit 0;; *) exit 1;; esac
