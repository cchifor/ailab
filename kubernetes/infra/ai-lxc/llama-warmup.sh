#!/usr/bin/env bash
# Warm up llama-server after it reports healthy, so the first real request isn't cold.
# Usage: llama-warmup.sh <port> <model-alias>
set -u
PORT="${1:-8080}"
ALIAS="${2:-default}"

# Wait up to ~3 min for the model to finish loading (/health returns 200 when ready).
for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# Fire one tiny completion to prime the GPU pipeline + KV cache. Best-effort.
curl -sf -X POST "http://127.0.0.1:${PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  --data "{\"model\":\"${ALIAS}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}" \
  >/dev/null 2>&1 || true
exit 0
