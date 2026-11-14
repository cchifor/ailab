#!/bin/bash
# Integration test of the OpenBao provision script for the Relay worker budget ledger's credential
# delivery (kubernetes/apps/infrastructure/security/openbao/relay-budget-provision-job.yaml) under the
# Job's OWN image, against a REAL `bao server -dev` started inside the container (kv-v2 at `af`,
# kubernetes auth enabled) -- no fake CLI:
#   1. first run: both policies (exactly their three read grants), both k8s-auth roles bound to their
#      ServiceAccount@namespace, and NO KV document anywhere under af/relay-budget/.
#   2. second run: everything re-asserted; operator-written documents are never touched.
#   3. least privilege: each role's policy reads its own password document plus ca/witness, and
#      neither the other login's password nor a sibling path.
#   4. login-widening drift on a ROLE (namespace selector, audience, CIDRs, extra policy, extra
#      ServiceAccount) is cleared by the next run.
#   5. a policy changed by hand: REFUSED before EITHER policy or EITHER role is rewritten.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/security/openbao/relay-budget-provision-job.yaml"
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
N=0
run() { N=$((N + 1)); sh /w/provision.sh >/tmp/run.$N 2>&1; }
expect() { grep -q "$2" /tmp/run.$1 || { cat /tmp/run.$1 >&2; fail "run $1: expected '$2'"; }; }
role() { echo "auth/kubernetes/role/$1"; }
check_role() { # <role> <sa> <ns>
  bao read -field=bound_service_account_names "$(role "$1")" | grep -qx "\[$2\]" || fail "$1 SA binding"
  bao read -field=bound_service_account_namespaces "$(role "$1")" | grep -qx "\[$3\]" || fail "$1 namespace binding"
  bao read -field=token_policies "$(role "$1")" | grep -qx "\[$1\]" || fail "$1 policy"
  [ -z "$(bao read -field=bound_service_account_namespace_selector "$(role "$1")")" ] || fail "$1 namespace selector"
}
check_policy() { # <policy> <own document>
  [ "$(bao policy read "$1" | grep -c 'path ')" = 3 ] || fail "$1 carries other than three paths"
  for p in "$2" ca witness; do
    bao policy read "$1" | grep -q "path \"af/data/relay-budget/$p\" { capabilities = \[\"read\"\] }" || fail "$1 grant on $p"
  done
}
no_documents() {
  if bao kv list -mount=af relay-budget >/dev/null 2>&1; then fail "a KV document exists under af/relay-budget/"; fi
}

# 1. first run
run || { cat /tmp/run.$N >&2; fail "first run"; }
expect $N "policy af-app-relay-budget absent"
expect $N "policy af-app-router-budget absent"
expect $N "no KV document written"
check_policy af-app-relay-budget allocator
check_policy af-app-router-budget router
check_role af-app-relay-budget relay-budget-eso relay
check_role af-app-router-budget router-budget-eso llm-router
no_documents
echo "1: first run -> two policies, two roles, no KV document"

