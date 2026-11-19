#!/usr/bin/env bash
# ec-fan-textfile -- publish the Bosgame M5 EC's fan and temperature readings (ec_su_axb35 driver) for
# node_exporter's textfile collector. Deployed by the pve_ec_fan role as /usr/local/sbin/ec-fan-textfile;
# ec-fan-textfile.timer runs it every 30 s. It only READS the driver's sysfs (all three fans, fan3 too).
#
# Writes <dir>/ec_fan.prom in one rename (a temp file in the same dir; node_exporter reads only *.prom):
#   ec_fan_driver_loaded               1 when the driver's class dir exists, else 0 and no fan series
#   ec_fan_rpm{fan}                    fan speed; fan3 reads 0 whenever the firmware stops it (normal)
#   ec_fan_level{fan}                  0 (off) to 5 (100 %)
#   ec_fan_mode_info{fan,mode}         1; mode is auto, fixed or curve
#   ec_temp_celsius                    the EC temperature the curve follows
#   ec_apu_power_mode_info{mode}       1; quiet, balanced or performance (set in the BIOS; never written)
#   ec_fan_read_errors                 reads that failed or did not parse in this run (left out above)
#   ec_fan_textfile_timestamp_seconds  when this file was written (the staleness alert reads it)
#
# Environment: EC_FAN_SYSFS (default /sys/class/ec_su_axb35), EC_FAN_TEXTFILE_DIR (default
# /var/lib/prometheus/node-exporter), EC_FAN_NOW (test seam: the timestamp to write).
set -u

SYSFS="${EC_FAN_SYSFS:-/sys/class/ec_su_axb35}"
DIR="${EC_FAN_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
NOW="${EC_FAN_NOW:-$(date +%s)}"
OUT="$DIR/ec_fan.prom"

errors=0
VALUE=""

# Read one sysfs value into VALUE; count a failed read or a value that does not match $2. `read` returns
# non-zero at an EOF without a newline but still fills VALUE, so only the match decides; a file that is
# missing or refuses the read (the driver returns EINVAL for an unknown power mode) leaves VALUE empty.
read_value() { # $1 path, $2 regex
  VALUE=""
  { IFS= read -r VALUE < "$1"; } 2> /dev/null
  [[ "$VALUE" =~ $2 ]] && return 0
  errors=$((errors + 1))
  return 1
}

# One block per metric family (HELP, TYPE, samples), printed only when it has samples.
declare -A HELP=(
  [ec_fan_driver_loaded]="1 when the ec_su_axb35 driver is loaded (its sysfs class dir exists)."
  [ec_fan_rpm]="Fan speed reported by the EC, in rpm."
  [ec_fan_level]="Fan level set in the EC: 0 off, 1-5 = 20-100 percent."
  [ec_fan_mode_info]="Fan control mode in the driver (auto = EC firmware curve, curve = driver curve, fixed)."
  [ec_temp_celsius]="EC temperature that the fan curves follow, in degrees Celsius."
  [ec_apu_power_mode_info]="APU power mode set in the EC (BIOS setting, read only here)."
  [ec_fan_read_errors]="sysfs reads that failed or did not parse in the last run."
  [ec_fan_textfile_timestamp_seconds]="Unix time at which ec-fan-textfile last wrote this file."
)
FAMILIES=(ec_fan_driver_loaded ec_fan_rpm ec_fan_level ec_fan_mode_info ec_temp_celsius
  ec_apu_power_mode_info ec_fan_read_errors ec_fan_textfile_timestamp_seconds)
declare -A SAMPLES=()
sample() { SAMPLES[$1]+="$1$2 $3"$'\n'; } # $1 family, $2 labels ("" or {..}), $3 value

if [ -d "$SYSFS" ]; then
  sample ec_fan_driver_loaded "" 1
  for fan in 1 2 3; do
    d="$SYSFS/fan$fan"
    read_value "$d/rpm" '^[0-9]+$' && sample ec_fan_rpm "{fan=\"$fan\"}" "$VALUE"
    read_value "$d/level" '^[0-5]$' && sample ec_fan_level "{fan=\"$fan\"}" "$VALUE"
    read_value "$d/mode" '^(auto|fixed|curve)$' && sample ec_fan_mode_info "{fan=\"$fan\",mode=\"$VALUE\"}" 1
  done
  read_value "$SYSFS/temp1/temp" '^[0-9]+$' && sample ec_temp_celsius "" "$VALUE"
  read_value "$SYSFS/apu/power_mode" '^(quiet|balanced|performance)$' &&
    sample ec_apu_power_mode_info "{mode=\"$VALUE\"}" 1
else
  sample ec_fan_driver_loaded "" 0
fi
sample ec_fan_read_errors "" "$errors"
sample ec_fan_textfile_timestamp_seconds "" "$NOW"

tmp="$DIR/.ec_fan.prom.$$"
trap 'rm -f "$tmp"' EXIT
{
  for family in "${FAMILIES[@]}"; do
    [ -n "${SAMPLES[$family]:-}" ] || continue
    printf '# HELP %s %s\n# TYPE %s gauge\n%s' "$family" "${HELP[$family]}" "$family" "${SAMPLES[$family]}"
  done
} > "$tmp" || {
  echo "ec-fan-textfile: cannot write $tmp" >&2
  exit 1
}
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT" || {
  echo "ec-fan-textfile: cannot rename $tmp to $OUT" >&2
  exit 1
}
