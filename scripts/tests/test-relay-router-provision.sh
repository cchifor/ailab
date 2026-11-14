#!/bin/bash
# Integration test of the OpenBao provision script for the Relay Router connection
# (kubernetes/apps/infrastructure/security/openbao/relay-router-provision-job.yaml) under the Job's OWN
# image, against a REAL `bao server -dev` started inside the container (kv-v2 at `af`, kubernetes auth
# enabled) -- no fake CLI:
#   1. first run: three policies (exact grants), two k8s-auth roles bound to their ServiceAccount@
#      namespace, the token role, NO KV document under af/relay-router/ and NO token minted.
#   2. second run: everything re-asserted; operator-written documents are never touched.
#   3. the escrow writer, minted from the token role exactly as the runbook does, behaves as Relay's
#      BaoEscrow needs (create with cas=0, read a version, CAS refusal of an overwrite, destroy) and is
#      denied everything else: another tenant, a shallower or deeper path, delete, metadata, list, the
#      connection documents, the ledger documents, lookup-self. Each ESO reader reads its own documents
#      and nothing else.
#   4. drift on the two k8s-auth roles and on the token role is cleared by the next run.
#   5. a policy changed by hand: REFUSED before ANY policy or role is rewritten.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/security/openbao/relay-router-provision-job.yaml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

IMAGE="$(sed -n 's/^ *image: *\(quay.io\/openbao\/openbao:[^ ]*\).*/\1/p' "$MANIFEST" | head -1)"
[ -n "$IMAGE" ] || { echo "cannot find the Job image in $MANIFEST" >&2; exit 1; }

PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$MANIFEST" "$WORK/provision.sh" <<'PY'
import sys, yaml
manifest, out = sys.argv[1:3]
docs = [d for d in yaml.safe_load_all(open(manifest, encoding="utf-8")) if d]
cm = next(d for d in docs if d.get("kind") == "ConfigMap" and "provision.sh" in d.get("data", {}))
open(out, "w", encoding="utf-8", newline="\n").write(cm["data"]["provision.sh"])
PY

