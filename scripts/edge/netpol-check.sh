#!/usr/bin/env bash
# netpol-check.sh - prove the edge proxy's tunnel listener (:8080) admits ONLY the cloudflared pods
# (ADR 0040; apps/edge/proxy/networkpolicy.yaml). The runtime fixture cannot test this: docker
# networks do not apply Kubernetes NetworkPolicy. Run after a change to that policy, from a host
# with `kubectl --context admin@ai`:
#
#   bash scripts/edge/netpol-check.sh
#
# Starts short-lived curl pods (deleted afterwards, also on Ctrl-C) and expects:
#   edge ns, label app.kubernetes.io/name=cloudflared         -> :8080 open     (the admitted peer)
#   edge ns, label app.kubernetes.io/name=cloudflared-strive  -> :8080 blocked  (the other connector)
#   edge ns, no such label                                    -> :8080 blocked
#   another ns, label app.kubernetes.io/name=cloudflared      -> :8080 blocked  (namespace AND label)
# each against both the ClusterIP Service and a pod IP.
#
# A "blocked" verdict needs evidence, not just a missing answer: curl must exit 28 (the dropped
# SYN timed out, Cilium's default) or 7 (refused), AND the same pod must reach the direct listener
# :8443 (open to every pod) as a control. Anything else (the pod never ran, an image pull, DNS, a
# TLS error, a control that failed) is an infrastructure error, never a pass.
# Exit: 0 = every expectation held; 1 = a policy expectation failed; 2 = could not tell.
set -euo pipefail
CTX="${KUBE_CONTEXT:-admin@ai}"
K=(kubectl --context "$CTX")
IMG="curlimages/curl:8.11.1@sha256:c1fe1679c34d9784c1b0d1e5f62ac0a79fca01fb6377cdd33e90473c6f9f9a69"
RUN="netpol-$(date +%s)-$RANDOM"
OTHER_NS="${OTHER_NS:-default}"
fail=0
infra=0
n=0

cleanup() {
  for ns in edge "$OTHER_NS"; do
    "${K[@]}" -n "$ns" delete pods -l "edge-netpol-check=$RUN" --wait=false --ignore-not-found >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
trap 'exit 130' INT TERM

pod_ip=$("${K[@]}" -n edge get pods -l app.kubernetes.io/name=edge-proxy \
  -o jsonpath='{range .items[?(@.status.podIP)]}{.status.podIP}{"\n"}{end}' | head -n1)
[ -n "$pod_ip" ] || { echo "no edge-proxy pod with an IP"; exit 2; }

# curl's outcome as "<http_code> rc=<exit>"; one line per request, read back from the pod log.
verdict() { # <line> -> open | blocked | error
  local re='^([0-9]{3}) rc=([0-9]+)$'
  if [[ "$1" =~ $re ]]; then
    if [ "${BASH_REMATCH[2]}" = 0 ] && [[ "${BASH_REMATCH[1]}" =~ ^[1-5] ]]; then echo open; return; fi
    case "${BASH_REMATCH[2]}" in 7|28) echo blocked; return ;; esac
  fi
  echo error
}

probe() { # <ns> <label-value|-> <target> <expect: open|blocked> <what>
  local ns="$1" label="$2" target="$3" want="$4" what="$5" name labels out line ctl got
  n=$((n + 1))
  name="$RUN-$n"
  labels="edge-netpol-check: \"$RUN\""
  [ "$label" = "-" ] || labels="$labels, app.kubernetes.io/name: $label"
  if ! "${K[@]}" create -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $name
  namespace: $ns
  labels: { $labels }
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: c
      image: $IMG
      securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
      command: [/bin/sh, -c]
      args:
        - |
          r() { curl -sk -o /dev/null -w '%{http_code}' --connect-timeout 4 -m 6 "\$@"; echo " rc=\$?"; }
          echo "probe \$(r -H 'Host: router.chifor.me' -H 'CF-Connecting-IP: 203.0.113.9' -H 'Cf-Visitor: {"scheme":"https"}' '$target')"
          echo "control \$(r --resolve 'router.chifor.me:8443:$pod_ip' https://router.chifor.me:8443/)"
EOF
  then
    echo "ERR  $what: could not create the probe pod"; infra=1; return
  fi
  if ! "${K[@]}" -n "$ns" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=120s >/dev/null 2>&1; then
    echo "ERR  $what: the probe pod did not complete ($("${K[@]}" -n "$ns" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null))"
    infra=1; return
  fi
  out=$("${K[@]}" -n "$ns" logs "$name" 2>/dev/null || true)
  "${K[@]}" -n "$ns" delete pod "$name" --wait=false >/dev/null 2>&1 || true
  line=$(printf '%s\n' "$out" | sed -n 's/^probe //p')
  ctl=$(printf '%s\n' "$out" | sed -n 's/^control //p')
  if [ "$(verdict "$ctl")" != open ]; then
    echo "ERR  $what: control (direct listener :8443) failed: '$ctl'; a missing :8080 answer proves nothing"
    infra=1; return
  fi
  got=$(verdict "$line")
  if [ "$got" = error ]; then
    echo "ERR  $what: inconclusive curl outcome '$line'"; infra=1
  elif [ "$got" = "$want" ]; then
    echo "ok   $what: $got ($line; control $ctl)"
  else
    echo "FAIL $what: wanted $want, got $got ($line)"; fail=1
  fi
}

for target in "http://edge-proxy-tunnel.edge.svc.cluster.local:8080/" "http://$pod_ip:8080/"; do
  probe edge cloudflared "$target" open "edge/cloudflared -> $target"
  probe edge cloudflared-strive "$target" blocked "edge/cloudflared-strive -> $target"
  probe edge - "$target" blocked "edge/unlabelled -> $target"
  probe "$OTHER_NS" cloudflared "$target" blocked "$OTHER_NS/cloudflared-label -> $target"
done
[ "$fail" = 0 ] || exit 1
[ "$infra" = 0 ] || exit 2
echo "all expectations held"
