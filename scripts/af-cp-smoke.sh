#!/usr/bin/env bash
# Post-deploy smoke test for deploy/agentforge-platform — the PR-B go-live verification walk in
# docs/runbooks/agentforge-platform-activation.md, step 5. One command, run AFTER PR-B has merged and
# Flux has reconciled.
#
# Usage:
#   scripts/af-cp-smoke.sh          # run the smoke test (rollout -> digest -> readyz -> healthz)
#   scripts/af-cp-smoke.sh --help   # show this usage (no kubectl/curl calls)
#
# Env:
#   AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

AF_NS="agentforge"
DEPLOY="deploy/agentforge-platform"
DEPLOY_FILE="kubernetes/apps/apps/agentforge/deployment.yaml"
LABEL_SELECTOR="app.kubernetes.io/name=agentforge-platform"
EXTERNAL_URL="https://agentforge.chifor.me/healthz"

K=(kubectl)
if [ -n "${AF_KUBE_CONTEXT:-}" ]; then
  K=(kubectl --context "$AF_KUBE_CONTEXT")
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") [--help]

Post-deploy smoke test for $DEPLOY:
  1. wait for the rollout to finish (--timeout=180s)
  2. assert the running pod's imageID digest == the digest pinned in $DEPLOY_FILE
  3. in-pod readiness probe: kubectl exec ... wget -qO- http://127.0.0.1:8080/readyz
  4. external liveness probe over the cloudflared tunnel: curl -fsS $EXTERNAL_URL
Prints the deployed image digest + a PASS/FAIL summary; exits non-zero on any failure.

Env:
  AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context)
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi
if [ $# -ne 0 ]; then
  echo "error: unexpected argument '$1'" >&2
  usage >&2
  exit 2
fi

pass=0
fail=0
ok()  { echo "  PASS  $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL  $*"; fail=$((fail + 1)); }

echo "== af-cp-smoke: $DEPLOY =="

echo "[1] rollout status"
if "${K[@]}" -n "$AF_NS" rollout status "$DEPLOY" --timeout=180s; then
  ok "rollout complete"
else
  bad "rollout did not complete within 180s"
fi

echo "[2] image digest pin"
expected_line="$(grep -m1 -E 'image: registry\.chifor\.me/agentforge/agentforge-platform@sha256:[0-9a-f]{64}' "$DEPLOY_FILE" || true)"
expected_digest="$(printf '%s' "$expected_line" | grep -oE 'sha256:[0-9a-f]{64}' || true)"
if [ -z "$expected_digest" ]; then
  bad "could not find the pinned agentforge-platform image digest in $DEPLOY_FILE"
else
  echo "  pinned:  $expected_digest"
  running_image_id="$("${K[@]}" -n "$AF_NS" get pod -l "$LABEL_SELECTOR" \
    -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || true)"
  running_digest="$(printf '%s' "$running_image_id" | grep -oE 'sha256:[0-9a-f]{64}' || true)"
  echo "  running: ${running_digest:-<none>}"
  if [ -n "$running_digest" ] && [ "$running_digest" = "$expected_digest" ]; then
    ok "running pod imageID matches the pinned digest"
  else
    bad "running pod imageID ($running_digest) != pinned digest ($expected_digest)"
  fi
fi

echo "[3] in-pod readiness (/readyz)"
readyz_out="$("${K[@]}" -n "$AF_NS" exec "$DEPLOY" -- wget -qO- http://127.0.0.1:8080/readyz 2>&1 || true)"
echo "  readyz: $readyz_out"
if printf '%s' "$readyz_out" | grep -qiE '"status"\s*:\s*"ok"|^ok$'; then
  ok "/readyz reports ok (DB SELECT 1 succeeded)"
else
  bad "/readyz did not report ok"
fi

echo "[4] external liveness (/healthz, cloudflared tunnel path)"
healthz_out="$(curl -fsS --max-time 10 "$EXTERNAL_URL" 2>&1 || true)"
echo "  healthz: $healthz_out"
if printf '%s' "$healthz_out" | grep -qiE '"status"\s*:\s*"ok"|^ok$'; then
  ok "$EXTERNAL_URL reports ok"
else
  bad "$EXTERNAL_URL did not report ok (or was unreachable)"
fi

echo
echo "== SUMMARY =="
echo "image digest: ${running_digest:-${expected_digest:-unknown}}"
echo "$pass passed, $fail failed"
if [ "$fail" -ne 0 ]; then
  echo "af-cp-smoke: FAIL" >&2
  exit 1
fi
echo "af-cp-smoke: PASS"
