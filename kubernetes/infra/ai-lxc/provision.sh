#!/usr/bin/env bash
###############################################################################
# Provision an ai-llm LXC: llama.cpp (Vulkan/RADV, gfx1151) as non-root systemd
# service(s) + node_exporter + amdgpu sysfs metrics. Idempotent.
# Run INSIDE the container (see scripts/lxc-exec.py). Targets Debian 13.
#
# Companion files expected next to this one (pushed together): llama-warmup.sh,
# amdgpu-textfile.sh.
#
# Env overrides (defaults = the daily driver on the current 64 GB VRAM carve):
#   LLAMA_BUILD  pinned llama.cpp Vulkan release tag       (default b9672)
#                (b9672 adds the qwen35moe + gemma4 arches needed by Qwen3.6 / Gemma-4;
#                 b9631 predated gemma4 vision. Re-provisioning a node's DEFAULT instance
#                 re-downloads this build and restarts every instance on that node.)
#   MODEL        GGUF path inside the CT                   (default daily driver)
#   MODEL_ALIAS  name reported by /v1/models               (default qwen3.6-35b-a3b)
#   CTX          total KV context (shared across slots)    (default 32768)
#   PARALLEL     concurrent server slots                   (default 4)
#   EXTRA_ARGS   extra llama-server flags (e.g. --no-mmap) (default empty)
#   CACHE_TYPE_K KV-cache K quant, e.g. q8_0                (default empty => f16)
#   CACHE_TYPE_V KV-cache V quant, e.g. q8_0 (needs FA)     (default empty => f16)
#   INSTANCE     instance name; "default" => llama-server.service on 8080.
#                Any other name => a SECOND unit llama-server-<INSTANCE>.service
#                (base setup is skipped — run the default instance first).
#   PORT         llama-server listen port                  (default 8080)
#   RENDER_GID / VIDEO_GID  host group gids for the devices (default 993 / 44)
###############################################################################
set -euo pipefail

LLAMA_BUILD="${LLAMA_BUILD:-b9672}"
# Daily driver = Qwen3.6-35B-A3B (replaced qwen3-30b-a3b, retired 2026-07-01). node1's full
# steady-state launch (CTX=262144 PARALLEL=1 CACHE_TYPE_K/V=q8_0 + MMPROJ) is passed explicitly —
# see docs/runbooks/ai-host-setup.md. CTX/PARALLEL/MMPROJ/CACHE_TYPE defaults stay generic so they
# don't leak into node2/3 heavyweight re-provisions (which override MODEL/CTX/PARALLEL but not these).
MODEL="${MODEL:-/models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf}"
MODEL_ALIAS="${MODEL_ALIAS:-qwen3.6-35b-a3b}"
CTX="${CTX:-32768}"
PARALLEL="${PARALLEL:-4}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
CACHE_TYPE_K="${CACHE_TYPE_K:-}"   # KV-cache K quant (e.g. q8_0); empty => llama.cpp default (f16)
CACHE_TYPE_V="${CACHE_TYPE_V:-}"   # KV-cache V quant (e.g. q8_0); needs flash-attn (auto, enabled below)
MMPROJ="${MMPROJ:-}"   # optional vision projector GGUF path -> adds --mmproj (enables image input)
# Optional: stage the model onto local NVMe (/models-local) for fast cold loads. Set MODEL_STAGE_SRC to
# the NFS source DIR (e.g. /models/gpt-oss-120b) and point MODEL at the LOCAL copy (e.g.
# /models-local/gpt-oss-120b/...gguf). provision.sh rsyncs src -> dirname(MODEL) if not already staged.
MODEL_STAGE_SRC="${MODEL_STAGE_SRC:-}"
INSTANCE="${INSTANCE:-default}"
PORT="${PORT:-8080}"
RENDER_GID="${RENDER_GID:-993}"
VIDEO_GID="${VIDEO_GID:-44}"
# ---- On-demand model loading (llama-swap) ----
# SWAP=true => serve PORT via llama-swap instead of a fixed llama-server.service: the model loads on
# the first request and UNLOADS after TTL idle seconds, returning its GTT (~59/71 GiB for the
# heavyweights) to the host so ballooning + dev-workers can use it. This is how node2/node3's
# rarely-used gpt-oss/qwen3.5-122b stop being a permanent RAM tax. node1's daily driver stays pinned
# (leave SWAP unset). TTL=0 => model is loaded on first use but never idle-unloaded (a "pinned via
# llama-swap" model). MODEL/MODEL_ALIAS/CTX/PARALLEL/EXTRA_ARGS/KV/MMPROJ describe the one model this
# node serves — identical to the direct-mode args. See docs/runbooks/ai-model-swap.md + models.yaml.
SWAP="${SWAP:-}"
TTL="${TTL:-900}"                       # idle seconds before llama-swap unloads the model (0 = never)
LLAMA_SWAP_VERSION="${LLAMA_SWAP_VERSION:-v236}"   # github.com/mostlygeek/llama-swap release tag (pin; latest 2026-07-07)
SWAP_DIR=/opt/llama-swap
SWAP_BIN="${SWAP_DIR}/llama-swap"

