#!/usr/bin/env bash
# test-warm-spread-repair.sh — the warm-spread-repair script (kubernetes/apps/infrastructure/testpool/
# warm-spread-repair.yaml, ConfigMap key repair.sh) under the CronJob's OWN image (busybox ash in
# docker.io/alpine/k8s, digest-pinned in the manifest) with `kubectl` shadowed by a stub that answers
# canned listings and records deletes:
#   A. one warm member per node                      -> "spread ok", no delete
#   B. two on node-b, none on node-a                 -> the YOUNGEST member on node-b is deleted, once
#   C. node-a NotReady (or cordoned)                 -> not eligible, nothing deleted
#   D. the pool asks for one member                  -> nothing to spread
#   E. a failed pod listing                          -> non-zero exit, nothing deleted (fail closed)
#   F. DRY_RUN=1                                     -> "would delete", nothing deleted
#   G. a Pending member (no node) beside two on b    -> ignored for the count; b's youngest goes
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

mkdir -p "$WORK/stub"
# The stub reads its answers from /state: pool.replicas, nodes.txt, pods.txt; FAIL_PODS makes the pod
# listing fail; every delete is appended to deletes.log.
cat > "$WORK/stub/kubectl" <<'STUB'
#!/bin/sh
S=/state
case "$*" in
  *"get sandboxwarmpool"*) cat "$S/pool.replicas" ;;
  *"get nodes"*) cat "$S/nodes.txt" ;;
  *"get pods"*) [ -f "$S/FAIL_PODS" ] && { echo "error: the server is currently unable to handle the request" >&2; exit 1; }; cat "$S/pods.txt" ;;
  *"delete sandbox"*) echo "$*" >> "$S/deletes.log"; exit 0 ;;
  *) echo "stub: unhandled: kubectl $*" >&2; exit 3 ;;
esac
STUB
chmod +x "$WORK/stub/kubectl"

cat > "$WORK/inner.sh" <<'EOF'
#!/bin/sh
# Runs INSIDE the CronJob image with the stub kubectl first on PATH.
set -u
export PATH=/t/stub:$PATH
S=/state
fail() { echo "FAIL: $*" >&2; exit 1; }
reset() { rm -rf "$S"/* "$S"/.[!.]* 2>/dev/null; echo 2 > "$S/pool.replicas"; : > "$S/deletes.log"; }
nodes_ok() { printf 'node-a false True\nnode-b false True\n' > "$S/nodes.txt"; }
run() { sh /t/repair.sh 2>&1; }
deletes() { grep -c . "$S/deletes.log"; }

echo "[A] balanced"; reset; nodes_ok
printf 'm-old node-a 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\n' > "$S/pods.txt"
out=$(run) || fail "A exit $?: $out"; echo "$out" | grep -q "spread ok: node-a=1 node-b=1" || fail "A: $out"
[ "$(deletes)" = 0 ] || fail "A deleted something"

echo "[B] crowded b, empty a -> youngest on b goes"; reset; nodes_ok
printf 'm-old node-b 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\n' > "$S/pods.txt"
out=$(run) || fail "B exit $?: $out"
echo "$out" | grep -q "repair: deleted sandbox m-new on node-b (node-a=0 node-b=2 ; node-a had none)" || fail "B: $out"
[ "$(deletes)" = 1 ] || fail "B deletes != 1"; grep -q "delete sandbox m-new" "$S/deletes.log" || fail "B wrong victim"
grep -q "m-old" "$S/deletes.log" && fail "B deleted the OLDEST member"

echo "[C] node-a NotReady / cordoned -> not eligible"; reset
printf 'node-a false False\nnode-b false True\n' > "$S/nodes.txt"
printf 'm-old node-b 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\n' > "$S/pods.txt"
out=$(run) || fail "C exit $?: $out"; echo "$out" | grep -q "nothing to spread: replicas=2 eligible-nodes=1" || fail "C: $out"
[ "$(deletes)" = 0 ] || fail "C deleted"
printf 'node-a true True\nnode-b false True\n' > "$S/nodes.txt"
out=$(run) || fail "C2 exit $?: $out"; echo "$out" | grep -q "eligible-nodes=1" || fail "C2: $out"; [ "$(deletes)" = 0 ] || fail "C2 deleted"

echo "[D] replicas 1"; reset; nodes_ok; echo 1 > "$S/pool.replicas"
printf 'm-old node-b 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\n' > "$S/pods.txt"
out=$(run) || fail "D exit $?: $out"; echo "$out" | grep -q "nothing to spread: replicas=1" || fail "D: $out"; [ "$(deletes)" = 0 ] || fail "D deleted"

echo "[E] failed pod listing -> non-zero, nothing deleted"; reset; nodes_ok; touch "$S/FAIL_PODS"
printf 'm-old node-b 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\n' > "$S/pods.txt"
out=$(run) && fail "E must fail: $out"; echo "$out" | grep -q "error: cannot list warm members" || fail "E: $out"; [ "$(deletes)" = 0 ] || fail "E deleted"

echo "[F] dry run"; reset; nodes_ok
printf 'm-old node-b 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\n' > "$S/pods.txt"
out=$(DRY_RUN=1 run) || fail "F exit $?: $out"; echo "$out" | grep -q "would delete sandbox m-new on node-b" || fail "F: $out"; [ "$(deletes)" = 0 ] || fail "F deleted"

echo "[G] a Pending member counts nowhere"; reset; nodes_ok
printf 'm-old node-b 2026-09-21T12:36:00Z\nm-new node-b 2026-09-23T10:00:00Z\nm-pending  2026-09-23T10:05:00Z\n' > "$S/pods.txt"
out=$(run) || fail "G exit $?: $out"; echo "$out" | grep -q "repair: deleted sandbox m-new on node-b" || fail "G: $out"
[ "$(deletes)" = 1 ] || fail "G deletes != 1"
echo "ALL WARM-SPREAD-REPAIR TESTS PASSED"
EOF

# Docker Desktop from Git Bash needs a Windows-style host path and no MSYS path mangling.
HOSTWORK="$WORK"
if command -v cygpath >/dev/null 2>&1; then HOSTWORK="$(cygpath -m "$WORK")"; export MSYS_NO_PATHCONV=1; fi
mkdir -p "$WORK/state"; chmod -R 755 "$WORK"; chmod 777 "$WORK/state"
docker run --rm -v "$HOSTWORK:/t:ro" -v "$HOSTWORK/state:/state" --user 65534 "$IMAGE" sh /t/inner.sh