cat > "$WORK/driver.sh" <<'DRIVER'
set -eu
fail() { echo "FAIL: $*" >&2; exit 1; }
bao server -dev -dev-root-token-id=root -dev-listen-address=127.0.0.1:8200 >/tmp/server.log 2>&1 &
export BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=root
i=0; until bao status >/dev/null 2>&1; do i=$((i + 1)); [ $i -lt 50 ] || fail "dev server did not start"; sleep 0.2; done
bao secrets enable -path=af kv-v2 >/dev/null
bao auth enable kubernetes >/dev/null
TENANT=00000000-0000-4000-8000-000000000001
OTHER=00000000-0000-4000-8000-000000000002
HOST=3f0a3d4e-6b1c-4f6e-9a51-0c9e2b7d8a10
CRED=8d1e5b2c-2a4f-4c3e-b6d7-5f9a0e1c2b3d
ESC="af/data/relay-router/escrow/$TENANT/$HOST/$CRED"
N=0
run() { N=$((N + 1)); sh /w/provision.sh >/tmp/run.$N 2>&1; }
expect() { grep -q "$2" /tmp/run.$1 || { cat /tmp/run.$1 >&2; fail "run $1: expected '$2'"; }; }
role() { echo "auth/kubernetes/role/$1"; }
TROLE=auth/token/roles/relay-router-escrow
accessors() { bao list -format=json auth/token/accessors | tr -d '[] \n' | tr ',' '\n' | grep -c . ; }
check_role() { # <role> <sa> <ns>
  bao read -field=bound_service_account_names "$(role "$1")" | grep -qx "\[$2\]" || fail "$1 SA binding"
  bao read -field=bound_service_account_namespaces "$(role "$1")" | grep -qx "\[$3\]" || fail "$1 namespace binding"
  bao read -field=token_policies "$(role "$1")" | grep -qx "\[$1\]" || fail "$1 policy"
  [ -z "$(bao read -field=bound_service_account_namespace_selector "$(role "$1")")" ] || fail "$1 namespace selector"
}
check_token_role() {
  j="$(bao read -format=json $TROLE | tr -d '[:space:]')"
  for want in '"allowed_policies":\["relay-router-escrow"\]' '"allowed_policies_glob":\[\]' '"disallowed_policies":\[\]' \
              '"disallowed_policies_glob":\[\]' '"allowed_entity_aliases":\[\]' '"orphan":true' '"renewable":false' \
              '"token_no_default_policy":true' '"token_explicit_max_ttl":2592000[,}]' '"token_period":0[,}]' \
              '"period":0[,}]' '"explicit_max_ttl":0[,}]' '"token_type":"service"'; do
    echo "$j" | grep -q "$want" || fail "token role: want $want in $j"
  done
  if echo "$j" | grep -q '"token_num_uses":[1-9]\|"token_bound_cidrs":\["'; then fail "token role kept uses/CIDRs: $j"; fi
}
check_policies() {
  [ "$(bao policy read relay-router-escrow | grep -c 'path ')" = 2 ] || fail "escrow policy carries other than two paths"
  bao policy read relay-router-escrow | grep -qF "path \"af/data/relay-router/escrow/$TENANT/+/+\" { capabilities = [\"create\", \"read\", \"update\"] }" || fail "escrow data grant"
  bao policy read relay-router-escrow | grep -qF "path \"af/destroy/relay-router/escrow/$TENANT/+/+\" { capabilities = [\"update\"] }" || fail "escrow destroy grant"
  [ "$(bao policy read af-app-relay-router | grep -c 'path ')" = 3 ] || fail "relay reader carries other than three paths"
  for p in admin-signer reconciler-signer escrow-token; do
    bao policy read af-app-relay-router | grep -qF "path \"af/data/relay-router/relay/$p\" { capabilities = [\"read\"] }" || fail "relay reader grant on $p"
  done
  [ "$(bao policy read af-app-router-relay-keys | grep -c 'path ')" = 1 ] || fail "router reader carries other than one path"
  bao policy read af-app-router-relay-keys | grep -qF 'path "af/data/relay-router/router/trust" { capabilities = ["read"] }' || fail "router reader grant"
}
no_documents() {
  if bao kv list -mount=af relay-router >/dev/null 2>&1; then fail "a KV document exists under af/relay-router/"; fi
}
denied() { # <description> <command...> -- the command must FAIL with the given token
  d="$1"; shift
  if "$@" >/tmp/denied.out 2>&1; then cat /tmp/denied.out >&2; fail "escrow writer was allowed to $d"; fi
}

# 1. first run
before_tokens="$(accessors)"
run || { cat /tmp/run.$N >&2; fail "first run"; }
expect $N "policy relay-router-escrow absent"
expect $N "policy af-app-relay-router absent"
expect $N "policy af-app-router-relay-keys absent"
expect $N "no KV document and no token written"
check_policies
check_role af-app-relay-router relay-router-eso relay
check_role af-app-router-relay-keys router-relay-keys-eso llm-router
check_token_role
no_documents
[ "$(accessors)" = "$before_tokens" ] || fail "the Job minted a token"
echo "1: first run -> three policies, two k8s-auth roles, token role, no KV document, no token"

# 2. operator-written documents survive a run untouched
printf '%s' "PRIVATE-ADMIN-PEM" | bao kv put -mount=af relay-router/relay/admin-signer private_key=- >/dev/null
printf '%s' "PRIVATE-RECONCILER-PEM" | bao kv put -mount=af relay-router/relay/reconciler-signer private_key=- >/dev/null
printf '%s' "escrow-token-value-0123456789" | bao kv put -mount=af relay-router/relay/escrow-token token=- >/dev/null
bao kv put -mount=af relay-router/router/trust admin_public_key=PUBLIC-ADMIN reconciler_public_key=PUBLIC-RECONCILER >/dev/null
printf '%s' "ledger" | bao kv put -mount=af relay-budget/witness witness=- >/dev/null
snap() { for p in relay/admin-signer relay/reconciler-signer relay/escrow-token router/trust; do bao kv metadata get -mount=af -format=json relay-router/$p | grep -o '"current_version": [0-9]*'; done; }
before="$(snap)"
run || { cat /tmp/run.$N >&2; fail "second run"; }
expect $N "policy relay-router-escrow matches a reviewed form"
expect $N "policy af-app-relay-router matches a reviewed form"
expect $N "policy af-app-router-relay-keys matches a reviewed form"
[ "$before" = "$(snap)" ] || fail "a run wrote an operator-owned document"
if grep -q "PRIVATE-\|escrow-token-value" /tmp/run.$N; then fail "a secret appeared in the Job output"; fi
echo "2: re-run -> operator documents untouched, nothing secret in the log"

