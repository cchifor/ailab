#!/usr/bin/env bash
# ec-fan-profile -- put the CPU fans of a Bosgame M5 (ec_su_axb35 driver) on a temperature curve, or hand
# them back to the EC firmware. Deployed by the pve_ec_fan role as /usr/local/sbin/ec-fan-profile and run
# by ec-fan-profile.service:
#
#   apply   for each configured fan: write the rampup and rampdown curves, read them back, then set the
#           mode to curve. Curves first, because switching to curve picks the starting level from the
#           rampup curve in effect at that moment. From then on the driver's 1 s worker moves the level
#           with the EC temperature, so this runs once per module load (ExecStart).
#   reset   set each configured fan back to auto, the EC's own curve (ExecStop: stopping or disabling the
#           service hands the fans back to the firmware). Nothing to do when the module is not loaded:
#           unloading it already reset every fan to auto.
#
# Only fan1 and fan2, the CPU fans. fan3 has no known control register (the EC accepts writes to it and
# ignores them), so it is refused here, not merely left out of the config.
#
# Environment (ec-fan-profile.service reads /etc/default/ec-fan-profile, rendered from the role vars):
#   EC_FAN_FANS            fans to manage, e.g. "1 2"
#   EC_FAN_RAMPUP_CURVE    five deg C thresholds for levels 1-5, e.g. 35,50,60,68,95 (0-100, rising)
#   EC_FAN_RAMPDOWN_CURVE  five deg C thresholds, each below the rampup value of the same level
#   EC_FAN_SYSFS           the driver's class dir (default /sys/class/ec_su_axb35)
#   EC_FAN_WAIT_S          seconds apply waits for the fan dirs to appear (default 10)
set -euo pipefail

SYSFS="${EC_FAN_SYSFS:-/sys/class/ec_su_axb35}"
FANS="${EC_FAN_FANS:-}"
RAMPUP="${EC_FAN_RAMPUP_CURVE:-}"
RAMPDOWN="${EC_FAN_RAMPDOWN_CURVE:-}"
WAIT_S="${EC_FAN_WAIT_S:-10}"

log() { echo "ec-fan-profile: $*"; }
die() {
  echo "ec-fan-profile: $*" >&2
  exit 1
}

check_fans() {
  local f
  [ -n "${FANS// /}" ] || die "EC_FAN_FANS is empty"
  for f in $FANS; do
    case "$f" in
      1 | 2) ;;
      3) die "fan3 is firmware-controlled (no known control register); refusing to write it" ;;
      *) die "unknown fan '$f': only 1 and 2 (the CPU fans) can be managed" ;;
    esac
  done
}

# Five integers 0-100, no leading zeros (the driver prints them back without), strictly rising.
check_curve() { # $1 name, $2 value
  local v prev=-1
  local -a vals
  [[ "$2" =~ ^(0|[1-9][0-9]*)(,(0|[1-9][0-9]*)){4}$ ]] ||
    die "$1='$2': need five comma-separated integers"
  IFS=, read -ra vals <<< "$2"
  for v in "${vals[@]}"; do
    ((v <= 100)) || die "$1='$2': $v is above 100"
    ((v > prev)) || die "$1='$2': the thresholds must rise strictly"
    prev=$v
  done
}

check_curves() {
  local i
  local -a up down
  check_curve EC_FAN_RAMPUP_CURVE "$RAMPUP"
  check_curve EC_FAN_RAMPDOWN_CURVE "$RAMPDOWN"
  IFS=, read -ra up <<< "$RAMPUP"
  IFS=, read -ra down <<< "$RAMPDOWN"
  for i in 0 1 2 3 4; do
    ((down[i] < up[i])) ||
      die "level $((i + 1)): rampdown ${down[i]} must be below rampup ${up[i]} (or the fan oscillates)"
  done
}

fans_present() {
  local f
  for f in $FANS; do
    [ -e "$SYSFS/fan$f/mode" ] || return 1
  done
}

write() { # $1 attribute path, $2 value
  printf '%s' "$2" > "$1" || die "writing '$2' to $1 failed"
  log "${1#"$SYSFS"/} <- $2"
}

expect() { # $1 attribute path, $2 expected content
  local got
  got=$(< "$1") || die "reading $1 failed"
  [ "$got" = "$2" ] || die "$1 reads '$got' after writing '$2'"
}

apply() {
  local f d i
  check_fans
  check_curves
  for ((i = 0; ; i++)); do
    fans_present && break
    ((i < WAIT_S)) || die "no fan dirs under $SYSFS after ${WAIT_S}s: is ec_su_axb35 loaded? (lsmod | grep ec_su_axb35)"
    sleep 1
  done
  for f in $FANS; do
    d="$SYSFS/fan$f"
    write "$d/rampup_curve" "$RAMPUP"
    write "$d/rampdown_curve" "$RAMPDOWN"
    expect "$d/rampup_curve" "$RAMPUP"
    expect "$d/rampdown_curve" "$RAMPDOWN"
    write "$d/mode" curve
    expect "$d/mode" curve
  done
}

reset() {
  local f rc=0
  check_fans
  if [ ! -d "$SYSFS" ]; then
    log "$SYSFS is absent (module not loaded): the EC already runs every fan on its own curve"
    return 0
  fi
  for f in $FANS; do
    if [ ! -e "$SYSFS/fan$f/mode" ]; then
      echo "ec-fan-profile: $SYSFS/fan$f/mode is missing" >&2
      rc=1
    elif printf '%s' auto > "$SYSFS/fan$f/mode"; then
      log "fan$f/mode <- auto"
    else
      echo "ec-fan-profile: writing auto to $SYSFS/fan$f/mode failed" >&2
      rc=1
    fi
  done
  return "$rc"
}

case "${1:-}" in
  apply) apply ;;
  reset) reset ;;
  *) die "usage: ec-fan-profile apply|reset" ;;
esac
