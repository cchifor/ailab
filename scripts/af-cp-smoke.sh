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
#   AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context; the estate
#                     convention is admin@ai — the sibling scripts/verify-sandbox-boundary.sh defaults
#                     its own KUBECTL_CONTEXT to admin@ai internally, but af-db.sh/af-cp-smoke.sh keep
#                     an empty=current-context contract instead and let callers (the justfile recipes)
#                     pin the default at the call site — see `just af-cp-smoke`)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

AF_NS="agentforge"
DEPLOY="deploy/agentforge-platform"
DEPLOY_FILE="kubernetes/apps/apps/agentforge/deployment.yaml"
# db-migrate.yaml's Job pods carry the SAME app.kubernetes.io/name=agentforge-platform label (and sort
# first / live up to ttlSecondsAfterFinished=3600 after a migrate run), so name alone can select a
# migrate-Job pod instead of the CP Deployment pod. The Job template additionally carries
# app.kubernetes.io/component=db-migrate (verified in kubernetes/apps/apps/agentforge/db-migrate.yaml);
# the Deployment template carries NO component label at all, so `component!=db-migrate` correctly
# matches it (Kubernetes' != selector matches resources missing the label too) while excluding any
# migrate-Job pod. --field-selector=status.phase=Running is a second, independent belt-and-braces
# filter (a migrate Job pod could theoretically be re-labeled to slip the component selector, but it
# can never be Running for the ~1h a completed Job pod lingers).
LABEL_SELECTOR="app.kubernetes.io/name=agentforge-platform,app.kubernetes.io/component!=db-migrate"
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
  AF_KUBE_CONTEXT   kubectl --context override (default: empty = current context; see the Env
                    comment at the top of this file for the admin@ai convention)
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
# Anchored to the actual YAML key (not just anywhere the string "image:" appears, e.g. in a comment)
# so a stray doc/comment line mentioning the same image@digest can never be mistaken for the pin.
expected_line="$(grep -m1 -E '^[[:space:]]*image:[[:space:]]*registry\.chifor\.me/agentforge/agentforge-platform@sha256:[0-9a-f]{64}' "$DEPLOY_FILE" || true)"
expected_digest="$(printf '%s' "$expected_line" | grep -oE 'sha256:[0-9a-f]{64}' || true)"
if [ -z "$expected_digest" ]; then
  bad "could not find the pinned agentforge-platform image digest in $DEPLOY_FILE"
else
  echo "  pinned:  $expected_digest"
  # -l excludes db-migrate Job pods (see LABEL_SELECTOR comment above); --field-selector is a second,
  # independent filter so only a currently-Running pod can ever be picked (.items[0] on a selector that
  # somehow still matched zero or several pods would otherwise silently grab an unrelated one).
  running_image_id="$("${K[@]}" -n "$AF_NS" get pod -l "$LABEL_SELECTOR" --field-selector=status.phase=Running \
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
# /readyz's response body shape is undocumented (only /healthz is documented as unconditional
# {"status":"ok"} — see deployment.yaml); the acceptance criterion here is HTTP 200, which wget's exit
# status already encodes (wget treats any non-2xx response as an error and exits non-zero). So PASS/FAIL
# on the exit status and print the body purely informationally, without assuming any particular shape.
readyz_out=""
if readyz_out="$("${K[@]}" -n "$AF_NS" exec "$DEPLOY" -- wget -qO- http://127.0.0.1:8080/readyz 2>&1)"; then
  echo "  readyz (informational): $readyz_out"
  ok "/readyz returned HTTP 200 (DB SELECT 1 succeeded)"
else
  echo "  readyz (informational): $readyz_out"
  bad "/readyz did not return HTTP 200"
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
