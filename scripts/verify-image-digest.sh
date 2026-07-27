#!/usr/bin/env bash
# Scripted step-0 of docs/runbooks/agentforge-platform-activation.md (replaces the eyeball curl):
# re-verify a pinned image tag still resolves to the approved digest, fail closed. Registry is
# anonymous-pull (registry.chifor.me), no auth needed.
#
# Usage:
#   scripts/verify-image-digest.sh <image> <tag> [expected-digest]
#   scripts/verify-image-digest.sh agentforge-platform 276ccad
#   scripts/verify-image-digest.sh agentforge-platform 276ccad sha256:<64-hex>
#
# expected-digest is OPTIONAL when image=agentforge-platform: it self-defaults to whatever digest is
# CURRENTLY pinned in kubernetes/apps/apps/agentforge/deployment.yaml, so the runbook/justfile example
# can never go stale again (previously both hardcoded a tag/digest pair that had already drifted from
# what's deployed). Any other <image> still requires an explicit <expected-digest> — there's no
# manifest to self-default it from.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

REGISTRY="https://registry.chifor.me"
REPO_PREFIX="agentforge"
DEPLOY_FILE="kubernetes/apps/apps/agentforge/deployment.yaml"
# Send both OCI + docker manifest media types so a multi-arch index resolves the same as it would for
# containerd/kubelet (avoids a false PASS/FAIL from a registry defaulting to a different manifest kind).
ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
DIGEST_RE='^sha256:[0-9a-f]{64}$'

usage() {
  cat <<EOF
Usage: $(basename "$0") <image> <tag> [expected-digest]

  HEAD ${REGISTRY}/v2/${REPO_PREFIX}/<image>/manifests/<tag> and compare the response's
  Docker-Content-Digest header to <expected-digest> (sha256:<64 hex>). Exit 0 on match, non-zero on
  mismatch or an unreachable/malformed response.

  expected-digest is optional for image=agentforge-platform: self-defaults to the digest currently
  pinned (anchored 'image:' line) in $DEPLOY_FILE. Any other image requires it explicitly.

Example (self-defaulting, always current):
  $(basename "$0") agentforge-platform 276ccad

Example (explicit override):
  $(basename "$0") agentforge-platform 276ccad sha256:<64-hex>
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi
if [ $# -lt 2 ]; then
  usage >&2
  exit 2
fi

IMAGE="$1"
TAG="$2"
EXPECTED="${3:-}"

if [ -z "$EXPECTED" ]; then
  if [ "$IMAGE" != "agentforge-platform" ]; then
    echo "error: expected-digest is required for image '$IMAGE' (self-default only covers agentforge-platform, from $DEPLOY_FILE)" >&2
    usage >&2
    exit 2
  fi
  # Anchored to the actual YAML key (not just anywhere the string "image:" appears, e.g. in a
  # comment), so a stray doc line can never be mistaken for the live pin.
  default_line="$(grep -m1 -E '^[[:space:]]*image:[[:space:]]*registry\.chifor\.me/agentforge/agentforge-platform@sha256:[0-9a-f]{64}' "$DEPLOY_FILE" || true)"
  EXPECTED="$(printf '%s' "$default_line" | grep -oE 'sha256:[0-9a-f]{64}' || true)"
  [ -n "$EXPECTED" ] || { echo "error: could not self-default expected-digest from $DEPLOY_FILE (no anchored 'image:' line found for agentforge-platform)" >&2; exit 2; }
  echo "(self-defaulted expected-digest from $DEPLOY_FILE: $EXPECTED)"
fi

if ! [[ "$EXPECTED" =~ $DIGEST_RE ]]; then
  echo "error: bad expected-digest '$EXPECTED' (want sha256:<64 lowercase hex>)" >&2
  exit 2
fi

URL="${REGISTRY}/v2/${REPO_PREFIX}/${IMAGE}/manifests/${TAG}"
echo "== pin-verify: HEAD $URL =="

if ! headers="$(curl -sS --fail --head --max-time 15 -H "Accept: ${ACCEPT}" "$URL")"; then
  echo "FAIL: HEAD request to $URL failed (network/registry error, or tag not found)" >&2
  exit 1
fi

actual="$(printf '%s' "$headers" | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2; exit}')"

if [ -z "$actual" ]; then
  echo "FAIL: no Docker-Content-Digest header in the response" >&2
  printf '%s\n' "$headers" | sed 's/^/  /' >&2
  exit 1
fi

echo "expected: $EXPECTED"
echo "actual:   $actual"

if [ "$actual" = "$EXPECTED" ]; then
  echo "PASS: ${IMAGE}:${TAG} resolves to the approved digest"
  exit 0
fi

echo "FAIL: ${IMAGE}:${TAG} resolves to $actual, expected $EXPECTED — the tag moved; re-verify provenance before re-pinning" >&2
exit 1
