#!/bin/sh
# tep-mini — spike-4 prototype of the tep CLI. POSIX sh + kubectl only.
# Usage:
#   tep-mini.sh lease <name> [ttl-minutes]   -> prints POD=<pod> when Ready
#   tep-mini.sh sync <name> <local-dir>      -> tar-over-exec into /work/src (rsync = prod item)
#   tep-mini.sh run <name> <cmd...>          -> runs in control container from /work/src
#   tep-mini.sh release <name>
set -eu
KCFG="${TEP_KUBECONFIG:-$HOME/.tep/kubeconfig}"
NS=testpool-spike
K="kubectl --kubeconfig=$KCFG -n $NS"

pod_of() {
  $K get sandboxclaim "$1" -o jsonpath='{.status.sandbox.name}'
}

case "$1" in
  lease)
    NAME=$2; MIN=${3:-120}
    EXP=$(date -u -d "+${MIN} minutes" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+${MIN}M +%Y-%m-%dT%H:%M:%SZ)
    T0=$(date +%s.%N)
    printf '{"apiVersion":"extensions.agents.x-k8s.io/v1beta1","kind":"SandboxClaim","metadata":{"name":"%s"},"spec":{"warmPoolRef":{"name":"env-std-pool"},"lifecycle":{"shutdownTime":"%s","shutdownPolicy":"Delete"}}}' "$NAME" "$EXP" | $K apply -f - >/dev/null
    i=0
    until [ "$($K get sandboxclaim "$NAME" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = "True" ]; do
      i=$((i+1)); [ $i -gt 1800 ] && echo "LEASE TIMEOUT (pool drained? cold-create pending)" && exit 1
      sleep 0.2
    done
    T1=$(date +%s.%N)
    echo "LEASE READY in $(awk "BEGIN{printf \"%.2f\", $T1-$T0}")s POD=$(pod_of "$NAME") EXPIRES=$EXP"
    ;;
  sync)
    NAME=$2; SRC=$3
    POD=$(pod_of "$NAME")
    T0=$(date +%s.%N)
    tar czf - -C "$SRC" . | $K exec -i "$POD" -c control -- sh -c 'mkdir -p /work/src && tar xzf - -C /work/src'
    T1=$(date +%s.%N)
    echo "SYNCED $(du -sk "$SRC" | cut -f1)KB in $(awk "BEGIN{printf \"%.2f\", $T1-$T0}")s"
    ;;
  run)
    NAME=$2; shift 2
    POD=$(pod_of "$NAME")
    $K exec "$POD" -c control -- sh -c "cd /work/src 2>/dev/null || cd /work; $*"
    ;;
  release)
    $K delete sandboxclaim "$2" --wait=false
    echo RELEASED
    ;;
  *) echo "unknown: $1" && exit 2 ;;
esac
