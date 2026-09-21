#!/bin/bash
# Dry-run of the openbao-devworker-provision script (the ConfigMap in
# kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml) under the Job's OWN
# image (busybox sh + the real `bao` binary shadowed by a stub on PATH), against canned OpenBao
# answers. It proves the RETIRED_SLOTS step's contract without a vault:
#   A. run 1: every SecretID accessor of the retired role destroyed, ONLY the tokens whose metadata
#      names that role revoked, role + policy deleted, the KV subtree purged RECURSIVELY (a nested
#      folder `sub1/` with a leaf under it, plus a flat leaf), a seed named after the retired slot
#      skipped, the live slots still upserted; run 2: converged — nothing destroyed again, exit 0;
#      and NO credential value ever reaches stdout/stderr (the stub plants a canary secret-id value).
#   B. partial failure: `bao policy delete` fails once AFTER the role was deleted -> run 1 exits
#      non-zero; run 2 (role already absent) still deletes the policy — cleanup is resumable and is
#      never reported "converged" while something is left.
#   C. a token-accessor listing failure aborts the run BEFORE the role is deleted (never "0 tokens
#      revoked" on a failed enumeration); D. the same for the secret-id listing; E. a failed KV
#      listing aborts the run (the KV purge is never reported done) and the rerun purges the subtree.
#   The stub mimics the real CLI surface: generic `bao list` rejects `-mount` (only `bao kv list`
#   takes it), so a traversal built on the wrong command fails the test instead of passing it.
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