INSTALL_DIR=/opt/llama.cpp
# The ggml-org release tarball extracts to a single dir named after the build,
# containing llama-server/llama-cli + the bundled .so files (flat).
BIN="${INSTALL_DIR}/llama-${LLAMA_BUILD}"
ASSET="llama-${LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz"
URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_BUILD}/${ASSET}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# RADV ICD filename varies by distro (radeon_icd.json vs radeon_icd.x86_64.json) — auto-detect.
# Forcing the RADV ICD excludes the llvmpipe software device (and any future amdvlk).
RADV_ICD="$(ls /usr/share/vulkan/icd.d/radeon_icd*.json 2>/dev/null | head -1 || true)"

UNIT="llama-server.service"
[ "$INSTANCE" != "default" ] && UNIT="llama-server-${INSTANCE}.service"
[ -n "$SWAP" ] && UNIT="llama-swap.service" # llama-swap owns PORT; the model is a child it spawns/kills

export DEBIAN_FRONTEND=noninteractive

if [ "$INSTANCE" = "default" ]; then
  echo "== [1/7] apt: RADV Vulkan userspace + tooling + node_exporter (NO amdvlk) =="
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    libvulkan1 mesa-vulkan-drivers vulkan-tools \
    libgomp1 libcurl4 ca-certificates curl tar jq rsync \
    prometheus-node-exporter

  echo "== [2/7] non-root service user 'llama' in render(${RENDER_GID})+video(${VIDEO_GID}) =="
  getent group "$RENDER_GID" >/dev/null || groupadd -g "$RENDER_GID" hostrender
  getent group "$VIDEO_GID"  >/dev/null || groupadd -g "$VIDEO_GID"  hostvideo
  RGRP="$(getent group "$RENDER_GID" | cut -d: -f1)"
  VGRP="$(getent group "$VIDEO_GID"  | cut -d: -f1)"
  id llama >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin llama
  usermod -aG "${RGRP},${VGRP}" llama

  echo "== [3/7] llama.cpp Vulkan prebuilt ${LLAMA_BUILD} =="
  mkdir -p "$INSTALL_DIR"
  if [ ! -x "$BIN/llama-server" ] || [ "$(cat "$INSTALL_DIR/.build" 2>/dev/null || true)" != "$LLAMA_BUILD" ]; then
    curl -fSL --retry 5 --retry-delay 5 "$URL" -o "/tmp/${ASSET}"
    rm -rf "$BIN"
    tar -xzf "/tmp/${ASSET}" -C "$INSTALL_DIR"
    echo "$LLAMA_BUILD" > "$INSTALL_DIR/.build"
    rm -f "/tmp/${ASSET}"
  fi
  test -x "$BIN/llama-server" || { echo "FATAL: llama-server not present after extract" >&2; exit 1; }

  echo "== [4/7] verify the Vulkan backend sees the iGPU (authoritative: llama.cpp's own list) =="
  if LD_LIBRARY_PATH="$BIN" "$BIN/llama-cli" --list-devices 2>/dev/null | grep -Eqi "RADV|Radeon|GFX1151"; then
    LD_LIBRARY_PATH="$BIN" "$BIN/llama-cli" --list-devices 2>/dev/null | grep -Ei "Vulkan|RADV|Radeon|GFX1151" | head
  else
    echo "WARNING: llama.cpp sees no Vulkan GPU — it will fall back to CPU. Check /dev/dri passthrough + Mesa + ICD." >&2
  fi

  echo "== [5/7] install helper scripts =="
  install -m 0755 "$HERE/llama-warmup.sh"   /usr/local/bin/llama-warmup.sh
  install -m 0755 "$HERE/amdgpu-textfile.sh" /usr/local/bin/amdgpu-textfile.sh

  if [ -n "$SWAP" ]; then
    echo "== [5b/7] llama-swap ${LLAMA_SWAP_VERSION} (on-demand model load + idle-unload) =="
    SWAP_VER_NUM="${LLAMA_SWAP_VERSION#v}" # release tag is vNNN; the asset filename drops the leading v
    SWAP_ASSET="llama-swap_${SWAP_VER_NUM}_linux_amd64.tar.gz"
    SWAP_URL="https://github.com/mostlygeek/llama-swap/releases/download/${LLAMA_SWAP_VERSION}/${SWAP_ASSET}"
    if [ ! -x "$SWAP_BIN" ] || [ "$(cat "$SWAP_DIR/.version" 2>/dev/null || true)" != "$LLAMA_SWAP_VERSION" ]; then
      curl -fSL --retry 5 --retry-delay 5 "$SWAP_URL" -o "/tmp/${SWAP_ASSET}"
      rm -rf "$SWAP_DIR"; mkdir -p "$SWAP_DIR"
      tar -xzf "/tmp/${SWAP_ASSET}" -C "$SWAP_DIR"
      # the binary may be at the archive root or one dir deep — normalize to $SWAP_BIN
      if [ ! -x "$SWAP_BIN" ]; then
        found="$(find "$SWAP_DIR" -maxdepth 2 -type f -name llama-swap | head -1)"
        [ -n "$found" ] && mv "$found" "$SWAP_BIN"
      fi
      chmod 0755 "$SWAP_BIN" 2>/dev/null || true
      echo "$LLAMA_SWAP_VERSION" > "$SWAP_DIR/.version"
      rm -f "/tmp/${SWAP_ASSET}"
    fi
    test -x "$SWAP_BIN" || { echo "FATAL: llama-swap missing after extract — check LLAMA_SWAP_VERSION / asset name" >&2; exit 1; }
  fi
