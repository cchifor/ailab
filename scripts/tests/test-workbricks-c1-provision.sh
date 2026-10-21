#!/bin/bash
# Integration test of the OpenBao provision script for the Workbricks cell c1
# (kubernetes/apps/infrastructure/security/openbao/workbricks-c1-provision-job.yaml) under the Job's
# OWN image, against REAL `bao server -dev` instances started inside the container (kv-v2 at `af`,
# kubernetes auth enabled) -- no fake CLI:
#   1. first run: transit/ enabled; three ecdsa-p256 keys, not exportable, no plaintext backup,
#      deletion not allowed; three policies equal to their pinned forms; three roles bound to the
#      right ServiceAccounts and namespaces (the sandbox namespace in none).
#   2. second run: everything re-asserted, the keys unchanged (still version 1).
#   3. role drift (namespace selector, audience, CIDRs) added by hand is cleared by the next run.
#   4. least privilege: each policy signs with its own keys only, reads public keys only, cannot
#      export, back up or rotate; the secrets policy reads af/workbricks/c1/* and nothing else.
#   5. a policy changed by hand: REFUSED before anything is rewritten.
#   6. (fresh vault) a non-transit engine mounted at transit/: REFUSED, nothing written.
#   7. (fresh vault) a key pre-created exportable: REFUSED, no policy or role written.
# Requires docker. Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/security/openbao/workbricks-c1-provision-job.yaml"
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
export BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=root
SERVER=
fresh_vault() {
  if [ -n "$SERVER" ]; then kill "$SERVER"; wait "$SERVER" 2>/dev/null || true; fi
  bao server -dev -dev-root-token-id=root -dev-listen-address=127.0.0.1:8200 >/tmp/server.log 2>&1 &
  SERVER=$!
  i=0; until bao status >/dev/null 2>&1; do i=$((i + 1)); [ $i -lt 50 ] || fail "dev server did not start"; sleep 0.2; done
  bao secrets enable -path=af kv-v2 >/dev/null
  bao auth enable kubernetes >/dev/null
}
N=0
run() { N=$((N + 1)); sh /w/provision.sh >/tmp/run.$N 2>&1; }
expect() { grep -q "$2" /tmp/run.$1 || { cat /tmp/run.$1 >&2; fail "run $1: expected '$2'"; }; }
field() { bao read -field="$2" "auth/kubernetes/role/$1"; }
keyj() { bao read -format=json "transit/keys/$1"; }
KEYS="workbricks-c1-jwks-root workbricks-c1-release workbricks-c1-audit"

fresh_vault
# 1. first run
run || { cat /tmp/run.$N >&2; fail "first run"; }
expect $N "transit engine enabled at transit/"
expect $N "policy workbricks-c1-gatekeeper absent"
expect $N "workbricks-c1 provisioning complete"
bao read -format=json sys/mounts/transit | grep -q '"type": "transit"' || fail "transit/ is not a transit engine"
for k in $KEYS; do
  j="$(keyj "$k")"
  for want in '"type": "ecdsa-p256"' '"exportable": false' '"allow_plaintext_backup": false' '"deletion_allowed": false' '"latest_version": 1'; do
    echo "$j" | grep -q "$want" || fail "key $k: expected $want"
  done