# The stub answers like bao 2.6.x would (JSON lists, "No value found" for empty/absent paths, a role
# that disappears once deleted, KV entries that disappear once their metadata is deleted, a folder
# listed as `sub1/`) and logs every call to /state/calls.log. Fault injection via marker files in
# /state: FAIL_POLICY_DELETE_ONCE, FAIL_ACCESSOR_LIST_ONCE, FAIL_SID_LIST_ONCE, FAIL_KV_LIST_ONCE. The canary CANARY-SECRET-ID-VALUE is what
# a `bao write .../secret-id` response would carry; nothing in the provision script may print it.
cat > "$WORK/stub/bao" <<'STUB'
#!/bin/sh
STATE=/state; LOG=/state/calls.log
echo "bao $*" >> "$LOG"
nvf() { echo "No value found at $1" >&2; exit 2; }
case "$1 $2" in
  "token lookup")
    case "$*" in
      *"-accessor t2"*) echo '{"data": {"accessor": "t2", "meta": {"role_name": "dev-worker-6"}}}' ;;
      *"-accessor t3"*) echo "token not found" >&2; exit 2 ;;   # expired between list and lookup
      *"-accessor "*)   echo '{"data": {"accessor": "tX", "meta": {"role_name": "dev-worker-1"}}}' ;;
      *) echo '{"data": {"id": "root"}}' ;;
    esac ;;
  "auth list") echo '{"approle/": {"type": "approle"}}' ;;
  "policy write") exit 0 ;;
  "token revoke") touch "$STATE/tok-gone-$4"; exit 0 ;;   # a revoked accessor leaves the list
  "policy delete")
    if [ -f "$STATE/FAIL_POLICY_DELETE_ONCE" ]; then rm -f "$STATE/FAIL_POLICY_DELETE_ONCE"; echo "Error deleting policy: connection reset" >&2; exit 2; fi
    touch "$STATE/policy-gone"; exit 0 ;;
  "write "*)
    case "$2" in
      auth/approle/role/*/secret-id) echo '{"data": {"secret_id": "CANARY-SECRET-ID-VALUE", "secret_id_accessor": "acc-new"}}' ;;
      auth/approle/role/dev-worker-6/secret-id-accessor/destroy) touch "$STATE/sids-gone-${3#secret_id_accessor=}"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  "delete auth/approle/role/dev-worker-6") touch "$STATE/role-gone"; exit 0 ;;
  "list -format=json")   # generic list: `bao list -format=json <path>` — NO -mount flag here
    case "$3" in
      -mount*|-*) echo "flag provided but not defined: ${3%%=*}" >&2; exit 1 ;;
      auth/approle/role/dev-worker-6/secret-id)
        if [ -f "$STATE/FAIL_SID_LIST_ONCE" ]; then rm -f "$STATE/FAIL_SID_LIST_ONCE"; echo "Error listing $3: connection refused" >&2; exit 2; fi
        { [ -f "$STATE/sids-gone-acc1" ] && [ -f "$STATE/sids-gone-acc2" ]; } && nvf "$3" || echo '["acc1", "acc2"]' ;;
      auth/token/accessors)
        if [ -f "$STATE/FAIL_ACCESSOR_LIST_ONCE" ]; then rm -f "$STATE/FAIL_ACCESSOR_LIST_ONCE"; echo "Error listing auth/token/accessors: Vault is sealed" >&2; exit 2; fi
        [ -f "$STATE/tok-gone-t2" ] && echo '["t1", "t3"]' || echo '["t1", "t2", "t3"]' ;;
      *) nvf "$3" ;;
    esac ;;
  "kv list")             # KV v2: `bao kv list -format=json -mount=af <path>`
    [ "$3" = "-format=json" ] && [ "$4" = "-mount=af" ] || { echo "stub: unexpected kv list form: $*" >&2; exit 3; }
    case "$5" in
      dev-workers/dev-worker-6/)
        if [ -f "$STATE/FAIL_KV_LIST_ONCE" ]; then rm -f "$STATE/FAIL_KV_LIST_ONCE"; echo "Error listing af/metadata/$5: Vault is sealed" >&2; exit 2; fi
        { [ -f "$STATE/kv-flat-gone" ] && [ -f "$STATE/kv-sub-gone" ]; } && nvf "$5" || echo '["flat", "sub1/"]' ;;
      dev-workers/dev-worker-6/sub1/) [ -f "$STATE/kv-sub-gone" ] && nvf "$5" || echo '["credential"]' ;;
      *) nvf "$5" ;;
    esac ;;
  "kv metadata")
    case "$3 $4 $5" in
      "get -mount=af dev-workers/dev-worker-6") [ -f "$STATE/kv-gone" ] && exit 2 || exit 0 ;;
      "get -mount=af dev-workers/dev-worker-6/sub1") exit 2 ;;  # a pure folder, not a secret
      "delete -mount=af dev-workers/dev-worker-6/flat") touch "$STATE/kv-flat-gone"; exit 0 ;;
      "delete -mount=af dev-workers/dev-worker-6/sub1/credential") touch "$STATE/kv-sub-gone"; exit 0 ;;
      "delete -mount=af dev-workers/dev-worker-6") touch "$STATE/kv-gone"; exit 0 ;;
      *) echo "stub: unexpected kv metadata op: $*" >&2; exit 3 ;;
    esac ;;
  "kv get") nvf "af/data/$4" ;;
  "kv put"|"kv patch") exit 0 ;;
  *) echo "stub: unhandled: bao $*" >&2; exit 3 ;;
esac
STUB
chmod +x "$WORK/stub/bao"

# Docker Desktop from Git Bash needs a Windows-style host path and no MSYS path mangling.
HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi

# The image runs as its own `openbao` user (uid 100) and mktemp dirs are 0700: open the scratch tree
# for reading and the state dir for the stub's call log and fault markers.
mkdir -p "$WORK/state"
chmod 755 "$WORK" "$WORK/stub" "$WORK/seeds" "$WORK/stub/bao"
chmod 644 "$WORK/provision.sh" "$WORK/seeds"/*.json
chmod 777 "$WORK/state"

run() {  # prints the script's combined output; returns its exit status
  docker run --rm -v "$HOSTWORK:/w" -v "$HOSTWORK/state:/state" -e BAO_TOKEN=stub -e BAO_ADDR=http://stub \
    --entrypoint /bin/sh "$IMAGE" -c '
      export PATH=/w/stub:$PATH
      sed "s#SEED_DIR=/etc/openbao-devworker-seeds#SEED_DIR=/w/seeds#" /w/provision.sh > /tmp/p.sh
      sh /tmp/p.sh 2>&1'
}
reset_state() { rm -rf "$WORK/state"; mkdir -p "$WORK/state"; chmod 777 "$WORK/state"; }
expect() { grep -qF -- "$2" <<<"$1" || { echo "MISSING: $2" >&2; exit 1; }; }
forbid() { grep -qF -- "$2" <<<"$1" && { echo "FORBIDDEN: $2" >&2; exit 1; } || true; }
count()  { grep -cF -- "$2" <<<"$1" || true; }
forbid_line() { grep -qxF -- "$2" <<<"$1" && { echo "FORBIDDEN LINE: $2" >&2; exit 1; } || true; }

# ---- A. happy path, then convergence -------------------------------------------------------------
reset_state
out1="$(run)"; rc1=$?; echo "$out1" | sed 's/^/  A1: /'; [ "$rc1" = 0 ] || { echo "run A1 exit $rc1" >&2; exit 1; }
out2="$(run)"; rc2=$?; echo "$out2" | sed 's/^/  A2: /'; [ "$rc2" = 0 ] || { echo "run A2 exit $rc2" >&2; exit 1; }
calls="$(cat "$WORK/state/calls.log")"
expect "$out1" "retired dev-worker-6: 2 secret-id accessors destroyed, 1 tokens revoked, role/policy deleted"
expect "$out1" "retired KV leaf dev-workers/dev-worker-6/flat: metadata deleted"
expect "$out1" "retired KV leaf dev-workers/dev-worker-6/sub1/credential: metadata deleted"
expect "$out1" "retired KV dev-workers/dev-worker-6: metadata deleted"
expect "$out1" "seed dev-worker-6.json names a retired slot; skipped"
expect "$out1" "devworker provision complete"
expect "$out2" "retired dev-worker-6: converged (no secret-ids, no tokens; role/policy/KV absent)"
expect "$out2" "seed dev-worker-6.json names a retired slot; skipped"
expect "$out2" "devworker provision complete"
forbid "$out1$out2" "CANARY-SECRET-ID-VALUE"
expect "$calls" "bao write auth/approle/role/dev-worker-6/secret-id-accessor/destroy secret_id_accessor=acc1"
expect "$calls" "bao write auth/approle/role/dev-worker-6/secret-id-accessor/destroy secret_id_accessor=acc2"
expect "$calls" "bao token revoke -accessor t2"
forbid "$calls" "bao token revoke -accessor t1"
forbid "$calls" "bao token revoke -accessor t3"
forbid "$calls" "bao token revoke -mode"
expect "$calls" "bao kv metadata delete -mount=af dev-workers/dev-worker-6/sub1/credential"
# a folder entry is recursed into, never handed to `metadata delete` as a path (whole-line checks)
forbid_line "$calls" "bao kv metadata delete -mount=af dev-workers/dev-worker-6/sub1/"
forbid_line "$calls" "bao kv metadata delete -mount=af dev-workers/dev-worker-6/sub1"
[ "$(count "$calls" 'bao kv metadata delete -mount=af dev-workers/dev-worker-6/flat')" = 1 ] || { echo "flat leaf deleted != once" >&2; exit 1; }
[ "$(count "$calls" 'bao kv metadata delete -mount=af dev-workers/dev-worker-6/sub1/credential')" = 1 ] || { echo "nested leaf deleted != once" >&2; exit 1; }
[ "$(grep -c 'bao kv metadata delete -mount=af dev-workers/dev-worker-6$' "$WORK/state/calls.log")" = 1 ] || { echo "KV root metadata deleted != once" >&2; exit 1; }
# the deletes are unconditional and idempotent: both runs issue them, neither run fails on absence
[ "$(count "$calls" 'bao delete auth/approle/role/dev-worker-6')" = 2 ] || { echo "role delete not issued on both runs" >&2; exit 1; }
[ "$(count "$calls" 'bao policy delete dev-worker-6')" = 2 ] || { echo "policy delete not issued on both runs" >&2; exit 1; }
forbid "$calls" "bao kv patch -mount=af dev-workers/dev-worker-6"
forbid "$calls" "bao kv put -cas=0 -mount=af dev-workers/dev-worker-6 "
expect "$calls" "bao kv put -cas=0 -mount=af dev-workers/dev-worker-3"
for h in dev-worker-1 dev-worker-2 dev-worker-3 dev-worker-4 dev-worker-5; do expect "$calls" "bao write auth/approle/role/$h "; done
# Both sync-owned writers get a k8s-auth role on the same KV-write policy (ADR 0021 / ADR 0028).
expect "$calls" "bao write auth/kubernetes/role/k8stoken-sync bound_service_account_names=openbao-k8stoken-sync bound_service_account_namespaces=openbao token_policies=k8stoken-sync"
expect "$calls" "bao write auth/kubernetes/role/platform-pg-sync bound_service_account_names=openbao-platform-pg-sync bound_service_account_namespaces=strive-ailab token_policies=k8stoken-sync"
forbid "$calls" "bao write auth/approle/role/dev-worker-6 "
forbid "$calls" "bao policy write dev-worker-6"

# ---- B. partial failure after the role delete: the rerun finishes the job -----------------------
reset_state; touch "$WORK/state/FAIL_POLICY_DELETE_ONCE"
set +e; outB1="$(run)"; rcB1=$?; set -e; echo "$outB1" | sed 's/^/  B1: /'
[ "$rcB1" != 0 ] || { echo "B1 must fail when the policy delete fails" >&2; exit 1; }
forbid "$outB1" "converged"
[ -f "$WORK/state/role-gone" ] || { echo "B1: the role should already be gone when the policy delete failed" >&2; exit 1; }
[ ! -f "$WORK/state/policy-gone" ] || { echo "B1: policy must not be gone yet" >&2; exit 1; }
outB2="$(run)"; echo "$outB2" | sed 's/^/  B2: /'
[ -f "$WORK/state/policy-gone" ] || { echo "B2 did not delete the policy left behind" >&2; exit 1; }
expect "$outB2" "devworker provision complete"

# ---- D. a failed secret-id listing aborts BEFORE anything irreversible -------------------------
reset_state; touch "$WORK/state/FAIL_SID_LIST_ONCE"
set +e; outD="$(run)"; rcD=$?; set -e; echo "$outD" | sed 's/^/  D: /'
[ "$rcD" != 0 ] || { echo "D must fail when the secret-id listing fails" >&2; exit 1; }
expect "$outD" "failed and was not a not-found; aborting"
[ ! -f "$WORK/state/role-gone" ] || { echo "D: the role must NOT be deleted after a failed secret-id enumeration" >&2; exit 1; }
forbid "$outD" "converged"

# ---- E. a failed KV listing aborts the run; the rerun purges the subtree -----------------------
reset_state; touch "$WORK/state/FAIL_KV_LIST_ONCE"
set +e; outE1="$(run)"; rcE1=$?; set -e; echo "$outE1" | sed 's/^/  E1: /'
[ "$rcE1" != 0 ] || { echo "E1 must fail when the KV listing fails" >&2; exit 1; }
forbid "$outE1" "retired KV dev-workers/dev-worker-6: metadata deleted"
forbid "$outE1" "converged"
[ ! -f "$WORK/state/kv-sub-gone" ] || { echo "E1: nothing under the subtree may be deleted after a failed listing" >&2; exit 1; }
outE2="$(run)"; echo "$outE2" | sed 's/^/  E2: /'
expect "$outE2" "retired KV leaf dev-workers/dev-worker-6/sub1/credential: metadata deleted"
expect "$outE2" "retired KV dev-workers/dev-worker-6: metadata deleted"
expect "$outE2" "devworker provision complete"

# ---- C. a failed token-accessor listing aborts BEFORE anything irreversible ---------------------
reset_state; touch "$WORK/state/FAIL_ACCESSOR_LIST_ONCE"
set +e; outC="$(run)"; rcC=$?; set -e; echo "$outC" | sed 's/^/  C: /'
[ "$rcC" != 0 ] || { echo "C must fail when the accessor listing fails" >&2; exit 1; }
expect "$outC" "bao list -format=json auth/token/accessors failed and was not a not-found; aborting"
[ ! -f "$WORK/state/role-gone" ] || { echo "C: the role must NOT be deleted after a failed enumeration" >&2; exit 1; }
forbid "$outC" "0 tokens revoked"
forbid "$outC" "converged"

echo "test-devworker-provision: OK (recursive, resumable, fail-closed, silent about values)"
