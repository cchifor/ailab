#!/usr/bin/env bash
###############################################################################
# Fetch GGUF models to the shared QNAP NFS model store (run ON a Proxmox host,
# which has /mnt/pve/qnap-nfs mounted + internet). Resumable (curl -C -),
# idempotent, no huggingface-cli dependency. All URLs/sizes verified 2026-06-14.
#
#   scripts/fetch-models.sh [daily|coder|gpt-oss|qwen3.5|vision|qwen3.6|gemma4|all]   (default: daily)
#
# Sizes: daily 18.6G · coder 18.6G · gpt-oss-120b 63.4G (3 shards) ·
#        qwen3.5-122b 76.5G (3 shards) · vision 6.4G · qwen3.6 ~22G · gemma4 ~15.6G.
#        NFS store has ~5 TB free.
###############################################################################
set -euo pipefail

MODELS="${MODELS:-/mnt/pve/qnap-nfs/models}"
SEL="${1:-daily}"
HF="https://huggingface.co"

dl() { # <url> <dest-relative-path>
  local url="$1" dest="$MODELS/$2"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ] && [ ! -f "$dest.part" ]; then
    echo "  [skip] $2 (present)"; return 0
  fi
  echo "  [get ] $2"
  curl -fL --retry 8 --retry-delay 5 -C - -o "$dest" "$url"
}

fetch_daily() {
  echo "== Qwen3-30B-A3B (daily driver, Q4_K_M ~18.6G) =="
  dl "$HF/unsloth/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf" \
     "qwen3-30b-a3b/Qwen3-30B-A3B-Q4_K_M.gguf"
}

fetch_coder() {
  echo "== Qwen3-Coder-30B-A3B-Instruct (Q4_K_M ~18.6G) =="
  dl "$HF/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf" \
     "qwen3-coder-30b-a3b/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
}

fetch_gpt_oss() {
  echo "== gpt-oss-120b (native MXFP4, 3 shards ~63.4G) =="
  for n in 00001 00002 00003; do
    dl "$HF/ggml-org/gpt-oss-120b-GGUF/resolve/main/gpt-oss-120b-mxfp4-${n}-of-00003.gguf" \
       "gpt-oss-120b/gpt-oss-120b-mxfp4-${n}-of-00003.gguf"
  done
}

fetch_qwen35() {
  echo "== Qwen3.5-122B-A10B (Q4_K_M, 3 shards ~76.5G) =="
  for n in 00001 00002 00003; do
    dl "$HF/unsloth/Qwen3.5-122B-A10B-GGUF/resolve/main/Q4_K_M/Qwen3.5-122B-A10B-Q4_K_M-${n}-of-00003.gguf" \
       "qwen3.5-122b-a10b/Qwen3.5-122B-A10B-Q4_K_M-${n}-of-00003.gguf"
  done
}

fetch_vision() {
  echo "== Qwen3-VL-8B-Instruct (vision/VL, Q4 ~5.2G + mmproj ~1.2G) =="
  dl "$HF/unsloth/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf" \
     "qwen3-vl-8b/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf"
  dl "$HF/unsloth/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-F16.gguf" \
     "qwen3-vl-8b/mmproj-F16.gguf"
}

fetch_qwen36() {
  # Qwen3.6-35B-A3B: hybrid (Gated-DeltaNet) MoE, ~35B/3B-active, coding+vision. Replaces the
  # qwen3-coder-30b slot (node1 :8081). Needs llama.cpp >= the qwen35moe arch build (b9672 pin).
  echo "== Qwen3.6-35B-A3B (coding+vision, UD-Q4_K_M ~21G + mmproj ~1.2G) =="
  dl "$HF/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf" \
     "qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
  dl "$HF/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/mmproj-F16.gguf" \
     "qwen3.6-35b-a3b/mmproj-F16.gguf"
}

fetch_gemma4() {
  # Gemma-4-26B-A4B (Google QAT q4_0): vision (image+video) MoE, ~25B/3.8B-active. Replaces the
  # qwen3-vl-8b vision slot (node1 :8082). NOT audio. Needs llama.cpp gemma4 arch (b9672 pin).
  echo "== Gemma-4-26B-A4B-it QAT q4_0 (vision, ~14.4G + mmproj ~1.2G) =="
  dl "$HF/google/gemma-4-26B-A4B-it-qat-q4_0-gguf/resolve/main/gemma-4-26B_q4_0-it.gguf" \
     "gemma-4-26b-a4b/gemma-4-26B_q4_0-it.gguf"
  dl "$HF/google/gemma-4-26B-A4B-it-qat-q4_0-gguf/resolve/main/gemma-4-26B-it-mmproj.gguf" \
     "gemma-4-26b-a4b/gemma-4-26B-it-mmproj.gguf"
}

mkdir -p "$MODELS"
case "$SEL" in
  daily)    fetch_daily ;;
  coder)    fetch_coder ;;
  gpt-oss)  fetch_gpt_oss ;;
  qwen3.5)  fetch_qwen35 ;;
  vision)   fetch_vision ;;
  qwen3.6)  fetch_qwen36 ;;
  gemma4)   fetch_gemma4 ;;
  all)      fetch_daily; fetch_coder; fetch_gpt_oss; fetch_qwen35; fetch_vision; fetch_qwen36; fetch_gemma4 ;;
  *) echo "usage: fetch-models.sh [daily|coder|gpt-oss|qwen3.5|vision|qwen3.6|gemma4|all]" >&2; exit 2 ;;
esac
echo "Done. Store: $MODELS"
ls -la "$MODELS"/*/ 2>/dev/null || true