# 3. least privilege. The escrow token is minted the way the runbook mints it.
W="$(bao token create -role=relay-router-escrow -field=token)"
L="$(bao token lookup -format=json "$W")"
echo "$L" | tr -d ' \n' | grep -q '"policies":\["relay-router-escrow"\]' || fail "escrow token policies: $L"
echo "$L" | tr -d ' \n' | grep -q '"orphan":true' || fail "escrow token is not an orphan"
echo "$L" | tr -d ' \n' | grep -q '"renewable":false' || fail "escrow token is renewable"
if bao token create -role=relay-router-escrow -policy=default -field=token >/dev/null 2>&1; then fail "the role mints a token with another policy"; fi
# Relay BaoEscrow.ensure: POST data with options.cas=0; then GET (and GET ?version=1); destroy version 1.
printf '%s' '{"options":{"cas":0},"data":{"token":"router-token-0123456789abcdef","router_key_id":"key_0123456789ab","operation_id":"op"}}' \
  | BAO_TOKEN="$W" bao write "$ESC" - >/dev/null || fail "escrow writer cannot create a new escrow version"
BAO_TOKEN="$W" bao read -field=data "$ESC" | grep -q router-token-0123456789abcdef || fail "escrow writer cannot read its escrow"
BAO_TOKEN="$W" bao read -field=metadata "$ESC" version=1 | grep -q "version:1" || fail "escrow writer cannot read version 1"
if printf '%s' '{"options":{"cas":0},"data":{"token":"another-token-0123456789","router_key_id":"key_0123456789ab","operation_id":"op"}}' \
  | BAO_TOKEN="$W" bao write "$ESC" - >/dev/null 2>&1; then fail "cas=0 overwrote an existing escrow"; fi
printf '%s' '{"versions":[1]}' | BAO_TOKEN="$W" bao write "af/destroy/relay-router/escrow/$TENANT/$HOST/$CRED" - >/dev/null || fail "escrow writer cannot destroy its version"
printf '%s' '{"options":{"cas":0},"data":{"token":"router-token-0123456789abcdef","router_key_id":"key_0123456789ab","operation_id":"op"}}' >/tmp/body.json
denied "write another tenant" sh -c "BAO_TOKEN='$W' bao write af/data/relay-router/escrow/$OTHER/$HOST/$CRED - </tmp/body.json"
denied "write a shallower path" sh -c "BAO_TOKEN='$W' bao write af/data/relay-router/escrow/$TENANT/$HOST - </tmp/body.json"
denied "write a deeper path" sh -c "BAO_TOKEN='$W' bao write af/data/relay-router/escrow/$TENANT/$HOST/$CRED/x - </tmp/body.json"
denied "write outside the escrow prefix" sh -c "BAO_TOKEN='$W' bao write af/data/relay-router/relay/escrow-token - </tmp/body.json"
denied "delete an escrow" env BAO_TOKEN="$W" bao delete "$ESC"
denied "read escrow metadata" env BAO_TOKEN="$W" bao read "af/metadata/relay-router/escrow/$TENANT/$HOST/$CRED"
denied "list escrows" env BAO_TOKEN="$W" bao list "af/metadata/relay-router/escrow/$TENANT"
denied "destroy another tenant" sh -c "printf '%s' '{\"versions\":[1]}' | BAO_TOKEN='$W' bao write af/destroy/relay-router/escrow/$OTHER/$HOST/$CRED -"
denied "read the admin signer" env BAO_TOKEN="$W" bao read af/data/relay-router/relay/admin-signer
denied "read the ledger witness" env BAO_TOKEN="$W" bao read af/data/relay-budget/witness
denied "look itself up (default policy)" env BAO_TOKEN="$W" bao token lookup
T="$(bao token create -policy=af-app-relay-router -field=token)"
for p in admin-signer reconciler-signer escrow-token; do BAO_TOKEN="$T" bao kv get -mount=af relay-router/relay/$p >/dev/null || fail "relay reader cannot read $p"; done
if BAO_TOKEN="$T" bao kv get -mount=af relay-router/router/trust >/dev/null 2>&1; then fail "relay reader reads the trust document"; fi
if BAO_TOKEN="$T" bao read "$ESC" >/dev/null 2>&1; then fail "relay reader reads the escrow"; fi
if BAO_TOKEN="$T" bao kv metadata get -mount=af relay-router/relay/admin-signer >/dev/null 2>&1; then fail "relay reader reads metadata"; fi
T="$(bao token create -policy=af-app-router-relay-keys -field=token)"
BAO_TOKEN="$T" bao kv get -mount=af relay-router/router/trust >/dev/null || fail "router reader cannot read the trust document"
for p in relay/admin-signer relay/reconciler-signer relay/escrow-token; do
  if BAO_TOKEN="$T" bao kv get -mount=af relay-router/$p >/dev/null 2>&1; then fail "router reader reads $p"; fi
