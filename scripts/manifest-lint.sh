#!/usr/bin/env bash
# manifest-lint.sh — the FIRST manifest-validation gate in ailab: fail-closed `kustomize build` +
# `kubeconform` over every Flux Kustomization path this checkout can actually build.
#
# FAIL CLOSED, following broker-inventory.yaml / tenant-guard-cel.yaml: `set -euo pipefail`, no
# `|| true`, no soft skips. A missing interpreter, a missing docker, a `kustomize build` that
# fails, or a kubeconform violation is a non-zero exit from THIS script, which fails
# .gitea/workflows/manifests.yaml's one job directly — there is no result aggregation for a
# silent-pass bug (join(needs.*.result) == "" on this forge's act_runner) to hide inside.
#
# PATH DISCOVERY is delegated to scripts/manifest-paths.py, which RESOLVES every Kustomization's
# sourceRef against the GitRepository objects the tree declares and excludes only the two whose
# resolved url is a different repo AND that are on its reviewed allowlist (agentforge-tenants,
# platform) — every other non-local shape is a non-zero exit there; see that script's docstring.
# Every path it prints is proven (verify_buildable) to contain a kustomization.yaml before this
# script ever shells out to docker. It prints which parser it used (PyYAML, or the strict stdlib
# fallback that is what the CI runner has) to stderr, so the log shows it.
#
# IMAGES ARE DIGEST-PINNED (kustomize v5.4.3, kubeconform v0.6.7) — a floating tag is a
# supply-chain surface this gate would otherwise reintroduce on every run. Re-pin by re-running:
#   docker pull registry.k8s.io/kustomize/kustomize:vX.Y.Z && docker inspect ... RepoDigests
#   docker pull ghcr.io/yannh/kubeconform:vX.Y.Z && docker inspect ... RepoDigests
#
# kubeconform runs -strict -ignore-missing-schemas: strict catches unknown-field typos in
# manifests kustomize build itself does not validate structurally; -ignore-missing-schemas is
# additive rather than a second hard gate — this estate vendors several CRDs (Flux, cert-manager,
# KEDA, kro, ...) not all of which resolve against the datreeio CRDs-catalog schema-location, and
# a CRD kubeconform cannot find a schema for must not become a false-negative failure on this
# script's first day. `kustomize build` succeeding is the hard gate; kubeconform is additive on
# top of it. See "SKIP LIST" below for the one adjustment the first real run required.
#
# ACCEPTED LIMITATION of -ignore-missing-schemas: kubeconform decides "missing schema" from the
# document's own apiVersion/kind, so a MISSPELLED built-in kind (`kind: Deploymnet`) or apiVersion
# is indistinguishable from an unknown CRD — it is counted as Skipped, not failed, and -strict
# never sees it (-strict only protects a resource whose schema resolves). Such a typo still fails
# at `kubectl apply`/Flux time, and `kustomize build` still has to render it, but THIS gate will
# not catch it. Dropping the flag would turn every un-catalogued CRD in this estate into a false
# failure instead; the trade is recorded here rather than hidden.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "no python3/python on the runner — failing closed rather than skipping the gate" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "no docker on the runner — failing closed rather than skipping the gate" >&2
  exit 1
fi

KUSTOMIZE_IMAGE="registry.k8s.io/kustomize/kustomize:v5.4.3@sha256:6dd0a67e2a8634a5d1aabd9c5e888ff220663e979b55bc17fe4b3a845718bb10"
KUBECONFORM_IMAGE="ghcr.io/yannh/kubeconform:v0.6.7@sha256:0925177fb05b44ce18574076141b5c3d83235e1904d3f952182ac99ddc45762c"

