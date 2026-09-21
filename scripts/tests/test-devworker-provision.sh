#!/bin/bash
# Dry-run of the openbao-devworker-provision script (the ConfigMap in
# kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml) under the Job's OWN
# image (busybox sh + the real `bao` binary shadowed by a stub on PATH), against canned OpenBao
# answers. It proves the RETIRED_SLOTS step's contract without a vault:
#   run 1: every SecretID accessor of the retired role destroyed, ONLY the tokens whose metadata names
#          that role revoked, role + policy deleted, KV descendants + metadata deleted, a seed named
#          after the retired slot skipped, the live slots still upserted;
#   run 2: converged — nothing destroyed again, the seed still skipped, exit 0;
#   and NO credential value ever reaches stdout/stderr (the stub plants a canary secret-id value).
# Requires docker (the manifests CI job has it). Exit non-zero on the first broken expectation.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

IMAGE="$(sed -n 's/^ *image: *\(quay.io\/openbao\/openbao:[^ ]*\).*/\1/p' "$MANIFEST" | head -1)"
[ -n "$IMAGE" ] || { echo "cannot find the Job image in $MANIFEST" >&2; exit 1; }

# python3 on the runner, python on the operator's Windows Git Bash (same fallback as manifest-lint.sh)
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
"$PY" - "$MANIFEST" "$WORK/provision.sh" <<'PY'
import sys, yaml
manifest, out = sys.argv[1:3]
docs = [d for d in yaml.safe_load_all(open(manifest, encoding="utf-8")) if d]
cm = next(d for d in docs if d.get("kind") == "ConfigMap" and "provision.sh" in d.get("data", {}))
open(out, "w", encoding="utf-8", newline="\n").write(cm["data"]["provision.sh"])
PY

mkdir -p "$WORK/stub" "$WORK/seeds"
echo '{"gitea_pat":"x"}' > "$WORK/seeds/common.json"
echo '{"strive_test_user":"x"}' > "$WORK/seeds/dev-worker-3.json"
echo '{"stale":"x"}' > "$WORK/seeds/dev-worker-6.json"   # names a retired slot: must be skipped

