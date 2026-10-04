#!/bin/bash
# Fixtures for scripts/check-slot-enumerations.py — the gate that keeps the repo's dev-worker SLOT
# enumerations in step (ADR 0028). A consistency checker that cannot fail is worse than none: it
# reports OK while a retirement quietly misses a file. So this proves the FAILURE paths, not the
# happy one:
#   A. the repo as it stands passes;
#   B. a MISMATCHED enumeration fails (one slot removed from each source in turn);
#   C. a REFORMATTED/REMOVED source fails rather than dropping out of the comparison (a deleted
#      RoleBinding, a deleted env entry, a reflowed `for _n in (...)` line);
#   D. a slot that is both live and retired fails;
#   E. a project-scoped grant naming a slot that is not live fails (and a comment naming one does not);
#   F. and the inverse: a COMPLETE retirement (every live list, both RETIRED_SLOTS lists, the
#      per-project subjects) goes green — the stale scan must never make a correct retirement red.
# The checker is run against a COPY of the tree, so nothing here can touch the working repo.
# No docker, no cluster, no network. Exit non-zero on the first broken expectation.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK=scripts/check-slot-enumerations.py
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The files the checker reads, plus the script itself.
FILES=(
  "$CHECK"
  kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml
  kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml
  kubernetes/apps/infrastructure/platform-access/rbac.yaml
  kubernetes/apps/infrastructure/platform-access/pg-sync.yaml
  kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml
  kubernetes/apps/infrastructure/testpool/tep-access.yaml
  kubernetes/apps/infrastructure/helmtest/namespaces.yaml
  kubernetes/infra/dev-workers/variables.tf
  inventory/hosts.yml
  kubernetes/apps/clusters/ai/trueswarm-observer.yaml
  kubernetes/apps/clusters/ai/trueswarm-admin-observer.yaml
)
for f in "${FILES[@]}"; do
  mkdir -p "$WORK/$(dirname "$f")"
  cp "$REPO_ROOT/$f" "$WORK/$f"
done

run_check() { (cd "$WORK" && "$PY" "$CHECK" >"$WORK/.out" 2>&1); }   # status = the checker's
restore() { cp "$REPO_ROOT/$1" "$WORK/$1"; }
fail() { echo "FAIL: $*" >&2; [ -s "$WORK/.out" ] && sed 's/^/  | /' "$WORK/.out" >&2; exit 1; }
# edit <file> <python expression over `s`>  — rewrite a copied file
edit() {
  "$PY" - "$WORK/$1" "$2" <<'PY'
import io, sys
path, expr = sys.argv[1], sys.argv[2]
s = io.open(path, encoding="utf-8", newline="").read()
io.open(path, "w", encoding="utf-8", newline="").write(eval(expr))
PY
}
expect_fail() { # expect_fail <label> <substring the output must contain>
  if run_check; then fail "$1: the checker PASSED but should have failed"; fi
  grep -qF -- "$2" "$WORK/.out" || fail "$1: failed for the wrong reason (no '$2' in the output)"
  echo "  ok  $1"
}

echo "[A] the repo as it stands"
run_check || fail "the unmodified tree must pass"
grep -q "check-slot-enumerations: OK" "$WORK/.out" || fail "missing the OK line"
echo "  ok  unmodified tree passes"

echo "[B] a mismatched enumeration fails"
edit kubernetes/apps/infrastructure/platform-access/rbac.yaml \
  's.replace("  - { kind: ServiceAccount, name: platform-dw4, namespace: platform-access }\n", "", 1)'
expect_fail "a RoleBinding missing one subject" "DIFF"
restore kubernetes/apps/infrastructure/platform-access/rbac.yaml

edit kubernetes/apps/infrastructure/testpool/tep-access.yaml \
  's.replace("metadata: { name: tep-dw4, namespace: testpool }", "metadata: { name: tep-dwX, namespace: testpool }", 1)'
expect_fail "a tep ServiceAccount removed" "DIFF"
restore kubernetes/apps/infrastructure/testpool/tep-access.yaml

edit inventory/hosts.yml 's.replace("        dev-worker-4:\n", "", 1)'
expect_fail "an inventory host removed" "DIFF"
restore inventory/hosts.yml

edit kubernetes/infra/dev-workers/variables.tf \
  's.replace(chr(34) + "dev-worker-4" + chr(34) + " = {", chr(34) + "dev-worker-9" + chr(34) + " = {", 1)'
expect_fail "a tofu map key renumbered" "DIFF"
restore kubernetes/infra/dev-workers/variables.tf

echo "[C] a removed or reformatted source fails instead of dropping out"
edit kubernetes/apps/infrastructure/platform-access/rbac.yaml \
  's[:s.index("kind: RoleBinding\nmetadata:\n  name: dev-worker-platform-observer\n  namespace: platform-edge") - 4]'
