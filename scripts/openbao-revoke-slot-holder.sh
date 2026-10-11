#!/usr/bin/env bash
# openbao-revoke-slot-holder.sh ROLE KEEP_SECRET_ID_ACCESSOR KEEP_HOLDER - cut a dev-worker slot's
# PREVIOUS machine out of OpenBao at a re-slot (docs/runbooks/s2s-identity.md, "Re-slot").
#
# Destroys every SecretID of AppRole ROLE except the replacement's (KEEP_SECRET_ID_ACCESSOR), and revokes
# every token the role issued except those whose metadata carries holder=KEEP_HOLDER (the replacement's
# SecretID was minted with that metadata, so its logins carry it). Destroying SecretIDs alone revokes
# nothing already issued: the role's tokens are periodic and renewable. These are the two sweeps the
# provision Job's RETIRED_SLOTS step runs (devworker-provision-job.yaml), for one role that stays.
#
# FAIL CLOSED: every list, lookup, destroy and revoke is checked. Only the documented empty answers
# (`{}` or "No value found" on a listing, "invalid accessor" for a token that expired meanwhile) count as
# nothing-to-do; a denial, a sealed or unreachable vault, unparsable output or a missing replacement
# accessor exits 1, before anything is published. A run during an OpenBao outage proves nothing: repeat
# it once the vault is healthy. Prints accessors and counts only, never a token or SecretID value.
#
# Needs BAO_ADDR (plus BAO_CACERT / BAO_TLS_SERVER_NAME) as in openbao-dev-workers.md section (e), and
# cluster-admin kubectl for the break-glass token, which is passed to each bao call as an env prefix.
# Exit: 0 swept (the summary line says how much), 1 aborted, 2 bad usage.
set -uo pipefail

[ $# -eq 3 ] && [ -n "$1" ] && [ -n "$2" ] && [ -n "$3" ] \
  || { echo "usage: $0 ROLE KEEP_SECRET_ID_ACCESSOR KEEP_HOLDER" >&2; exit 2; }
R=$1 KEEP_SID=$2 KEEP_HOLDER=$3
case "$R$KEEP_HOLDER" in *"|"*) echo "ROLE and KEEP_HOLDER must not contain |" >&2; exit 2;; esac

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

# 1. SecretIDs: every accessor but the replacement's. Its absence means the wrong accessor was given,
#    or the replacement was never bootstrapped: stop rather than strand the new machine.
sids=$(listing "auth/approle/role/$R/secret-id") || exit 1
sids=$(printf '%s' "$sids" | words) || fail "unparsable secret-id listing for $R"
case " $sids " in *" $KEEP_SID "*) ;; *) fail "the replacement's accessor $KEEP_SID is not listed on $R";; esac
n=0
for a in $sids; do
  [ "$a" = "$KEEP_SID" ] && continue
  out=$(b write "auth/approle/role/$R/secret-id-accessor/destroy" secret_id_accessor="$a" 2>&1) \
    || fail "destroying secret-id accessor $a: $out"
  n=$((n + 1))
done

# 2. Issued tokens: every accessor whose metadata names this role and not the replacement.
accs=$(listing auth/token/accessors) || exit 1
accs=$(printf '%s' "$accs" | words) || fail "unparsable token accessor listing"
t=0
for a in $accs; do
  if ! info=$(b token lookup -format=json -accessor "$a" 2>&1); then
    printf '%s' "$info" | grep -qi 'invalid accessor' && continue      # expired since the listing
    fail "token lookup $a: $info"
  fi
  meta=$(printf '%s' "$info" | python3 -c 'import json, sys
m = (json.load(sys.stdin).get("data") or {}).get("meta") or {}
print((m.get("role_name") or "") + "|" + (m.get("holder") or ""))') || fail "unparsable lookup of $a"
  IFS="|" read -r role holder <<<"$meta"
  [ "$role" = "$R" ] && [ "${holder:-}" != "$KEEP_HOLDER" ] || continue
  if ! out=$(b token revoke -accessor "$a" 2>&1); then
    printf '%s' "$out" | grep -qi 'invalid accessor' && continue
    fail "revoking token accessor $a: $out"
  fi
  echo "revoked token accessor $a"
  t=$((t + 1))
done
echo "$R: $n secret-id accessors destroyed, $t tokens revoked (kept $KEEP_SID, holder=$KEEP_HOLDER)"
