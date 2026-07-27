#!/usr/bin/env bash
# Scripted step-0 of docs/runbooks/agentforge-platform-activation.md (replaces the eyeball curl):
# re-verify a pinned image tag still resolves to the approved digest, fail closed. Registry is
# anonymous-pull (registry.chifor.me), no auth needed.
#
# Usage:
#   scripts/verify-image-digest.sh <image> <tag> <expected-digest>
#   scripts/verify-image-digest.sh agentforge-platform 2776074 \
#     sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e
set -euo pipefail

REGISTRY="https://registry.chifor.me"
REPO_PREFIX="agentforge"
# Send both OCI + docker manifest media types so a multi-arch index resolves the same as it would for
# containerd/kubelet (avoids a false PASS/FAIL from a registry defaulting to a different manifest kind).
ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
DIGEST_RE='^sha256:[0-9a-f]{64}$'

usage() {
  cat <<EOF
Usage: $(basename "$0") <image> <tag> <expected-digest>

  HEAD ${REGISTRY}/v2/${REPO_PREFIX}/<image>/manifests/<tag> and compare the response's
  Docker-Content-Digest header to <expected-digest> (sha256:<64 hex>). Exit 0 on match, non-zero on
  mismatch or an unreachable/malformed response.

Example:
  $(basename "$0") agentforge-platform 2776074 sha256:85a4a3c7a3599b20834688c8f2ea060341435d7cba07239d94bf5b00afac374e
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi
if [ $# -lt 3 ]; then
  usage >&2
  exit 2
fi

IMAGE="$1"
TAG="$2"
EXPECTED="$3"

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