done
# The policies equal the *_DESIRED forms the script pins (evaluated from the script itself, so a
# DESIRED that later references its BASELINE still resolves).
norm() { sed -e 's/^[[:space:]]*#.*$//' | tr -d '[:space:]'; }
eval "$(grep -E '^(GK|SIGN|SECRETS)_(BASELINE|DESIRED)=' /w/provision.sh)"
[ -n "$GK_DESIRED" ] && [ -n "$SIGN_DESIRED" ] && [ -n "$SECRETS_DESIRED" ] || fail "cannot find the pinned policy forms in the script"
[ "$(bao policy read workbricks-c1-gatekeeper | norm)" = "$GK_DESIRED" ] || fail "policy workbricks-c1-gatekeeper differs from GK_DESIRED"
[ "$(bao policy read workbricks-c1-signing | norm)" = "$SIGN_DESIRED" ] || fail "policy workbricks-c1-signing differs from SIGN_DESIRED"
[ "$(bao policy read workbricks-c1-secrets | norm)" = "$SECRETS_DESIRED" ] || fail "policy workbricks-c1-secrets differs from SECRETS_DESIRED"
[ "$(field workbricks-c1-gatekeeper bound_service_account_names)" = "[gatekeeper]" ] || fail "gatekeeper role SA"
[ "$(field workbricks-c1-gatekeeper bound_service_account_namespaces)" = "[workbricks-c1]" ] || fail "gatekeeper role namespace"
[ "$(field workbricks-c1-gatekeeper token_policies)" = "[workbricks-c1-gatekeeper]" ] || fail "gatekeeper role policy"
[ "$(field workbricks-c1-signing bound_service_account_names)" = "[signing]" ] || fail "signing role SA"
[ "$(field workbricks-c1-signing bound_service_account_namespaces)" = "[workbricks-c1]" ] || fail "signing role namespace"
[ "$(field workbricks-c1-signing token_policies)" = "[workbricks-c1-signing]" ] || fail "signing role policy"
[ "$(field workbricks-c1-secrets bound_service_account_names)" = "[secret-delivery]" ] || fail "secrets role SA"
[ "$(field workbricks-c1-secrets bound_service_account_namespaces)" = "[workbricks-c1 workbricks-c1-data]" ] || fail "secrets role namespaces"
[ "$(field workbricks-c1-secrets token_policies)" = "[workbricks-c1-secrets]" ] || fail "secrets role policy"
for r in workbricks-c1-gatekeeper workbricks-c1-signing workbricks-c1-secrets; do
  if field "$r" bound_service_account_namespaces | grep -q sandbox; then fail "role $r reaches the sandbox namespace"; fi
done
echo "1: first run -> transit/, 3 keys (p256, not exportable), 3 pinned policies, 3 roles"

# 2. second run keeps the keys
run || { cat /tmp/run.$N >&2; fail "second run"; }
expect $N "transit engine at transit/ present"
expect $N "policy workbricks-c1-signing matches a reviewed form"
for k in $KEYS; do keyj "$k" | grep -q '"latest_version": 1' || fail "a re-run changed key $k"; done
echo "2: re-run -> everything re-asserted, keys unchanged"

# 3. login-widening drift on a ROLE is cleared
bao write auth/kubernetes/role/workbricks-c1-secrets bound_service_account_namespace_selector='{"matchLabels":{"any":"ns"}}' audience=other token_bound_cidrs=10.0.0.0/8 >/dev/null
[ -n "$(field workbricks-c1-secrets bound_service_account_namespace_selector)" ] || fail "test setup: selector not set"
run || { cat /tmp/run.$N >&2; fail "run after role drift"; }
[ -z "$(field workbricks-c1-secrets bound_service_account_namespace_selector)" ] || fail "the namespace selector survived a run"
[ -z "$(field workbricks-c1-secrets audience 2>/dev/null)" ] || fail "the audience survived a run"
[ "$(field workbricks-c1-secrets token_bound_cidrs)" = "[]" ] || fail "token_bound_cidrs survived a run"
echo "3: role drift (namespace selector, audience, CIDRs) -> cleared"

# 4. least privilege. allow/deny run a command as a given token; deny fails the test if it works.
IN=aGVsbG8= # "hello"
allow() { msg=$1; t=$2; shift 2; env BAO_TOKEN="$t" "$@" >/dev/null 2>&1 || fail "$msg"; }
deny() { msg=$1; t=$2; shift 2; if env BAO_TOKEN="$t" "$@" >/dev/null 2>&1; then fail "$msg"; fi; }
GK="$(bao token create -policy=workbricks-c1-gatekeeper -field=token)"
SG="$(bao token create -policy=workbricks-c1-signing -field=token)"
SC="$(bao token create -policy=workbricks-c1-secrets -field=token)"
allow "gatekeeper cannot sign with jwks-root (jws)" "$GK" bao write transit/sign/workbricks-c1-jwks-root input=$IN marshaling_algorithm=jws
allow "gatekeeper cannot sign with jwks-root/sha2-256" "$GK" bao write transit/sign/workbricks-c1-jwks-root/sha2-256 input=$IN
allow "gatekeeper cannot read jwks-root's public keys" "$GK" bao read transit/keys/workbricks-c1-jwks-root
deny "gatekeeper signs with release" "$GK" bao write transit/sign/workbricks-c1-release input=$IN
deny "gatekeeper signs with sha2-512" "$GK" bao write transit/sign/workbricks-c1-jwks-root/sha2-512 input=$IN
allow "signing cannot sign with release" "$SG" bao write transit/sign/workbricks-c1-release input=$IN
allow "signing cannot sign with audit" "$SG" bao write transit/sign/workbricks-c1-audit input=$IN
deny "signing signs with jwks-root" "$SG" bao write transit/sign/workbricks-c1-jwks-root input=$IN
for t in "$GK" "$SG" "$SC"; do
  for k in $KEYS; do
    deny "a cell policy exports $k" "$t" bao read "transit/export/signing-key/$k"
    deny "a cell policy backs up $k" "$t" bao read "transit/backup/$k"
    deny "a cell policy rotates $k" "$t" bao write -f "transit/keys/$k/rotate"
    deny "a cell policy reconfigures $k" "$t" bao write "transit/keys/$k/config" deletion_allowed=true
  done
  deny "a cell policy creates a key" "$t" bao write -f transit/keys/workbricks-c1-extra