# The stub answers like bao 2.6.x would (JSON lists, a role that disappears once deleted, KV
# descendants that disappear once their metadata is deleted) and logs every call. The canary
# CANARY-SECRET-ID-VALUE is what a `bao write .../secret-id` response would carry; nothing in the
# provision script may ever print it.
cat > "$WORK/stub/bao" <<'STUB'
#!/bin/sh
STATE=/state; LOG=/state/calls.log
echo "bao $*" >> "$LOG"
case "$1 $2" in
  "token lookup")
    case "$*" in
      *"-accessor t2"*) echo '{"data": {"accessor": "t2", "meta": {"role_name": "dev-worker-6"}}}' ;;
      *"-accessor "*)   echo '{"data": {"accessor": "tX", "meta": {"role_name": "dev-worker-1"}}}' ;;
      *) echo '{"data": {"id": "root"}}' ;;
    esac ;;
  "auth list") echo '{"approle/": {"type": "approle"}}' ;;
  "policy write"|"policy delete"|"token revoke") exit 0 ;;
  "write "*)
    case "$3" in
      auth/approle/role/*/secret-id) echo '{"data": {"secret_id": "CANARY-SECRET-ID-VALUE", "secret_id_accessor": "acc-new"}}' ;;
      *) exit 0 ;;
    esac ;;
  "read auth/approle/role/dev-worker-6") [ -f "$STATE/role-gone" ] && { echo "no role" >&2; exit 2; } || exit 0 ;;
  "delete auth/approle/role/dev-worker-6") touch "$STATE/role-gone"; exit 0 ;;
  "list -format=json")
    case "$3" in
      auth/approle/role/dev-worker-6/secret-id) echo '["acc1", "acc2"]' ;;
      auth/token/accessors) echo '["t1", "t2", "t3"]' ;;
      *) echo "no list" >&2; exit 2 ;;
    esac ;;
  "kv list") [ -f "$STATE/kid-gone" ] && { echo "No value found" >&2; exit 2; } || echo '["sub1"]' ;;
  "kv metadata")
    case "$3 $4 $5" in
      "get -mount=af dev-workers/dev-worker-6") [ -f "$STATE/kv-gone" ] && exit 2 || exit 0 ;;
      "delete -mount=af dev-workers/dev-worker-6/sub1") touch "$STATE/kid-gone"; exit 0 ;;
      "delete -mount=af dev-workers/dev-worker-6") touch "$STATE/kv-gone"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  "kv get") echo "No value found at af/data/$4" >&2; exit 2 ;;
  "kv put"|"kv patch") exit 0 ;;
  *) echo "stub: unhandled: bao $*" >&2; exit 3 ;;
esac
STUB
chmod +x "$WORK/stub/bao"

# Docker Desktop from Git Bash needs a Windows-style host path and no MSYS path mangling.
HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi

run() {  # $1 = run number; prints the script's combined output
  docker run --rm -v "$HOSTWORK:/w" -v "$HOSTWORK/state:/state" -e BAO_TOKEN=stub -e BAO_ADDR=http://stub \
    --entrypoint /bin/sh "$IMAGE" -c '
      export PATH=/w/stub:$PATH
      sed "s#SEED_DIR=/etc/openbao-devworker-seeds#SEED_DIR=/w/seeds#" /w/provision.sh > /tmp/p.sh
      sh /tmp/p.sh 2>&1'
}
mkdir -p "$WORK/state"
out1="$(run 1)"; echo "$out1" | sed 's/^/  run1: /'
out2="$(run 2)"; echo "$out2" | sed 's/^/  run2: /'
log="$WORK/state/calls.log"

expect() { grep -qF -- "$2" <<<"$1" || { echo "MISSING: $2" >&2; exit 1; }; }
forbid() { grep -qF -- "$2" <<<"$1" && { echo "FORBIDDEN OUTPUT: $2" >&2; exit 1; } || true; }

expect "$out1" "retired dev-worker-6: 2 secret-id accessors destroyed, 1 tokens revoked, role/policy deleted"
expect "$out1" "retired dev-worker-6: KV descendant sub1 metadata deleted"
expect "$out1" "retired dev-worker-6: KV metadata deleted"
expect "$out1" "seed dev-worker-6.json names a retired slot; skipped"
expect "$out1" "devworker provision complete"
expect "$out2" "retired dev-worker-6: role already absent (converged)"
expect "$out2" "seed dev-worker-6.json names a retired slot; skipped"
expect "$out2" "devworker provision complete"
forbid "$out1$out2" "CANARY-SECRET-ID-VALUE"

calls="$(cat "$log")"
expect "$calls" "bao write auth/approle/role/dev-worker-6/secret-id-accessor/destroy secret_id_accessor=acc1"
expect "$calls" "bao write auth/approle/role/dev-worker-6/secret-id-accessor/destroy secret_id_accessor=acc2"
expect "$calls" "bao token revoke -accessor t2"
forbid "$calls" "bao token revoke -accessor t1"
forbid "$calls" "bao token revoke -accessor t3"
forbid "$calls" "bao token revoke -mode"
expect "$calls" "bao delete auth/approle/role/dev-worker-6"
expect "$calls" "bao policy delete dev-worker-6"
expect "$calls" "bao kv metadata delete -mount=af dev-workers/dev-worker-6/sub1"
expect "$calls" "bao kv metadata delete -mount=af dev-workers/dev-worker-6"
[ "$(grep -c 'bao delete auth/approle/role/dev-worker-6' "$log")" = 1 ] || { echo "role deleted more than once" >&2; exit 1; }
[ "$(grep -c 'bao kv metadata delete -mount=af dev-workers/dev-worker-6$' "$log")" = 1 ] || { echo "KV metadata deleted more than once" >&2; exit 1; }
forbid "$calls" "bao kv patch -mount=af dev-workers/dev-worker-6"
forbid "$calls" "bao kv put -cas=0 -mount=af dev-workers/dev-worker-6 "
expect "$calls" "bao kv put -cas=0 -mount=af dev-workers/dev-worker-3"
# the live slots are still provisioned, the retired one is not re-created
for h in dev-worker-1 dev-worker-2 dev-worker-3 dev-worker-4 dev-worker-5; do expect "$calls" "bao write auth/approle/role/$h "; done
forbid "$calls" "bao write auth/approle/role/dev-worker-6 "
forbid "$calls" "bao policy write dev-worker-6"
echo "test-devworker-provision: OK (retired-slot revocation is complete, idempotent and silent about values)"
