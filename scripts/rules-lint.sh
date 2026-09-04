#!/usr/bin/env bash
# rules-lint.sh — `promtool check rules` over every kubernetes/apps/infrastructure/monitoring/*-rules.yaml.
#
# WHY: kustomize build + kubeconform (scripts/manifest-lint.sh, C-P0-05) prove a PrometheusRule is a
# well-formed CRD object; neither parses a single `expr`. A rule whose PromQL does not parse is
# rejected by prometheus-operator at reconcile time and the WHOLE file's rules silently vanish from
# Prometheus — the same "coverage that watches nothing" defect agentforge-rules.yaml's header is
# about. promtool is the only tool that type-checks the expressions, so it runs here on every PR.
#
# HOW: promtool reads a plain `groups:` document, not the CRD wrapper, so scripts/promrule-spec.py
# (stdlib only — the CI runner has no PyYAML) first cuts each manifest's `spec:` into a scratch
# directory, failing closed on anything it cannot extract exactly; then ONE docker run checks them
# all. The prometheus image is distroless (no shell): the file list is expanded HERE and passed as
# explicit /rules/<name> arguments, and `--entrypoint promtool` replaces the server binary.
#
# FAIL CLOSED, in the idiom of manifest-lint.sh / broker-inventory.yaml: `set -euo pipefail`, no
# `|| true`, no soft skips. No python, no docker, zero rules files, an extraction that fails, or a
# promtool error is a non-zero exit from THIS script. And the two tools must AGREE on how many
# rules they saw: promrule-spec.py reports the `- alert:`/`- record:` count it extracted, promtool
# prints "N rules found" per file, and a mismatch between the two totals is a failure — that is
# the guard against the one way this gate could pass while checking less than the whole file
# (a truncated extraction, which the extractor's own count check also refuses).
#
# IMAGE IS DIGEST-PINNED (Prometheus v3.5.0 LTS; the cluster runs kube-prometheus-stack 86.x, a v3
# Prometheus, so its promtool — stricter than v2 — is the one whose verdict matters). quay.io, not
# Docker Hub: anonymous Docker Hub pulls 429 on this estate (kube-prometheus-stack.yaml). Re-pin:
#   docker pull quay.io/prometheus/prometheus:vX.Y.Z && docker inspect --format '{{index .RepoDigests}}' ...
#
# UNIT TESTS TOO, not just `check rules`: a rule whose PromQL parses can still be one that
# can never fire. Both obvious thresholds for reviewbot-rules.yaml were wrong that way when
# first written (`oldest_job_age > 3600` could not fire during a 50-minute incident; an ungated
# 6h no-success rule fired for 369 minutes across a healthy 48h window). So any
# monitoring/*-rules.test.yaml is run through `promtool test rules`, which evaluates the real
# expressions against synthetic series and asserts which alerts fire. Optional by design — a
# rules file with no fixture is still checked, just not exercised.
#
# CI: .gitea/workflows/rules-lint.yaml runs this on every push and PR (one job, fail closed) until
# manifest-lint.sh lands on main (C-P0-05, ailab#464); the intended hook there is one line,
# `bash scripts/rules-lint.sh`, after its kubeconform step, at which point that workflow folds
# into manifests.yaml.
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

PROMETHEUS_IMAGE="quay.io/prometheus/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996"
RULES_DIR="kubernetes/apps/infrastructure/monitoring"

