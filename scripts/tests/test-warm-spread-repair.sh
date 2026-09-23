#!/usr/bin/env bash
# test-warm-spread-repair.sh — the warm-spread-repair script (kubernetes/apps/infrastructure/testpool/
# warm-spread-repair.yaml, ConfigMap key repair.sh) under the CronJob's OWN image (busybox ash in
# docker.io/alpine/k8s, digest-pinned in the manifest) with `kubectl` and `curl` shadowed by stubs
# that answer canned listings IN THE SHAPES THE REAL API PRODUCES (tab-delimited; a schedulable node
# has NO spec.unschedulable field, a Pending pod has no nodeName) and record every delete:
#   A. one warm member per node                      -> "spread ok", no delete
#   B. two on node-b, none on node-a                 -> the YOUNGEST member on node-b is deleted, once,
#                                                       with the resourceVersion precondition just read
#   C. node-a NotReady, then cordoned (`true`)       -> not eligible, nothing deleted
#   D. the pool asks for one member                  -> nothing to spread
#   E. a failed pod listing                          -> non-zero exit, nothing deleted (fail closed)
#   F. DRY_RUN=1                                     -> "would delete", nothing deleted
#   G. a Pending member (no node) beside two on b    -> ignored for the count; b's youngest goes
#   H. the victim's Sandbox is already a claim's     -> stale pod label: skipped, nothing deleted
#   I. adoption between the check and the delete     -> the API answers 409: skipped, exit 0
#   J. the delete answers an unexpected status        -> non-zero exit (a visible Job failure)
# FAIL CLOSED like scripts/manifest-lint.sh: set -euo pipefail, no soft skips; docker required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$REPO_ROOT/kubernetes/apps/infrastructure/testpool/warm-spread-repair.yaml"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }

IMAGE="$($PY - "$MANIFEST" <<'EOF'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if d]
cj = [d for d in docs if d["kind"] == "CronJob"][0]
print(cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"])
EOF
)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
$PY - "$MANIFEST" "$WORK/repair.sh" <<'EOF'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1], encoding="utf-8")) if d]
cm = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "warm-spread-repair"][0]
open(sys.argv[2], "w", newline="\n").write(cm["data"]["repair.sh"])
EOF

mkdir -p "$WORK/stub" "$WORK/sa"
printf 'fake-token\n' > "$WORK/sa/token"; printf 'fake-ca\n' > "$WORK/sa/ca.crt"
# kubectl stub: answers from /state — pool.replicas, nodes.txt, pods.txt (tab-delimited, as jsonpath
# emits them), sandbox-<name>.json for `get sandbox`; FAIL_PODS fails the pod listing.
cat > "$WORK/stub/kubectl" <<'STUB'
#!/bin/sh
S=/state
case "$*" in
  *"get sandboxwarmpool"*) cat "$S/pool.replicas" ;;
  *"get nodes"*) cat "$S/nodes.txt" ;;
  *"get pods"*) [ -f "$S/FAIL_PODS" ] && { echo "error: the server is currently unable to handle the request" >&2; exit 1; }; cat "$S/pods.txt" ;;
  *"get sandbox "*) n=$(printf '%s' "$*" | sed -n 's/.*get sandbox \([^ ]*\).*/\1/p'); [ -f "$S/sandbox-$n.json" ] || { echo "Error from server (NotFound): sandboxes.agents.x-k8s.io \"$n\" not found" >&2; exit 1; }; cat "$S/sandbox-$n.json" ;;
  *) echo "stub: unhandled: kubectl $*" >&2; exit 3 ;;
esac
STUB
# curl stub: the raw DELETE with the precondition body — records URL + body, answers the status in
# /state/delete.code (default 200) the way `-w %{http_code}` would.
cat > "$WORK/stub/curl" <<'STUB'
#!/bin/sh
S=/state
url=""; body=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) body="$2"; shift ;;
    -o|-w|--cacert|-H|-X) shift ;;
    http*|https*) url="$1" ;;
  esac
  shift