expect_fail "a whole RoleBinding deleted" "no dev-worker-platform-observer RoleBinding for namespace platform-edge"
restore kubernetes/apps/infrastructure/platform-access/rbac.yaml

edit kubernetes/apps/infrastructure/platform-access/pg-sync.yaml \
  's.replace(chr(34) + "1 2 3 4" + chr(34), chr(34) + "1 2 3" + chr(34), 1)'
expect_fail "the CronJob and bootstrap Job disagreeing on LIVE_SLOTS" "disagree on LIVE_SLOTS"
restore kubernetes/apps/infrastructure/platform-access/pg-sync.yaml

edit kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml \
  's.replace(chr(34) + "1 2 3 4" + chr(34), chr(34) + "1 2 3" + chr(34))'
expect_fail "the e2e token sync dropping a live slot from both copies" "DIFF"
restore kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml

edit kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml \
  's.replace(chr(34) + "1 2 3 4" + chr(34), chr(34) + "1 2 3" + chr(34), 1)'
expect_fail "the e2e token sync CronJob and bootstrap Job disagreeing" "disagree on LIVE_SLOTS"
restore kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml

edit kubernetes/apps/infrastructure/platform-access/pg-sync.yaml \
  's.replace("                - { name: RETIRED_SLOTS, value: " + chr(34) + "5 6" + chr(34) + " }", "", 1)'
expect_fail "one RETIRED_SLOTS env entry deleted" "RETIRED_SLOTS env entries, expected 2"
restore kubernetes/apps/infrastructure/platform-access/pg-sync.yaml

edit kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml \
  's.replace("    for _n in (1, 2, 3, 4):", "    for _n in LIVE:  # reformatted away from a literal")'
expect_fail "the sync loop reformatted" "no match for"
restore kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml

echo "[D] a slot that is both live and retired fails"
edit kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml \
  's.replace("RETIRED_SLOTS=" + chr(34) + "dev-worker-5 dev-worker-6" + chr(34), "RETIRED_SLOTS=" + chr(34) + "dev-worker-4 dev-worker-5 dev-worker-6" + chr(34), 1)'
edit kubernetes/apps/infrastructure/platform-access/pg-sync.yaml \
  's.replace("{ name: RETIRED_SLOTS, value: " + chr(34) + "5 6" + chr(34) + " }", "{ name: RETIRED_SLOTS, value: " + chr(34) + "4 5 6" + chr(34) + " }")'
expect_fail "slot 4 listed as both live and retired" "both live and retired"
restore kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml
restore kubernetes/apps/infrastructure/platform-access/pg-sync.yaml

echo "[E] a project-scoped grant must name only live slots"
# 99 is far above any slot ever provisioned (dev-worker-1..6, vmids 4201-4206), so it is never live;
# using it keeps these fixtures independent of whichever slots are live or retired at the time.
edit kubernetes/apps/clusters/ai/trueswarm-observer.yaml \
  's.replace("  name: platform-dw2\n", "  name: platform-dw99\n", 1)'
expect_fail "a per-project RoleBinding naming a retired slot" "STALE"
restore kubernetes/apps/clusters/ai/trueswarm-observer.yaml

edit kubernetes/apps/clusters/ai/trueswarm-observer.yaml \
  's.replace("  name: platform-dw2\n", "  name: " + chr(34) + "platform-dw99" + chr(34) + "\n", 1)'
expect_fail "a QUOTED retired-slot subject name" "STALE"
restore kubernetes/apps/clusters/ai/trueswarm-observer.yaml

edit kubernetes/apps/clusters/ai/trueswarm-observer.yaml \
  's.replace("  name: platform-dw2\n", "  name:    platform-dw99\n", 1)'
expect_fail "a retired-slot subject with extra whitespace" "STALE"
restore kubernetes/apps/clusters/ai/trueswarm-observer.yaml

edit kubernetes/apps/clusters/ai/trueswarm-observer.yaml \
  's.replace("- kind: ServiceAccount\n  name: platform-dw2\n  namespace: platform-access\n", "- { apiGroup: rbac.authorization.k8s.io, kind: User, name: system:serviceaccount:platform-access:platform-dw99 }\n", 1)'
expect_fail "a retired slot as a kind: User subject" "STALE"
restore kubernetes/apps/clusters/ai/trueswarm-observer.yaml

edit kubernetes/apps/clusters/ai/trueswarm-admin-observer.yaml \
  's.replace("  name: platform-dw2\n", "  name: platform-dw99\n", 1)'
expect_fail "the admin observer's RoleBinding naming a non-live slot" "STALE"
restore kubernetes/apps/clusters/ai/trueswarm-admin-observer.yaml

