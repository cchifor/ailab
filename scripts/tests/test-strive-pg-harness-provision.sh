#!/bin/bash
# Integration test of the OpenBao provision script for the harness role's password
# (kubernetes/apps/infrastructure/security/openbao/strive-pg-harness-provision-job.yaml) under the
# Job's OWN image, against a REAL `bao server -dev` started inside the container (kv-v2 at `af`,
# kubernetes auth enabled) -- no fake CLI, so the CAS, stdin and read-back semantics are the real ones:
#   1. first run: policy (read on af/data/strive/pg-harness ONLY), k8s-auth role bound to
#      strive-pg-harness-eso@strive-ailab with that policy, and af/strive/pg-harness.password =
#      48 lowercase hex characters; the value never appears in the Job's output.
#   2. second run: everything re-asserted, the KV value UNCHANGED (operator-owned after creation).
#   3. an operator-set value (rotation by `bao kv patch`) survives a run.
#   4. the policy changed by hand: REFUSED before the policy or the role is rewritten.
#   5. the document soft-deleted: a loud failure carrying the CLI's own message, no new value.
#   6. the role's token can read the password and nothing else (a sibling path is denied).
#   3b. a namespace selector / audience / CIDRs added to the ROLE by hand are cleared by the next run
#      (a role write keeps omitted fields, so the Job must write them empty).
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/security/openbao/strive-pg-harness-provision-job.yaml"
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
pw() { bao kv get -mount=af -field=password strive/pg-harness; }
version() { bao kv metadata get -mount=af -format=json strive/pg-harness | grep -o '"current_version": [0-9]*'; }

# 1. first run
run || { cat /tmp/run.$N >&2; fail "first run"; }
expect $N "policy af-app-strive-pg-harness absent"
expect $N "created with a generated password"
P1="$(pw)"
echo "$P1" | grep -qx '[0-9a-f]\{48\}' || fail "generated password is not 48 lowercase hex characters"
grep -q -F -- "$P1" /tmp/run.$N && fail "the generated password appeared in the Job output"
[ "$(bao policy read af-app-strive-pg-harness | grep -c 'path ')" = 1 ] || fail "policy carries more than one path"
bao policy read af-app-strive-pg-harness | grep -q 'path "af/data/strive/pg-harness" { capabilities = \["read"\] }' || fail "policy grant"
bao read -field=bound_service_account_names auth/kubernetes/role/af-app-strive-pg-harness | grep -qx '\[strive-pg-harness-eso\]' || fail "role SA binding"
bao read -field=bound_service_account_namespaces auth/kubernetes/role/af-app-strive-pg-harness | grep -qx '\[strive-ailab\]' || fail "role namespace binding"
bao read -field=token_policies auth/kubernetes/role/af-app-strive-pg-harness | grep -qx '\[af-app-strive-pg-harness\]' || fail "role policy"
echo "1: first run -> policy, role, generated 48-hex password (not in the log)"

# 2. second run keeps the value
V1="$(version)"
run || { cat /tmp/run.$N >&2; fail "second run"; }
expect $N "matches a reviewed form"
expect $N "already exists; its value is operator-owned"
grep -q "concurrently" /tmp/run.$N && fail "an existing document must be skipped before any write is attempted"
[ "$(pw)" = "$P1" ] && [ "$(version)" = "$V1" ] || fail "a re-run changed the KV document"
echo "2: re-run -> KV untouched"

# 3. an operator rotation survives
printf '%s' "operator-chosen-0123456789abcdef" | bao kv patch -mount=af strive/pg-harness password=- >/dev/null
run || fail "run after operator rotation"
[ "$(pw)" = "operator-chosen-0123456789abcdef" ] || fail "the Job overwrote an operator-set value"
echo "3: operator-set value -> kept"

# 3b. login-widening drift on the ROLE (a role write is an update that keeps omitted fields): a
# namespace selector, an audience and CIDRs added by hand are all cleared by the next run.
bao write auth/kubernetes/role/af-app-strive-pg-harness bound_service_account_namespace_selector='{"matchLabels":{"any":"ns"}}' audience=other token_bound_cidrs=10.0.0.0/8 >/dev/null
[ -n "$(bao read -field=bound_service_account_namespace_selector auth/kubernetes/role/af-app-strive-pg-harness)" ] || fail "test setup: selector not set"
run || { cat /tmp/run.$N >&2; fail "run after role drift"; }
[ -z "$(bao read -field=bound_service_account_namespace_selector auth/kubernetes/role/af-app-strive-pg-harness)" ] || fail "the namespace selector survived a run"
[ -z "$(bao read -field=audience auth/kubernetes/role/af-app-strive-pg-harness 2>/dev/null)" ] || fail "the audience survived a run"
bao read -field=token_bound_cidrs auth/kubernetes/role/af-app-strive-pg-harness | grep -qx '\[\]' || fail "token_bound_cidrs survived a run"
echo "3b: role drift (namespace selector, audience, CIDRs) -> cleared"

# 6. (before the drift case mutates the policy) the role's policy reads the password and nothing else
printf '%s' x | bao kv put -mount=af strive/other password=- >/dev/null
T="$(bao token create -policy=af-app-strive-pg-harness -field=token)"
BAO_TOKEN="$T" bao kv get -mount=af -field=password strive/pg-harness >/dev/null || fail "the policy cannot read its document"
if BAO_TOKEN="$T" bao kv get -mount=af strive/other >/dev/null 2>&1; then fail "the policy reads a sibling path"; fi
echo "6: the policy reads exactly its one document"

# 4. drifted policy -> refused before anything is rewritten
bao policy write af-app-strive-pg-harness - >/dev/null <<'P'
path "af/data/strive/pg-harness" { capabilities = ["read", "list"] }
P
bao write auth/kubernetes/role/af-app-strive-pg-harness bound_service_account_names=marker bound_service_account_namespaces=strive-ailab token_policies=af-app-strive-pg-harness >/dev/null
if run; then fail "a drifted policy must be refused"; fi
expect $N "REFUSING"
bao policy read af-app-strive-pg-harness | grep -q '"list"' || fail "the drifted policy was rewritten anyway"
bao read -field=bound_service_account_names auth/kubernetes/role/af-app-strive-pg-harness | grep -qx '\[marker\]' || fail "the role was rewritten after a refusal"
echo "4: drifted policy -> refused, nothing rewritten"

# 5. soft-deleted document -> loud failure, no new value
bao policy write af-app-strive-pg-harness - >/dev/null <<'P'
path "af/data/strive/pg-harness" { capabilities = ["read"] }
P
bao kv delete -mount=af strive/pg-harness >/dev/null
if run; then fail "a soft-deleted document must fail the Job"; fi
expect $N "operator action needed"
expect $N "check-and-set parameter did not match"
bao kv metadata get -mount=af -format=json strive/pg-harness | grep -q '"current_version": 2' || fail "a new version was written over a soft-deleted one"
echo "5: soft-deleted -> loud failure, nothing written"
echo "driver: all scenarios passed"
DRIVER

HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
chmod 755 "$WORK"; chmod 644 "$WORK/provision.sh" "$WORK/driver.sh"
docker run --rm -u 100:1000 -v "$HOSTWORK:/w:ro" -e HOME=/tmp --entrypoint sh "$IMAGE" /w/driver.sh
echo "test-strive-pg-harness-provision: OK (create, keep, operator-owned value, drift refusal, soft-delete, least privilege)"
