#!/usr/bin/env bash
###############################################################################
# Emit amdgpu iGPU metrics as Prometheus text for node_exporter's textfile
# collector. rocm-smi/amd-smi are unreliable on gfx1151 — read amdgpu sysfs
# directly (works on this APU). Privileged LXC sees the host's /sys.
#
# Usage: amdgpu-textfile.sh <textfile-dir>   (default /var/lib/prometheus/node-exporter)
# Writes <dir>/amdgpu.prom ATOMICALLY (tmp + mv) — a partial .prom corrupts the scrape.
###############################################################################
set -u
OUT_DIR="${1:-/var/lib/prometheus/node-exporter}"
mkdir -p "$OUT_DIR"

# Pick the amdgpu render card (the one with a gpu_busy_percent file).
DEV=""
for d in /sys/class/drm/card*/device; do
  [ -r "$d/gpu_busy_percent" ] && { DEV="$d"; break; }
done
[ -z "$DEV" ] && exit 0

read_n() { cat "$1" 2>/dev/null || echo ""; }
emit() { # name, help, type, value
  [ -n "$4" ] && printf '# HELP %s %s\n# TYPE %s %s\n%s{card="0"} %s\n' "$1" "$2" "$1" "$3" "$1" "$4"
}

HWMON="$(echo "$DEV"/hwmon/hwmon* 2>/dev/null | awk '{print $1}')"

TMP="$(mktemp "${OUT_DIR}/amdgpu.XXXXXX")"
{
  emit amdgpu_gpu_busy_percent   "iGPU utilization (%)"                gauge "$(read_n "$DEV/gpu_busy_percent")"
  emit amdgpu_vram_used_bytes    "Carved VRAM heap used (bytes)"       gauge "$(read_n "$DEV/mem_info_vram_used")"
  emit amdgpu_vram_total_bytes   "Carved VRAM heap total (bytes)"      gauge "$(read_n "$DEV/mem_info_vram_total")"
  emit amdgpu_gtt_used_bytes     "GTT (shared/system) heap used bytes" gauge "$(read_n "$DEV/mem_info_gtt_used")"
  emit amdgpu_gtt_total_bytes    "GTT (shared/system) heap total bytes" gauge "$(read_n "$DEV/mem_info_gtt_total")"
  if [ -n "${HWMON:-}" ] && [ -d "$HWMON" ]; then
    emit amdgpu_temp_millicelsius "iGPU edge temperature (m°C)"         gauge "$(read_n "$HWMON/temp1_input")"
    emit amdgpu_power_microwatts  "iGPU average power (µW)"             gauge "$(read_n "$HWMON/power1_average")"
    emit amdgpu_sclk_hz           "iGPU shader clock (Hz)"              gauge "$(read_n "$HWMON/freq1_input")"
  fi
} > "$TMP" 2>/dev/null

chmod 0644 "$TMP" # node_exporter runs as a non-root user and must be able to read it
mv -f "$TMP" "${OUT_DIR}/amdgpu.prom"