RULE_FILES=("$RULES_DIR"/*-rules.yaml)
if [ "${#RULE_FILES[@]}" -eq 0 ] || [ ! -f "${RULE_FILES[0]}" ]; then
  echo "no $RULES_DIR/*-rules.yaml found — failing closed (expected > 0)" >&2
  exit 1
fi
echo "found ${#RULE_FILES[@]} PrometheusRule manifests under $RULES_DIR"

# A scratch dir (not a repo path) so nothing has to be gitignored; removed on every exit path.
# mktemp -d is 0700 and the prometheus image runs promtool as `nobody`, which then cannot even
# stat the bind mount ("permission denied") — so the dir and the specs are made world-readable
# (they are copies of committed, non-secret rule files; nothing else is ever written there).
OUT_DIR="$(mktemp -d)"
trap 'rm -rf "$OUT_DIR"' EXIT
chmod 755 "$OUT_DIR"

"$PY" --version
# The extractor's stdout is kept (not just streamed) for the rule-count reconciliation below; the
# scratch files live OUTSIDE $OUT_DIR so nothing but the specs is ever bind-mounted into promtool.
LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$OUT_DIR" "$LOG_DIR"' EXIT
"$PY" scripts/promrule-spec.py --out "$OUT_DIR" "${RULE_FILES[@]}" | tee "$LOG_DIR/extract.log"
chmod 644 "$OUT_DIR"/*.yaml
EXPECTED_RULES="$(sed -n 's/^promrule-spec: \([0-9][0-9]*\) rules across .*$/\1/p' "$LOG_DIR/extract.log")"
if ! [[ "$EXPECTED_RULES" =~ ^[0-9]+$ ]] || [ "$EXPECTED_RULES" -eq 0 ]; then
  echo "promrule-spec did not report a positive total rule count (got '${EXPECTED_RULES}') — failing closed" >&2
  exit 1
fi

CONTAINER_FILES=()
for f in "$OUT_DIR"/*.yaml; do
  CONTAINER_FILES+=("/rules/$(basename "$f")")
done
if [ "${#CONTAINER_FILES[@]}" -ne "${#RULE_FILES[@]}" ]; then
  echo "extracted ${#CONTAINER_FILES[@]} specs for ${#RULE_FILES[@]} manifests — refusing to check a partial set" >&2
  exit 1
fi

echo "== promtool check rules over ${#CONTAINER_FILES[@]} extracted specs =="
docker run --rm -v "$OUT_DIR:/rules:ro" --entrypoint promtool "$PROMETHEUS_IMAGE" \
  check rules "${CONTAINER_FILES[@]}" 2>&1 | tee "$LOG_DIR/promtool.log"

# Reconcile: the sum of promtool's per-file "N rules found" must equal what was extracted.
FOUND_RULES="$(sed -n 's/.*SUCCESS: \([0-9][0-9]*\) rules found.*$/\1/p' "$LOG_DIR/promtool.log" | awk '{ s += $1 } END { print s + 0 }')"
if [ "$FOUND_RULES" -ne "$EXPECTED_RULES" ]; then
  echo "promtool found ${FOUND_RULES} rules but promrule-spec extracted ${EXPECTED_RULES} — the gate did not check the whole set" >&2
  exit 1
fi

# `promtool test rules` resolves each fixture's `rule_files:` RELATIVE TO THE FIXTURE, so the
# tests are copied in beside the extracted specs they name.
TEST_FILES=("$RULES_DIR"/*-rules.test.yaml)
if [ -f "${TEST_FILES[0]}" ]; then
  CONTAINER_TESTS=()
  for f in "${TEST_FILES[@]}"; do
    cp "$f" "$OUT_DIR/$(basename "$f")"
    chmod 644 "$OUT_DIR/$(basename "$f")"
    CONTAINER_TESTS+=("/rules/$(basename "$f")")
  done
  # FAIL CLOSED on a fixture that loads nothing. promtool only WARNS when a `rule_files:`
  # entry matches no file, so a fixture naming a rules file that was never extracted (a
  # typo, a renamed manifest) would evaluate zero rules and pass - a gate checking nothing,
  # which is the exact defect this repo has already shipped once. Every referenced basename
  # must exist among the extracted specs.
  for t in "${TEST_FILES[@]}"; do
    # scripts/promtest-refs.py FAILS CLOSED when it cannot parse a nonempty rule_files
    # list, so `rule_files:` at EOF, written inline, or absent is an error rather than an
    # empty happy path that checks zero references. Captured (not piped into `while`) so
    # the extractor's exit status is not swallowed by the loop. NO PIPE: piping through `tr`
    # would put that exit status behind `set -o pipefail` (which this script does set, but a
    # gate whose fail-closed behaviour depends on a setting 90 lines away is one edit from
    # silently passing). promtest-refs.py writes LF on every platform instead.
    if ! refs="$("$PY" scripts/promtest-refs.py "$t")"; then
      echo "$(basename "$t"): unusable rule_files list - refusing a fixture that may load no rules" >&2
      exit 1
    fi
    while read -r ref; do
      [ -n "$ref" ] || continue
      if [ ! -f "$OUT_DIR/$ref" ]; then
        echo "$(basename "$t") references rule file '$ref', which is not among the extracted specs" >&2
        exit 1
      fi
    done <<< "$refs"
  done

  echo "== promtool test rules over ${#CONTAINER_TESTS[@]} fixture(s) =="
  docker run --rm -v "$OUT_DIR:/rules:ro" --entrypoint promtool "$PROMETHEUS_IMAGE" \
    test rules "${CONTAINER_TESTS[@]}"
else
  echo "no $RULES_DIR/*-rules.test.yaml fixtures — skipping promtool test rules"
fi

echo "rules-lint: OK (${#RULE_FILES[@]} PrometheusRule specs, ${FOUND_RULES} rules checked by promtool)"
