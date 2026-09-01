#!/bin/sh
# golden-refresh.sh — build and publish the next golden snapshot (golden-vN+1).
#
# The publication protocol (test-env-pool plan): golden snapshots are IMMUTABLE, versioned by
# name; the SandboxTemplate's dataSource NAME is the pointer; bumping it is a git change. This
# script does the cluster half; the git bump is deliberately manual (a PR), so a bad golden can
# never roll out silently. Run from an operator workstation with the admin kubeconfig.
#
# Steps: populate pod (env shape, blank or previous-golden volume) -> pull hack/golden-images.txt
# through the mirror -> clean dockerd stop + sync -> VolumeSnapshot golden-vN+1 -> wait ReadyToUse
# -> VERIFY (restore a scratch clone, assert overlay2 + image list) -> print the template bump
# instructions. Old goldens are pruned only when no PVC references them (kubectl below refuses
# otherwise).
set -eu
NS=testpool
KC="kubectl --context admin@ai -n $NS"
HERE=$(dirname "$0")
NEXT=${1:?usage: golden-refresh.sh golden-vN+1 (e.g. golden-v2)}

echo "== populate pod"
$KC apply -f "$HERE/golden-populate-pod.yaml"
i=0
until $KC logs golden-populate -c dind 2>/dev/null | grep -q ENV-DIND-OK; do
  i=$((i+1)); [ $i -gt 200 ] && echo "populate pod never ready" && exit 1
  sleep 3
done

echo "== pulling golden set"
grep -v '^#' "$HERE/golden-images.txt" | while read -r img; do
  [ -n "$img" ] || continue
  echo "  pull $img"
  $KC exec golden-populate -c control -- docker pull -q "$img"
done
$KC exec golden-populate -c control -- sh -c 'docker system df; docker builder prune -af >/dev/null 2>&1 || true; sync'

echo "== quiesce + snapshot $NEXT"
# The populate pod's dind stops cleanly on pod deletion (SIGTERM -> dockerd graceful); we snapshot
# BEFORE deletion with containers stopped + a global sync — crash-consistent-plus-synced, and the
# verify step below is the gate that matters.
$KC exec golden-populate -c control -- sh -c 'test -z "$(docker ps -q)" || docker stop $(docker ps -q); sync'
# Generic-ephemeral PVC name is <podName>-<volumeName> (KEP-1698) — NOT the sandbox VCT order.
PVC=golden-populate-dockerlib
cat <<EOF | $KC apply -f -
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: $NEXT
  namespace: $NS
spec:
  volumeSnapshotClassName: qnap-iscsi
  source:
    persistentVolumeClaimName: $PVC
EOF
until [ "$($KC get volumesnapshot "$NEXT" -o jsonpath='{.status.readyToUse}' 2>/dev/null)" = "true" ]; do sleep 2; done
echo "$NEXT readyToUse"

echo "== verify (scratch restore)"
sed "s/GOLDEN_NAME/$NEXT/" "$HERE/golden-verify-pod.yaml" | $KC apply -f -
i=0
until $KC logs golden-verify -c dind 2>/dev/null | grep -qE 'ENV-DIND-OK|FATAL'; do
  i=$((i+1)); [ $i -gt 200 ] && echo "verify pod never ready" && exit 1
  sleep 3
done
$KC logs golden-verify -c dind | grep -q ENV-DIND-OK || { echo "VERIFY FAILED (not overlay2?)"; exit 2; }
$KC exec golden-verify -c control -- docker images --format '{{.Repository}}:{{.Tag}}' | sort
echo "verify OK — images above must cover golden-images.txt"

echo "== cleanup populate/verify pods (volumes auto-GC on the Delete class)"
$KC delete pod golden-populate golden-verify --wait=true

cat <<EOF

DONE. To ROLL OUT $NEXT:
  1. Edit sandboxtemplate-std.yaml: dataSource.name -> $NEXT   (git commit + PR + merge; Flux applies)
  2. Bounce the warm pool so members refill from $NEXT:
       kubectl --context admin@ai -n $NS delete sandbox -l agents.x-k8s.io/warm-pool-sandbox
  3. Keep the previous golden until no PVC references it, then:
       kubectl --context admin@ai -n $NS delete volumesnapshot <old>
EOF