# 2. operator-written documents survive a run untouched
printf '%s' "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" | bao kv put -mount=af relay-budget/allocator password=- >/dev/null
printf '%s' "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210" | bao kv put -mount=af relay-budget/router password=- >/dev/null
printf '%s' "-----BEGIN CERTIFICATE-----" | bao kv put -mount=af relay-budget/ca certificate=- >/dev/null
printf '%s' '{"version":1}' | bao kv put -mount=af relay-budget/witness witness=- >/dev/null
printf '%s' "sibling" | bao kv put -mount=af relay-budget/other password=- >/dev/null
before="$(for p in allocator router ca witness; do bao kv metadata get -mount=af -format=json relay-budget/$p | grep -o '"current_version": [0-9]*'; done)"
run || { cat /tmp/run.$N >&2; fail "second run"; }
expect $N "policy af-app-relay-budget matches a reviewed form"
expect $N "policy af-app-router-budget matches a reviewed form"
after="$(for p in allocator router ca witness; do bao kv metadata get -mount=af -format=json relay-budget/$p | grep -o '"current_version": [0-9]*'; done)"
[ "$before" = "$after" ] || fail "a run wrote an operator-owned document"
[ "$(bao kv get -mount=af -field=password relay-budget/allocator)" = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" ] || fail "allocator password changed"
grep -q "0123456789abcdef" /tmp/run.$N && fail "a password appeared in the Job output"
echo "2: re-run -> operator documents untouched, nothing secret in the log"

# 3. least privilege
T="$(bao token create -policy=af-app-relay-budget -field=token)"
for p in allocator ca witness; do BAO_TOKEN="$T" bao kv get -mount=af relay-budget/$p >/dev/null || fail "relay cannot read $p"; done
for p in router other; do if BAO_TOKEN="$T" bao kv get -mount=af relay-budget/$p >/dev/null 2>&1; then fail "relay reads $p"; fi; done
if BAO_TOKEN="$T" bao kv metadata get -mount=af relay-budget/allocator >/dev/null 2>&1; then fail "relay reads metadata"; fi
T="$(bao token create -policy=af-app-router-budget -field=token)"
for p in router ca witness; do BAO_TOKEN="$T" bao kv get -mount=af relay-budget/$p >/dev/null || fail "router cannot read $p"; done
for p in allocator other; do if BAO_TOKEN="$T" bao kv get -mount=af relay-budget/$p >/dev/null 2>&1; then fail "router reads $p"; fi; done
echo "3: each role reads its own password plus ca/witness, nothing else"

# 4. login-widening drift on both roles is cleared
for r in af-app-relay-budget af-app-router-budget; do
  bao write "$(role $r)" bound_service_account_names=relay-budget-eso,intruder bound_service_account_namespace_selector='{"matchLabels":{"any":"ns"}}' \
    audience=other token_bound_cidrs=10.0.0.0/8 token_policies=$r,default >/dev/null
done
run || { cat /tmp/run.$N >&2; fail "run after role drift"; }
check_role af-app-relay-budget relay-budget-eso relay
check_role af-app-router-budget router-budget-eso llm-router
for r in af-app-relay-budget af-app-router-budget; do
  [ -z "$(bao read -field=audience "$(role $r)" 2>/dev/null)" ] || fail "$r audience survived a run"
  bao read -field=token_bound_cidrs "$(role $r)" | grep -qx '\[\]' || fail "$r token_bound_cidrs survived a run"
done
echo "4: role drift (extra SA, selector, audience, CIDRs, extra policy) -> cleared"

# 5. a drifted policy -> refused before either policy or either role is rewritten
bao policy write af-app-router-budget - >/dev/null <<'P'
path "af/data/relay-budget/router" { capabilities = ["read", "list"] }
path "af/data/relay-budget/ca" { capabilities = ["read"] }
path "af/data/relay-budget/witness" { capabilities = ["read"] }
P
bao write "$(role af-app-relay-budget)" bound_service_account_names=marker >/dev/null
if run; then fail "a drifted policy must be refused"; fi
expect $N "REFUSING: policy af-app-router-budget"
bao policy read af-app-router-budget | grep -q '"list"' || fail "the drifted policy was rewritten anyway"
bao read -field=bound_service_account_names "$(role af-app-relay-budget)" | grep -qx '\[marker\]' || fail "a role was rewritten after a refusal"
echo "5: drifted policy -> refused, nothing rewritten"
echo "driver: all scenarios passed"
DRIVER

HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
chmod 755 "$WORK"; chmod 644 "$WORK/provision.sh" "$WORK/driver.sh"
docker run --rm -u 100:1000 -v "$HOSTWORK:/w:ro" -e HOME=/tmp --entrypoint sh "$IMAGE" /w/driver.sh
echo "test-relay-budget-provision: OK (create, no KV, operator documents untouched, least privilege, role drift, policy drift refusal)"