done
printf '%s' v | bao kv put -mount=af workbricks/c1/probe value=- >/dev/null
printf '%s' v | bao kv put -mount=af workbricks/c2/probe value=- >/dev/null
printf '%s' v | bao kv put -mount=af estate/probe value=- >/dev/null
allow "secrets cannot read af/workbricks/c1/*" "$SC" bao kv get -mount=af workbricks/c1/probe
deny "secrets reads another cell's path" "$SC" bao kv get -mount=af workbricks/c2/probe
deny "secrets reads af/estate" "$SC" bao kv get -mount=af estate/probe
deny "secrets writes af/workbricks/c1/*" "$SC" bao kv put -mount=af workbricks/c1/probe value=x
deny "gatekeeper reads the cell's KV" "$GK" bao kv get -mount=af workbricks/c1/probe
deny "secrets signs" "$SC" bao write transit/sign/workbricks-c1-jwks-root input=$IN
echo "4: least privilege -> own keys only, public keys only, no export/backup/rotate/config, KV read on c1 only"

# 5. drifted policy -> refused before anything is rewritten
bao policy write workbricks-c1-secrets - >/dev/null <<'P'
path "af/data/workbricks/c1/*" { capabilities = ["read", "list"] }
P
bao write auth/kubernetes/role/workbricks-c1-secrets bound_service_account_names=marker bound_service_account_namespaces=workbricks-c1 token_policies=workbricks-c1-secrets >/dev/null
if run; then fail "a drifted policy must be refused"; fi
expect $N "REFUSING: policy workbricks-c1-secrets"
bao policy read workbricks-c1-secrets | grep -q '"list"' || fail "the drifted policy was rewritten anyway"
[ "$(field workbricks-c1-secrets bound_service_account_names)" = "[marker]" ] || fail "a role was rewritten after a refusal"
echo "5: drifted policy -> refused, nothing rewritten"

# 6. fresh vault, something else at transit/ -> refused, nothing written
fresh_vault
bao secrets enable -path=transit kv-v2 >/dev/null
if run; then fail "a non-transit engine at transit/ must be refused"; fi
expect $N "REFUSING: transit/ is mounted, but not as a transit engine"
if bao policy list | grep -q workbricks; then fail "a policy was written after the mount refusal"; fi
echo "6: non-transit engine at transit/ -> refused, nothing written"

# 7. fresh vault, a key pre-created exportable -> refused, no policy or role
fresh_vault
bao secrets enable -path=transit transit >/dev/null
bao write -f transit/keys/workbricks-c1-audit type=ecdsa-p256 exportable=true >/dev/null
if run; then fail "an exportable key must be refused"; fi
expect $N "REFUSING: transit key workbricks-c1-audit"
if bao policy list | grep -q workbricks; then fail "a policy was written after the key refusal"; fi
if bao list auth/kubernetes/role >/dev/null 2>&1; then fail "a role was written after the key refusal"; fi
echo "7: exportable key -> refused, no policy or role written"
echo "driver: all scenarios passed"
DRIVER

HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
chmod 755 "$WORK"; chmod 644 "$WORK/provision.sh" "$WORK/driver.sh"
docker run --rm -u 100:1000 -v "$HOSTWORK:/w:ro" -e HOME=/tmp --entrypoint sh "$IMAGE" /w/driver.sh
echo "test-workbricks-c1-provision: OK (create, keep, role drift, least privilege, policy drift, foreign mount, exportable key)"