else
  echo "== additional instance '${INSTANCE}' (port ${PORT}) — base setup skipped =="
  test -x "$BIN/llama-server" || { echo "FATAL: run the default instance first (binary missing)" >&2; exit 1; }
fi

# ---- Stage the model onto local NVMe (/models-local) for fast cold (re)loads ----
# Only when MODEL_STAGE_SRC is set AND MODEL points at the local copy. Idempotent (rsync skips
# already-staged shards). Requires the local-lvm mount from tofu (ai_llm_nodes[].model_cache_gb).
if [ -n "$MODEL_STAGE_SRC" ]; then
  DEST_DIR="$(dirname "$MODEL")"
  echo "== staging model: ${MODEL_STAGE_SRC}/ -> ${DEST_DIR}/ (local NVMe; ~7-15x faster cold loads) =="
  test -d "$MODEL_STAGE_SRC" || { echo "FATAL: MODEL_STAGE_SRC is not a directory: $MODEL_STAGE_SRC" >&2; exit 1; }
  mountpoint -q /models-local || echo "WARNING: /models-local is not a mount — staging onto the CT rootfs (add ai_llm_nodes[].model_cache_gb + tofu apply)." >&2
  mkdir -p "$DEST_DIR"
  rsync -a --partial "${MODEL_STAGE_SRC}/" "${DEST_DIR}/" # --partial resumes a killed copy; -a skips unchanged shards
  test -f "$MODEL" || { echo "FATAL: model file missing after staging: $MODEL (check MODEL vs MODEL_STAGE_SRC)" >&2; exit 1; }
  echo "   staged $(du -sh "$DEST_DIR" 2>/dev/null | cut -f1) into ${DEST_DIR}"
fi

# Shared llama-server flags (used by both the direct unit and, in SWAP mode, the llama-swap model cmd).
# Only pin the RADV ICD if we actually found it (an empty path would load NO driver -> CPU).
ICD_LINE=""
[ -n "$RADV_ICD" ] && ICD_LINE="Environment=VK_ICD_FILENAMES=${RADV_ICD}"
MMPROJ_FLAG=""
[ -n "$MMPROJ" ] && MMPROJ_FLAG="--mmproj ${MMPROJ}"
# Optional KV-cache quantization (halves KV memory at q8_0, near-lossless with flash-attn).
# Note: on hybrid/recurrent models (e.g. Qwen3.6) only the attention-KV honors this; the
# recurrent/conv state stays f32. Empty vars => llama.cpp default (f16), preserving prior behavior.
KV_FLAGS=""
[ -n "$CACHE_TYPE_K" ] && KV_FLAGS="--cache-type-k ${CACHE_TYPE_K}"
[ -n "$CACHE_TYPE_V" ] && KV_FLAGS="${KV_FLAGS} --cache-type-v ${CACHE_TYPE_V}"

if [ -n "$SWAP" ]; then
  echo "== systemd unit ${UNIT}: llama-swap on :${PORT} serving '${MODEL_ALIAS}' on-demand (ttl=${TTL}s) =="
  # llama-swap owns the listen port and spawns llama-server (on its \${PORT} macro) on first request,
  # then idle-unloads it after ttl seconds — returning the model's GTT to the host. The model cmd is the
  # SAME llama-server invocation as direct mode; no warm-up (on-demand loading is the whole point).
  install -d -o llama -g llama /etc/llama-swap
  # NOTE: \${PORT} below is a llama-swap MACRO (auto-assigned upstream port) — it must reach the config
  # literally, so it is backslash-escaped here to survive this (unquoted) heredoc. ttl=0 => never unload.
  cat >/etc/llama-swap/config.yaml <<EOF