# SKIP LIST — kinds this first real run proved kubeconform cannot validate meaningfully, kept to
# the minimum kubeconform's kind-only `-skip` supports (no per-resource filter exists: `-skip`
# and `-reject` both take "comma-separated list of kinds or GVKs" only, confirmed via `-h`).
#
#   Secret — this repo commits credentials SOPS-encrypted-AT-REST (age; `.sops.yaml` /
#   docs: Secrets = SOPS + age). `kustomize build` renders those Secret documents exactly as
#   Flux's own kustomize-controller does BEFORE its sops decryption post-processing step: an
#   opaque `stringData`/`data` blob PLUS the top-level `sops:` metadata block the SOPS
#   KustomizeGenerator needs to decrypt it. `-strict` correctly flags that `sops` key as an
#   unknown additionalProperty against the core v1 Secret schema — it is not a real manifest
#   defect, it is the shape at-rest encryption produces, and decrypting to validate around it is
#   explicitly out of scope (this gate must NEVER decrypt anything; scripts/check-inline-hashes.py
#   and af-verify-brokers cover derived-value drift on the encrypted document without opening it).
#   Cost: this also stops validating the handful of Secrets this repo does NOT encrypt (six opaque
#   `tep-dw*-token` Secrets in kubernetes/apps/infrastructure/testpool, first run: 2026-09-02) —
#   accepted, since kubeconform has no way to skip only the encrypted ones and those six are
#   already the simplest possible Secret shape (a bare opaque token) with the least for `-strict`
#   to usefully catch. Revisit if kubeconform ever grows a per-document skip.
SKIP_ARGS=(-skip Secret)

"$PY" --version

OUT_DIR="$REPO_ROOT/out"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# Written to a real file, NOT read via `mapfile < <(...)`: a process-substitution's exit status is
# invisible to `set -e` (and to `pipefail`, which only covers an actual `|` pipeline), so a failing
# manifest-paths.py would otherwise be silently treated as "printed nothing" instead of failing
# this script outright. A plain redirected command IS covered by `set -e`.
PATHS_FILE="$(mktemp)"
trap 'rm -f "$PATHS_FILE"' EXIT
"$PY" scripts/manifest-paths.py > "$PATHS_FILE"
mapfile -t PATHS < "$PATHS_FILE"
if [ "${#PATHS[@]}" -eq 0 ]; then
  echo "manifest-paths.py discovered zero Kustomization paths — failing closed (expected > 0)" >&2
  exit 1
fi
echo "discovered ${#PATHS[@]} locally-buildable Flux Kustomization paths"

# Rendered filename = <2-digit index>-<slug>.yaml. The index is what makes the name INJECTIVE:
# the slug alone (leading "./" stripped, "/" flattened to "__") is only for readability and is
# NOT collision-safe — ./a/b and ./a__b flatten to the same slug, and the second build would
# silently overwrite the first, so kubeconform would validate one rendered manifest for two built
# paths. The index makes every path's render its own file whatever its slug; the count check
# after the loop is the belt to that brace.
i=0
for path in "${PATHS[@]}"; do
  i=$((i + 1))
  slug="${path#./}"
  slug="${slug//\//__}"
  echo "== kustomize build $path =="
  docker run --rm -v "$REPO_ROOT:/work:ro" -w /work "$KUSTOMIZE_IMAGE" \
    build "$path" > "$OUT_DIR/$(printf '%02d' "$i")-$slug.yaml"
done

# kubeconform's image has no shell (distroless), so `/out/*.yaml` cannot glob INSIDE the
# container — expand it out here, on the host, where $OUT_DIR actually exists, and pass each
# rendered file as an explicit /out/<name>.yaml argument instead.
CONTAINER_FILES=()
for f in "$OUT_DIR"/*.yaml; do
  CONTAINER_FILES+=("/out/$(basename "$f")")
done
if [ "${#CONTAINER_FILES[@]}" -ne "${#PATHS[@]}" ]; then
  echo "built ${#PATHS[@]} paths but found ${#CONTAINER_FILES[@]} rendered files in $OUT_DIR — refusing to validate a partial set" >&2
  exit 1
fi

echo "== kubeconform -strict over ${#CONTAINER_FILES[@]} rendered manifests =="
docker run --rm -v "$OUT_DIR:/out:ro" "$KUBECONFORM_IMAGE" \
  -strict -summary -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  "${SKIP_ARGS[@]}" \
  "${CONTAINER_FILES[@]}"

echo "manifest-lint: OK (${#PATHS[@]} paths built and validated)"
