#!/usr/bin/env bash
# openbao-revoke-slot-holder.sh ROLE - cut a dev-worker slot's PREVIOUS machine out of OpenBao at a
# re-slot (docs/runbooks/s2s-identity.md, "Re-slot"), BEFORE the replacement's SecretID is minted.
#
# Destroys EVERY SecretID of AppRole ROLE and revokes EVERY token the role issued. Destroying SecretIDs
# alone revokes nothing already issued: the role's tokens are periodic and renewable. These are the two
# sweeps the provision Job's RETIRED_SLOTS step runs (devworker-provision-job.yaml), for one role that
# stays. Run it while the role has no replacement yet: it spares nothing.
#
# FAIL CLOSED: every list, lookup, destroy and revoke is checked. Only the documented empty answers
# (`{}` or "No value found" on a listing, "invalid accessor" for a token that expired meanwhile) count as
# nothing-to-do; a denial, a sealed or unreachable vault or unparsable output exits 1, before anything is
# published. A run during an OpenBao outage proves nothing: repeat it once the vault is healthy. Prints
# accessors and counts only, never a token or SecretID value.
#
# Needs BAO_ADDR (plus BAO_CACERT / BAO_TLS_SERVER_NAME) as in openbao-dev-workers.md section (e), and
# cluster-admin kubectl for the break-glass token, which is passed to each bao call as an env prefix.
# Exit: 0 swept (the summary line says how much; a second run must report 0 and 0), 1 aborted, 2 bad usage.
set -uo pipefail

[ $# -eq 1 ] && [ -n "$1" ] || { echo "usage: $0 ROLE" >&2; exit 2; }
R=$1

fail() { echo "ABORT: $*" >&2; exit 1; }
TOK=$(kubectl --context admin@ai -n openbao get secret openbao-breakglass-token \
  -o jsonpath='{.data.root_token}' | base64 -d) && [ -n "$TOK" ] || fail "no break-glass token"
b() { BAO_TOKEN="$TOK" bao "$@"; }

# listing PATH: the JSON list on stdout, or nothing for a documented empty answer; anything else aborts.
# -format=json renders "no value found" as an EMPTY object and still exits non-zero, so ONLY a literal
# `{}` is empty: a denial or a sealed vault renders a non-empty object ({"errors": [...]}).
listing() {
  local out
  if out=$(b list -format=json "$1" 2>&1); then printf '%s' "$out"; return 0; fi
  [ "$(printf '%s' "$out" | tr -d '[:space:]')" = "{}" ] && return 0
  printf '%s' "$out" | grep -qi 'no value found' && return 0
  fail "bao list $1: $out"
}
words() { python3 -c 'import json, sys
t = sys.stdin.read().strip()
v = json.loads(t) if t else []
assert isinstance(v, list) and all(isinstance(x, str) for x in v)
print(" ".join(v))'; }

# 1. SecretIDs: every accessor the role lists.
sids=$(listing "auth/approle/role/$R/secret-id") || exit 1
sids=$(printf '%s' "$sids" | words) || fail "unparsable secret-id listing for $R"
n=0
for a in $sids; do
  out=$(b write "auth/approle/role/$R/secret-id-accessor/destroy" secret_id_accessor="$a" 2>&1) \
    || fail "destroying secret-id accessor $a: $out"
  n=$((n + 1))
done

# 2. Issued tokens: every accessor whose metadata names this role.
accs=$(listing auth/token/accessors) || exit 1
accs=$(printf '%s' "$accs" | words) || fail "unparsable token accessor listing"
t=0
for a in $accs; do
  if ! info=$(b token lookup -format=json -accessor "$a" 2>&1); then
    printf '%s' "$info" | grep -qi 'invalid accessor' && continue      # expired since the listing
    fail "token lookup $a: $info"
  fi
  role=$(printf '%s' "$info" | python3 -c 'import json, sys
print(((json.load(sys.stdin).get("data") or {}).get("meta") or {}).get("role_name") or "")') \
    || fail "unparsable lookup of $a"
  [ "$role" = "$R" ] || continue
  if ! out=$(b token revoke -accessor "$a" 2>&1); then
    printf '%s' "$out" | grep -qi 'invalid accessor' && continue
    fail "revoking token accessor $a: $out"
  fi
  echo "revoked token accessor $a"
  t=$((t + 1))
done
echo "$R: $n secret-id accessors destroyed, $t tokens revoked"
