#!/usr/bin/env bash
# netpol-check.sh - prove the edge proxy's tunnel listener (:8080) admits ONLY the cloudflared pods
# (ADR 0040; apps/edge/proxy/networkpolicy.yaml). The runtime fixture cannot test this: docker
# networks do not apply Kubernetes NetworkPolicy. Run after a change to that policy, from a host
# with `kubectl --context admin@ai`:
#
#   bash scripts/edge/netpol-check.sh
#
# Starts short-lived curl pods (deleted afterwards) and expects:
#   edge ns, label app.kubernetes.io/name=cloudflared         -> :8080 answers  (the admitted peer)
#   edge ns, label app.kubernetes.io/name=cloudflared-strive  -> :8080 refused  (the other connector)
#   edge ns, no such label                                    -> :8080 refused
#   another ns, label app.kubernetes.io/name=cloudflared      -> :8080 refused  (namespace AND label)
#   any of them                                               -> :8443 answers  (the direct listener is open)
# each against both the ClusterIP Service and a pod IP. Exits non-zero on any surprise.
set -euo pipefail
CTX="${KUBE_CONTEXT:-admin@ai}"
K=(kubectl --context "$CTX")
IMG="curlimages/curl:8.11.1"
RUN="netpol-$(date +%s)-$RANDOM"
OTHER_NS="${OTHER_NS:-default}"
fail=0

pod_ip=$("${K[@]}" -n edge get pods -l app.kubernetes.io/name=edge-proxy -o jsonpath='{.items[0].status.podIP}')
[ -n "$pod_ip" ] || { echo "no edge-proxy pod"; exit 2; }

probe() { # <ns> <label-value|-> <target> <port> <expect: open|refused> <what>
  local ns="$1" label="$2" target="$3" port="$4" want="$5" what="$6" name labels=() code got
  name="$RUN-$(printf '%s' "$ns$label$target$port" | md5sum | cut -c1-6)"
  [ "$label" = "-" ] || labels=(--labels "app.kubernetes.io/name=$label")
  code=$("${K[@]}" -n "$ns" run "$name" "${labels[@]}" --image="$IMG" --restart=Never --rm -i --quiet \
    --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":65532,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"c","image":"'"$IMG"'","args":["-sk","-o","/dev/null","-w","%{http_code}","--connect-timeout","4","-m","6","-H","Host: router.chifor.me","-H","CF-Connecting-IP: 203.0.113.9","-H","Cf-Visitor: {\"scheme\":\"https\"}","'"$target"'"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' \
    2>/dev/null || true)
  if [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]; then got=open; else got=refused; fi
  if [ "$got" = "$want" ]; then echo "ok   $what ($code)"; else echo "FAIL $what: wanted $want, got $got ($code)"; fail=1; fi
}

for target in "http://edge-proxy-tunnel.edge.svc.cluster.local:8080/" "http://$pod_ip:8080/"; do
  probe edge cloudflared "$target" 8080 open "edge/cloudflared -> $target"
  probe edge cloudflared-strive "$target" 8080 refused "edge/cloudflared-strive -> $target"
  probe edge - "$target" 8080 refused "edge/unlabelled -> $target"
  probe "$OTHER_NS" cloudflared "$target" 8080 refused "$OTHER_NS/cloudflared-label -> $target"
done
probe "$OTHER_NS" - "https://$pod_ip:8443/" 8443 open "$OTHER_NS/unlabelled -> direct listener"
exit "$fail"
