#!/usr/bin/env bash
# test-check-litellm-mirrored-routes.sh — prove scripts/check-litellm-mirrored-routes.py bites.
#
# Four cases against COPIES of the real manifests (the checker takes the two paths as arguments):
#   1. the repo as committed passes;
#   2. a sampling drift on the main gateway's route (temperature) fails, naming the field;
#   3. a litellm-local copy without the synthetic cost fields fails (its spend would never meter);
#   4. a litellm-local without the route at all fails.
# A checker that passes on 2-4 would let the mirror rot silently, which is the whole bug class.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECK="$REPO/scripts/check-litellm-mirrored-routes.py"
MAIN="$REPO/kubernetes/apps/apps/ai/litellm.yaml"
LOCAL="$REPO/kubernetes/apps/apps/ai/litellm-local.yaml"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. committed state passes
"$PY" "$CHECK" "$MAIN" "$LOCAL" >/dev/null || fail "the committed manifests must pass"

# 2. main-side drift is caught and named. Mutate ONLY the vllm-cloud route's temperature on a copy.
"$PY" - "$MAIN" "$T/main-drift.yaml" <<'PY'
import sys, re
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
# first vllm-cloud entry: the temperature line that follows it
i = text.index("model_name: qwen3.8-27b-vllm-cloud")
j = text.index("temperature: 1.0", i)
text = text[:j] + "temperature: 0.7" + text[j + len("temperature: 1.0"):]
open(dst, "w", encoding="utf-8").write(text)
PY
if "$PY" "$CHECK" "$T/main-drift.yaml" "$LOCAL" 2>"$T/err2" >/dev/null; then fail "a temperature drift on the main route must fail"; fi
grep -q 'litellm_params.temperature: main=0.7 local=1.0' "$T/err2" || fail "the drift must be named (got: $(cat "$T/err2"))"

# 3. a local copy without the cost fields fails
"$PY" - "$LOCAL" "$T/local-nocost.yaml" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
i = text.index("model_name: qwen3.8-27b-vllm-cloud")
head, tail = text[:i], text[i:]
tail = tail.replace("          input_cost_per_token: 0.000001\n", "", 1).replace("          output_cost_per_token: 0.000001\n", "", 1)
open(dst, "w", encoding="utf-8").write(head + tail)
PY
if "$PY" "$CHECK" "$MAIN" "$T/local-nocost.yaml" 2>"$T/err3" >/dev/null; then fail "a local route without cost fields must fail"; fi
grep -q 'lacks litellm_params.input_cost_per_token' "$T/err3" || fail "the missing cost field must be named (got: $(cat "$T/err3"))"

# 4. the route missing from litellm-local fails
"$PY" - "$LOCAL" "$T/local-noroute.yaml" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
open(dst, "w", encoding="utf-8").write(open(src, encoding="utf-8").read().replace("model_name: qwen3.8-27b-vllm-cloud", "model_name: qwen3.8-27b-vllm-cloud-renamed"))
PY
if "$PY" "$CHECK" "$MAIN" "$T/local-noroute.yaml" 2>"$T/err4" >/dev/null; then fail "a litellm-local without the route must fail"; fi
grep -q 'absent from local-noroute.yaml' "$T/err4" || fail "the absence must be named (got: $(cat "$T/err4"))"

echo "test-check-litellm-mirrored-routes: OK (passes as committed; drift, missing cost, missing route all fail and are named)"
