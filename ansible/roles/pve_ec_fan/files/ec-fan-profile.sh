#!/usr/bin/env bash
# ec-fan-profile -- put the CPU fans of a Bosgame M5 (ec_su_axb35 driver) on a temperature curve and the
# APU in the configured power mode, or hand both back to the EC firmware. Deployed by the pve_ec_fan role as
# /usr/local/sbin/ec-fan-profile and run by ec-fan-profile.service:
#
#   apply   for each configured fan: write the rampup and rampdown curves, read them back, then set the
#           mode to curve. Curves first, because switching to curve picks the starting level from the
#           rampup curve in effect at that moment. A CPU fan NOT in the list goes back to auto, so a fan
#           dropped from the config is handed back to the firmware (systemd's ExecStop reads the NEW
#           config, so it cannot be relied on for that). Then write the APU power mode. From then on the
#           driver's 1 s worker moves the level with the EC temperature, so this runs once per module load
#           (ExecStart).
#   reset   set both CPU fans back to auto, the EC's own curve, and the APU power mode back to balanced,
#           the EC default, whatever the config says (ExecStop: stopping or disabling the service hands
#           both back to the firmware; a broken config must not keep them on the profile). Nothing to do
#           when the module is not loaded.
#   check   compare the live state with the config without writing anything: exit 0 when every
#           configured fan is in curve mode with the configured curves, every other CPU fan is in auto and
#           the power mode is the configured one; 3 (and the differences on stdout) when not; 1 on a bad
#           config. The role runs it on each converge and restarts the service on 3: a module reloaded
#           under an active service, a start that failed at boot, a value written by hand.
#
# Only fan1 and fan2, the CPU fans. fan3 is controllable through the driver too, but it is not managed by
# this role yet: it stays on the EC's own curve (owner decision pending), so it is refused in the config and
# never written, not even by reset.
#
# Environment (ec-fan-profile.service reads /etc/default/ec-fan-profile, rendered from the role vars):
#   EC_FAN_FANS            fans to manage, e.g. "1 2"
#   EC_FAN_RAMPUP_CURVE    five deg C thresholds for levels 1-5, e.g. 40,55,80,90,95 (0-100, rising)
#   EC_FAN_RAMPDOWN_CURVE  five deg C thresholds, each below the rampup value of the same level
#   EC_FAN_POWER_MODE      APU power mode: quiet, balanced or performance
#   EC_FAN_SYSFS           the driver's class dir (default /sys/class/ec_su_axb35)
#   EC_FAN_WAIT_S          seconds apply waits for the driver's dirs to appear (default 10)
set -euo pipefail

SYSFS="${EC_FAN_SYSFS:-/sys/class/ec_su_axb35}"
FANS="${EC_FAN_FANS:-}"
RAMPUP="${EC_FAN_RAMPUP_CURVE:-}"
RAMPDOWN="${EC_FAN_RAMPDOWN_CURVE:-}"
POWER_MODE="${EC_FAN_POWER_MODE:-}"
WAIT_S="${EC_FAN_WAIT_S:-10}"
CPU_FANS="1 2"
EC_DEFAULT_POWER_MODE=balanced

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
      3) die "fan3 is not managed by this role yet (it stays on the EC's own curve); refusing to write it" ;;
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

# The three values the driver's power_mode store accepts.
check_power_mode() {
  case "$POWER_MODE" in
    quiet | balanced | performance) ;;
    *) die "EC_FAN_POWER_MODE='$POWER_MODE': need quiet, balanced or performance" ;;
  esac
}

driver_present() {
  local f
  for f in $CPU_FANS; do
    [ -e "$SYSFS/fan$f/mode" ] || return 1
  done
  [ -e "$SYSFS/apu/power_mode" ]
}

managed() { [[ " $FANS " == *" $1 "* ]]; }

# No trailing newline is needed: at the pin, the driver's mode and power_mode stores compare with
# sysfs_streq (which ignores one), and its curve store splits on ',' and parses each value with kstrtoint
# (which accepts one).
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
  check_power_mode
  for ((i = 0; ; i++)); do
    driver_present && break
    ((i < WAIT_S)) || die "no fan/apu dirs under $SYSFS after ${WAIT_S}s: is ec_su_axb35 loaded? (lsmod | grep ec_su_axb35)"
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
  for f in $CPU_FANS; do
    managed "$f" && continue
    write "$SYSFS/fan$f/mode" auto
    expect "$SYSFS/fan$f/mode" auto
  done
  write "$SYSFS/apu/power_mode" "$POWER_MODE"
  expect "$SYSFS/apu/power_mode" "$POWER_MODE"
}

reset_one() { # $1 attribute path, $2 value; reports instead of exiting, so reset tries everything
  if [ ! -e "$1" ]; then
    echo "ec-fan-profile: $1 is missing" >&2
    return 1
  elif printf '%s' "$2" > "$1"; then
    log "${1#"$SYSFS"/} <- $2"
  else
    echo "ec-fan-profile: writing $2 to $1 failed" >&2
    return 1
  fi
}

reset() {
  local f rc=0
  if [ ! -d "$SYSFS" ]; then
    log "$SYSFS is absent (module not loaded): nothing to hand back"
    return 0
  fi
  for f in $CPU_FANS; do
    reset_one "$SYSFS/fan$f/mode" auto || rc=1
  done
  reset_one "$SYSFS/apu/power_mode" "$EC_DEFAULT_POWER_MODE" || rc=1
  return "$rc"
}

check() {
  local f d want got drift=0
  check_fans
  check_curves
  check_power_mode
  for f in $CPU_FANS; do
    d="$SYSFS/fan$f"
    if managed "$f"; then
      want="curve $RAMPUP $RAMPDOWN"
      got="$(cat "$d/mode" 2> /dev/null || echo missing) $(cat "$d/rampup_curve" 2> /dev/null || echo missing)"
      got="$got $(cat "$d/rampdown_curve" 2> /dev/null || echo missing)"
    else
      want=auto
      got=$(cat "$d/mode" 2> /dev/null || echo missing)
    fi
    if [ "$got" != "$want" ]; then
      echo "fan$f: is '$got', want '$want'"
      drift=1
    fi
  done
  got=$(cat "$SYSFS/apu/power_mode" 2> /dev/null || echo missing)
  if [ "$got" != "$POWER_MODE" ]; then
    echo "apu: is '$got', want '$POWER_MODE'"
    drift=1
  fi
  ((drift == 0)) || exit 3
}

case "${1:-}" in
  apply) apply ;;
  reset) reset ;;
  check) check ;;
  *) die "usage: ec-fan-profile apply|reset|check" ;;
esac