done
echo "DELETE $url $body" >> "$S/deletes.log"
printf '%s' "$(cat "$S/delete.code" 2>/dev/null || echo 200)"
STUB
chmod +x "$WORK/stub/kubectl" "$WORK/stub/curl"

cat > "$WORK/inner.sh" <<'EOF'
#!/bin/sh
# Runs INSIDE the CronJob image with the stubs first on PATH.
set -u
export PATH=/t/stub:$PATH
export SA_DIR=/t/sa KUBE_API=https://api.test
S=/state
T="$(printf '\t')"
fail() { echo "FAIL: $*" >&2; exit 1; }
reset() { rm -rf "$S"/* "$S"/.[!.]* 2>/dev/null; echo 2 > "$S/pool.replicas"; : > "$S/deletes.log"; }
nodes_ok() { printf 'node-a%s%sTrue\nnode-b%s%sTrue\n' "$T" "$T" "$T" "$T" > "$S/nodes.txt"; }   # unschedulable ABSENT, as the real API prints it
pods_crowded() { printf 'm-old%snode-b%s2026-09-21T12:36:00Z\nm-new%snode-b%s2026-09-23T10:00:00Z\n' "$T" "$T" "$T" "$T" > "$S/pods.txt"; }
sandbox() { # name owner-kind rv [label]
  printf '{"apiVersion":"agents.x-k8s.io/v1beta1","kind":"Sandbox","metadata":{"name":"%s","resourceVersion":"%s","labels":{%s},"ownerReferences":[{"kind":"%s","controller":true,"name":"x"}]}}\n' \
    "$1" "$3" "${4:-\"agents.x-k8s.io/warm-pool-sandbox\":\"595505bd\"}" "$2" > "$S/sandbox-$1.json"
}
run() { sh /t/repair.sh 2>&1; }
deletes() { grep -c '^DELETE' "$S/deletes.log"; }

echo "[A] balanced"; reset; nodes_ok
printf 'm-old%snode-a%s2026-09-21T12:36:00Z\nm-new%snode-b%s2026-09-23T10:00:00Z\n' "$T" "$T" "$T" "$T" > "$S/pods.txt"
out=$(run) || fail "A exit $?: $out"; echo "$out" | grep -q "spread ok: node-a=1 node-b=1" || fail "A: $out"
[ "$(deletes)" = 0 ] || fail "A deleted something"

echo "[B] crowded b, empty a -> youngest on b goes, pinned to its resourceVersion"; reset; nodes_ok; pods_crowded
sandbox m-new SandboxWarmPool 4242; sandbox m-old SandboxWarmPool 1
out=$(run) || fail "B exit $?: $out"
echo "$out" | grep -q "repair: deleted sandbox m-new on node-b at resourceVersion 4242 (node-a=0 node-b=2 ; node-a had none)" || fail "B: $out"
[ "$(deletes)" = 1 ] || fail "B deletes != 1"
grep -q 'DELETE https://api.test/apis/agents.x-k8s.io/v1beta1/namespaces/testpool/sandboxes/m-new {"preconditions":{"resourceVersion":"4242"}}' "$S/deletes.log" || fail "B wrong request: $(cat "$S/deletes.log")"
grep -q "m-old" "$S/deletes.log" && fail "B deleted the OLDEST member"

echo "[C] node-a NotReady, then cordoned -> not eligible"; reset; pods_crowded; sandbox m-new SandboxWarmPool 7
printf 'node-a%s%sFalse\nnode-b%s%sTrue\n' "$T" "$T" "$T" "$T" > "$S/nodes.txt"
out=$(run) || fail "C exit $?: $out"; echo "$out" | grep -q "nothing to spread: replicas=2 eligible-nodes=1" || fail "C: $out"
[ "$(deletes)" = 0 ] || fail "C deleted"
printf 'node-a%strue%sTrue\nnode-b%s%sTrue\n' "$T" "$T" "$T" "$T" > "$S/nodes.txt"
out=$(run) || fail "C2 exit $?: $out"; echo "$out" | grep -q "eligible-nodes=1" || fail "C2: $out"; [ "$(deletes)" = 0 ] || fail "C2 deleted"

echo "[D] replicas 1"; reset; nodes_ok; pods_crowded; echo 1 > "$S/pool.replicas"
out=$(run) || fail "D exit $?: $out"; echo "$out" | grep -q "nothing to spread: replicas=1" || fail "D: $out"; [ "$(deletes)" = 0 ] || fail "D deleted"

echo "[E] failed pod listing -> non-zero, nothing deleted"; reset; nodes_ok; pods_crowded; touch "$S/FAIL_PODS"
out=$(run) && fail "E must fail: $out"; echo "$out" | grep -q "error: cannot list warm members" || fail "E: $out"; [ "$(deletes)" = 0 ] || fail "E deleted"

echo "[F] dry run"; reset; nodes_ok; pods_crowded; sandbox m-new SandboxWarmPool 9
out=$(DRY_RUN=1 run) || fail "F exit $?: $out"; echo "$out" | grep -q "would delete sandbox m-new on node-b at resourceVersion 9" || fail "F: $out"; [ "$(deletes)" = 0 ] || fail "F deleted"

echo "[G] a Pending member counts nowhere"; reset; nodes_ok; sandbox m-new SandboxWarmPool 11
printf 'm-old%snode-b%s2026-09-21T12:36:00Z\nm-new%snode-b%s2026-09-23T10:00:00Z\nm-pending%s%s2026-09-23T10:05:00Z\n' "$T" "$T" "$T" "$T" "$T" "$T" > "$S/pods.txt"
out=$(run) || fail "G exit $?: $out"; echo "$out" | grep -q "repair: deleted sandbox m-new on node-b" || fail "G: $out"; [ "$(deletes)" = 1 ] || fail "G deletes != 1"

echo "[H] the victim was adopted already (stale pod label) -> skipped"; reset; nodes_ok; pods_crowded
sandbox m-new SandboxClaim 13 '"agents.x-k8s.io/launch-type":"warm"'
out=$(run) || fail "H exit $?: $out"; echo "$out" | grep -q "skip: sandbox m-new is no longer the pool's (controller=SandboxClaim, warm-label=absent)" || fail "H: $out"
[ "$(deletes)" = 0 ] || fail "H deleted a LEASED environment"

echo "[I] adopted between the check and the delete -> 409, skipped, exit 0"; reset; nodes_ok; pods_crowded; sandbox m-new SandboxWarmPool 15; echo 409 > "$S/delete.code"
out=$(run) || fail "I exit $?: $out"; echo "$out" | grep -q "skip: sandbox m-new changed between the check and the delete (resourceVersion 15 is stale)" || fail "I: $out"
[ "$(deletes)" = 1 ] || fail "I: the preconditioned delete must have been attempted exactly once"

echo "[J] an unexpected delete status fails the run"; reset; nodes_ok; pods_crowded; sandbox m-new SandboxWarmPool 17; echo 500 > "$S/delete.code"
out=$(run) && fail "J must fail: $out"; echo "$out" | grep -q "error: delete sandbox testpool/m-new answered HTTP 500" || fail "J: $out"
echo "ALL WARM-SPREAD-REPAIR TESTS PASSED"
EOF

# Docker Desktop from Git Bash needs a Windows-style host path and no MSYS path mangling.
HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
mkdir -p "$WORK/state"; chmod -R 755 "$WORK"; chmod 777 "$WORK/state"
docker run --rm -v "$HOSTWORK:/t:ro" -v "$HOSTWORK/state:/state" --user 65534 "$IMAGE" sh /t/inner.sh