done
echo "3: escrow writer does exactly create/read/CAS/destroy on its tenant's escrow; each reader reads its own documents"

# 4. drift on the k8s-auth roles and on the token role is cleared
for r in af-app-relay-router af-app-router-relay-keys; do
  bao write "$(role $r)" bound_service_account_names=relay-router-eso,intruder bound_service_account_namespace_selector='{"matchLabels":{"any":"ns"}}' \
    audience=other token_bound_cidrs=10.0.0.0/8 token_policies=$r,default >/dev/null
done
bao write $TROLE allowed_policies=relay-router-escrow,default allowed_policies_glob='*' orphan=false renewable=true \
  token_no_default_policy=false token_period=768h token_explicit_max_ttl=0 token_num_uses=5 token_bound_cidrs=10.0.0.0/8 \
  allowed_entity_aliases=intruder path_suffix=other >/dev/null
run || { cat /tmp/run.$N >&2; fail "run after role drift"; }
check_role af-app-relay-router relay-router-eso relay
check_role af-app-router-relay-keys router-relay-keys-eso llm-router
for r in af-app-relay-router af-app-router-relay-keys; do
  [ -z "$(bao read -field=audience "$(role $r)" 2>/dev/null)" ] || fail "$r audience survived a run"
  bao read -field=token_bound_cidrs "$(role $r)" | grep -qx '\[\]' || fail "$r token_bound_cidrs survived a run"
done
check_token_role
echo "4: k8s-auth role drift and token role drift (policy, glob, parent, renewal, period, TTL, uses, CIDRs) -> cleared"

# 5. a drifted policy -> refused before any policy or role is rewritten
bao policy write relay-router-escrow - >/dev/null <<P
path "af/data/relay-router/escrow/$TENANT/+/+" { capabilities = ["create", "read", "update", "delete"] }
path "af/destroy/relay-router/escrow/$TENANT/+/+" { capabilities = ["update"] }
P
bao write "$(role af-app-relay-router)" bound_service_account_names=marker >/dev/null
if run; then fail "a drifted policy must be refused"; fi
expect $N "REFUSING: policy relay-router-escrow"
bao policy read relay-router-escrow | grep -q '"delete"' || fail "the drifted policy was rewritten anyway"
bao read -field=bound_service_account_names "$(role af-app-relay-router)" | grep -qx '\[marker\]' || fail "a role was rewritten after a refusal"
echo "5: drifted policy -> refused, nothing rewritten"
echo "driver: all scenarios passed"
DRIVER

HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
chmod 755 "$WORK"; chmod 644 "$WORK/provision.sh" "$WORK/driver.sh"
docker run --rm -u 100:1000 -v "$HOSTWORK:/w:ro" -e HOME=/tmp --entrypoint sh "$IMAGE" /w/driver.sh
echo "test-relay-router-provision: OK (create, no KV, no token, escrow writer least privilege, role + token-role drift, policy drift refusal)"
