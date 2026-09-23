#!/bin/bash
# Fixtures for scripts/check-slot-enumerations.py — the gate that keeps the repo's dev-worker SLOT
# enumerations in step (ADR 0028). A consistency checker that cannot fail is worse than none: it
# reports OK while a retirement quietly misses a file. So this proves the FAILURE paths, not the
# happy one:
#   A. the repo as it stands passes;
#   B. a MISMATCHED enumeration fails (one slot removed from each source in turn);
#   C. a REFORMATTED/REMOVED source fails rather than dropping out of the comparison (a deleted
#      RoleBinding, a deleted env entry, a reflowed `for _n in (...)` line);
#   D. a slot that is both live and retired fails.
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
  kubernetes/apps/infrastructure/testpool/tep-access.yaml
  kubernetes/apps/infrastructure/helmtest/namespaces.yaml
  kubernetes/infra/dev-workers/variables.tf
  inventory/hosts.yml
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

run_check || fail "the tree must pass again after every fixture is restored"
echo "test-check-slot-enumerations: OK (mismatch, removal, reformat and live/retired overlap all fail closed)"