# Rendered by provision.sh (SWAP=true). Intent/source-of-truth: kubernetes/infra/ai-lxc/models.yaml.
# One model per node (iGPU affinity). Tune idle-unload via TTL (0 = pin: load-once, never unload).
healthCheckTimeout: 900   # seconds to wait for a cold model to become healthy (a 122B load is minutes)
logLevel: info
models:
  "${MODEL_ALIAS}":
    cmd: >
      ${BIN}/llama-server --host 127.0.0.1 --port \${PORT}
      -m ${MODEL} -a ${MODEL_ALIAS}
      -ngl 99 -c ${CTX} --parallel ${PARALLEL}
      --flash-attn auto --jinja --metrics ${KV_FLAGS} ${MMPROJ_FLAG} ${EXTRA_ARGS}
    proxy: "http://127.0.0.1:\${PORT}"
    checkEndpoint: /health
    ttl: ${TTL}
EOF
  chown -R llama:llama /etc/llama-swap
  cat >"/etc/systemd/system/${UNIT}" <<EOF
[Unit]
Description=llama-swap (on-demand llama.cpp model load/idle-unload) — ai-llm [${MODEL_ALIAS}]
After=network-online.target
Wants=network-online.target

[Service]
User=llama
Environment=LD_LIBRARY_PATH=${BIN}
${ICD_LINE}
ExecStart=${SWAP_BIN} --listen 0.0.0.0:${PORT} --config /etc/llama-swap/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  # Free :${PORT}: if this node previously ran the fixed direct-mode unit, stop+disable it.
  systemctl disable --now llama-server.service 2>/dev/null || true
else
  echo "== systemd unit ${UNIT} (port ${PORT}, model ${MODEL_ALIAS}) with warm-up =="
  cat >"/etc/systemd/system/${UNIT}" <<EOF
[Unit]
Description=llama.cpp server (Vulkan/RADV gfx1151) — ai-llm [${INSTANCE}]
After=network-online.target
Wants=network-online.target

[Service]
User=llama
Environment=LD_LIBRARY_PATH=${BIN}
${ICD_LINE}
ExecStart=${BIN}/llama-server --host 0.0.0.0 --port ${PORT} \\
  -m ${MODEL} -a ${MODEL_ALIAS} \\
  -ngl 99 -c ${CTX} --parallel ${PARALLEL} \\
  --flash-attn auto --jinja --metrics ${KV_FLAGS} ${MMPROJ_FLAG} ${EXTRA_ARGS}
# Prime the model/KV after /health is up so the first user request isn't cold:
ExecStartPost=/usr/local/bin/llama-warmup.sh ${PORT} ${MODEL_ALIAS}
# Big models (120B/122B) take >90s to load+warm — raise the default start timeout so systemd
# doesn't kill the load as a 'timeout' and crashloop.
TimeoutStartSec=900
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

if [ "$INSTANCE" = "default" ]; then
  echo "== node_exporter textfile dir + amdgpu sysfs metrics timer =="
  mkdir -p /var/lib/prometheus/node-exporter
  # Point node_exporter at the textfile dir (idempotent: replace any existing ARGS= line).
  NE_DEFAULT=/etc/default/prometheus-node-exporter
  touch "$NE_DEFAULT"
  sed -i '/^ARGS=/d' "$NE_DEFAULT"
  echo 'ARGS="--collector.textfile.directory=/var/lib/prometheus/node-exporter"' >> "$NE_DEFAULT"
  cat >/etc/systemd/system/amdgpu-metrics.service <<'EOF'
[Unit]
Description=amdgpu sysfs -> Prometheus textfile (gfx1151)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/amdgpu-textfile.sh /var/lib/prometheus/node-exporter
EOF
  cat >/etc/systemd/system/amdgpu-metrics.timer <<'EOF'
[Unit]
Description=Run amdgpu-metrics every 10s
[Timer]
OnBootSec=15s
OnUnitActiveSec=10s
AccuracySec=1s
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable prometheus-node-exporter.service || true
  systemctl restart prometheus-node-exporter.service || true # restart so the textfile-dir ARGS take effect
  systemctl enable --now amdgpu-metrics.timer || true
  systemctl start amdgpu-metrics.service || true # generate the first .prom now (timer handles the rest)
fi

systemctl daemon-reload
systemctl enable "${UNIT}"
systemctl restart "${UNIT}" # restart (not just start) so a model/flags change takes effect

echo "OK: ai-llm provisioned [${INSTANCE}]. Health: curl http://$(hostname -I | awk '{print $1}'):${PORT}/health"
echo "    Model: ${MODEL} (alias ${MODEL_ALIAS}); port=${PORT} ctx=${CTX} parallel=${PARALLEL}"
[ -n "$SWAP" ] && echo "    Mode: llama-swap (on-demand, ttl=${TTL}s) — model loads on first request + idle-unloads, freeing GTT to the host."