edit kubernetes/apps/clusters/ai/trueswarm-observer.yaml \
  's + "# name: platform-dw99 (a comment, not a subject)\n"'
run_check || fail "a COMMENT naming a non-live slot must not fail the check"
echo "  ok  a comment naming a non-live slot is ignored"
restore kubernetes/apps/clusters/ai/trueswarm-observer.yaml

echo "[F] a complete retirement of slot 4 passes"
RBAC=kubernetes/apps/infrastructure/platform-access/rbac.yaml
PGSYNC=kubernetes/apps/infrastructure/platform-access/pg-sync.yaml
K8STOKEN=kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml
PROVISION=kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml
TEP=kubernetes/apps/infrastructure/testpool/tep-access.yaml
HELMTEST=kubernetes/apps/infrastructure/helmtest/namespaces.yaml
TOFU=kubernetes/infra/dev-workers/variables.tf
INVENTORY=inventory/hosts.yml
TS=kubernetes/apps/clusters/ai/trueswarm-observer.yaml
TSA=kubernetes/apps/clusters/ai/trueswarm-admin-observer.yaml
E2E=kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml
# edit_all <file> <old> <new> <expected count>: replace every occurrence, asserting how many there were
edit_all() {
  "$PY" - "$WORK/$1" "$2" "$3" "$4" <<'PY' || fail "[F] fixture edit did not match: $1"
import io, sys
path, old, new, n = sys.argv[1], sys.argv[2].encode().decode("unicode_escape"), sys.argv[3].encode().decode("unicode_escape"), int(sys.argv[4])
s = io.open(path, encoding="utf-8", newline="").read()
if s.count(old) != n:
    sys.exit("%s: expected %d x %r, found %d" % (path, n, old, s.count(old)))
io.open(path, "w", encoding="utf-8", newline="").write(s.replace(old, new))
PY
}
edit_all "$RBAC" 'apiVersion: v1\nkind: ServiceAccount\nmetadata: { name: platform-dw4, namespace: platform-access }\nautomountServiceAccountToken: false\n---\n' '' 1
edit_all "$RBAC" ', "platform-dw4"' '' 1
edit_all "$RBAC" '  - { kind: ServiceAccount, name: platform-dw4, namespace: platform-access }\n' '' 3
edit_all "$PGSYNC" 'value: "1 2 3 4"' 'value: "1 2 3"' 2
edit_all "$PGSYNC" 'value: "5 6"' 'value: "4 5 6"' 2
edit_all "$K8STOKEN" 'for _n in (1, 2, 3, 4):' 'for _n in (1, 2, 3):' 1
edit_all "$PROVISION" 'for host in dev-worker-1 dev-worker-2 dev-worker-3 dev-worker-4; do' 'for host in dev-worker-1 dev-worker-2 dev-worker-3; do' 1
edit_all "$PROVISION" 'RETIRED_SLOTS="dev-worker-5 dev-worker-6"' 'RETIRED_SLOTS="dev-worker-4 dev-worker-5 dev-worker-6"' 1
edit_all "$TEP" 'metadata: { name: tep-dw4, namespace: testpool }' 'metadata: { name: tep-retired, namespace: testpool }' 1
edit_all "$HELMTEST" 'kind: Namespace\nmetadata:\n  name: helmtest-dw4\n' 'kind: Namespace\nmetadata:\n  name: helmtest-retired\n' 1
edit_all "$TOFU" '"dev-worker-4" = {' '"retired-4" = {' 1
edit_all "$INVENTORY" '        dev-worker-4:\n' '' 1
edit_all "$TS" '- kind: ServiceAccount\n  name: platform-dw4\n  namespace: platform-access\n' '' 1
edit_all "$TSA" '- kind: ServiceAccount\n  name: platform-dw4\n  namespace: platform-access\n' '' 1
edit_all "$E2E" '{ name: LIVE_SLOTS, value: "1 2 3 4" }' '{ name: LIVE_SLOTS, value: "1 2 3" }' 2
run_check || fail "a complete retirement of slot 4 must pass"
grep -q "live slots (reference): \[1, 2, 3\]" "$WORK/.out" || fail "[F] the retirement did not take effect"
grep -q "retired slots (reference): \[4, 5, 6\]" "$WORK/.out" || fail "[F] slot 4 is not recorded as retired"
echo "  ok  retiring slot 4 everywhere goes green (live [1, 2, 3], retired [4, 5, 6])"
for f in "$RBAC" "$PGSYNC" "$K8STOKEN" "$PROVISION" "$TEP" "$HELMTEST" "$TOFU" "$INVENTORY" "$TS" "$TSA" "$E2E"; do restore "$f"; done

run_check || fail "the tree must pass again after every fixture is restored"
echo "test-check-slot-enumerations: OK (mismatch, removal, reformat and live/retired overlap all fail closed)"
