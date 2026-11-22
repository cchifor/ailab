#!/usr/bin/env bash
# ec-fan-governor -- keep the CPU fans of a Bosgame M5 (ec_su_axb35 driver) at one steady level through
# short heat bursts. Deployed by the pve_ec_fan role as /usr/local/sbin/ec-fan-governor and run by
# ec-fan-governor.service, next to the curve that ec-fan-profile sets.
#
# Why: the driver's curve moves the level every second on the instantaneous EC temperature. Under CI that
# temperature jumps 10-15 C in seconds and falls back as fast (ai-node2, 2026-11-22: 90 -> 92 -> 82 -> 75 C in
# 16 s), so a curve on its own spins the fans up for heat that is gone before the extra airflow does
# anything. This daemon decides levels 3-5 (60-100 %) on a SMOOTHED temperature instead:
#
#   up    from level L (2-4) to L+1 when the exponential moving average of the EC temperature (time
#         constant EC_FAN_GOV_TAU_S) reaches the up threshold for L+1: sustained heat, not a burst;
#   down  from level L (3-5) to L-1 when the average is at or below the down threshold for L, the last
#         step UP (the governor's or the driver's) was at least EC_FAN_GOV_HOLD_S ago, and the
#         instantaneous temperature is below the curve's own rampup value for L (or the driver would put it
#         straight back). The hold counts from the last step up, not from the last change, so a fan that
#         is no longer needed high comes down level after level instead of waiting the hold at each one.
#
# The driver keeps the rest, so the kernel stays the safety net whatever this process does: levels 0-2
# (the curve's values for levels 1-2), and an immediate step up on the instantaneous temperature (the
# curve's rampup values for levels 3-5, which must sit above this daemon's up thresholds). The curve's
# rampdown values for levels 3-5 must sit below this daemon's down thresholds, so the driver does not undo
# a hold; the cost is that if this process dies at level 3+, the fans stay there (louder, never hotter)
# until systemd restarts it or the EC cools to those values. `check` refuses a config that breaks either.
#
# It writes only `level`, only for a fan in EC_FAN_FANS whose mode is curve, and only to change it. It
# never touches fan3, the mode, the curves or the power mode (ec-fan-profile owns those).
#
#   run       the daemon. Every EC_FAN_GOV_METRICS_EVERY ticks it also writes
#             <EC_FAN_TEXTFILE_DIR>/ec_fan_governor.prom for node_exporter (in one rename):
#               ec_fan_governor_average_celsius    the smoothed EC temperature it decides on
#               ec_fan_governor_steps_total{direction}  level changes it made since it started (up, down)
#               ec_fan_governor_heartbeat_timestamp_seconds  when it wrote the file (the staleness alert)
#   simulate  replay instantaneous EC temperatures (one integer per line on stdin, one line per tick)
#             through the driver's curve AND the governor, without sysfs, and print "tick temp average
#             level" per line. For the tests, and to try a config against a recorded trace. The fan starts
#             at EC_FAN_GOV_SIM_LEVEL (default 2).
#   check     validate the config, write nothing: exit 0 when it is good, 78 (EX_CONFIG, and why) when not.
#             `run` and `simulate` refuse a bad config with 78 too, before anything else; the unit's
#             RestartPreventExitStatus=78 keeps systemd from restarting a refused config every 5 s.
#
# Warm-up: the average starts at the first reading, so for its first EC_FAN_GOV_TAU_S of readings it does not
# step up (a restart, which every converge does, landing on a burst sample would otherwise go to 60 % at
# once and hold it). The curve's instant steps up still apply then; steps down wait for the hold anyway.
#
# Environment (ec-fan-governor.service reads /etc/default/ec-fan-profile, rendered from the role vars):
#   EC_FAN_FANS, EC_FAN_RAMPUP_CURVE, EC_FAN_RAMPDOWN_CURVE  as for ec-fan-profile (the curve it works with)
#   EC_FAN_GOV_UP            three average temperatures (deg C) that step up to level 3, 4 and 5
#   EC_FAN_GOV_DOWN          three average temperatures that step down from level 3, 4 and 5
#   EC_FAN_GOV_TAU_S         time constant of the average, seconds (default 60)
#   EC_FAN_GOV_HOLD_S        least time from a step up to a step down, seconds (default 180)
#   EC_FAN_GOV_INTERVAL_S    tick, seconds (default 1)
#   EC_FAN_SYSFS             the driver's class dir (default /sys/class/ec_su_axb35)
#   EC_FAN_TEXTFILE_DIR      node_exporter's textfile dir (default /var/lib/prometheus/node-exporter)
#   EC_FAN_GOV_METRICS_EVERY ticks between metrics writes (default 15)
#   Test seams: EC_FAN_GOV_SLEEP_S (seconds slept per tick, default the tick), EC_FAN_GOV_MAX_TICKS (stop
#   after that many ticks, default 0 = never), EC_FAN_NOW (the heartbeat timestamp to write).
set -euo pipefail

SYSFS="${EC_FAN_SYSFS:-/sys/class/ec_su_axb35}"
DIR="${EC_FAN_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
FANS="${EC_FAN_FANS:-}"
RAMPUP="${EC_FAN_RAMPUP_CURVE:-}"
RAMPDOWN="${EC_FAN_RAMPDOWN_CURVE:-}"
GOV_UP="${EC_FAN_GOV_UP:-}"
GOV_DOWN="${EC_FAN_GOV_DOWN:-}"
TAU="${EC_FAN_GOV_TAU_S:-60}"
HOLD="${EC_FAN_GOV_HOLD_S:-180}"
TICK="${EC_FAN_GOV_INTERVAL_S:-1}"
SLEEP_S="${EC_FAN_GOV_SLEEP_S:-$TICK}"
MAX_TICKS="${EC_FAN_GOV_MAX_TICKS:-0}"
METRICS_EVERY="${EC_FAN_GOV_METRICS_EVERY:-15}"
OUT="$DIR/ec_fan_governor.prom"

log() { echo "ec-fan-governor: $*"; }
DIE_RC=1 # load_config sets 78 (EX_CONFIG) while it checks
die() {
  echo "ec-fan-governor: $*" >&2
  exit "$DIE_RC"
}

# ---- config ----
declare -a RU RD UPT DNT # curve rampup/rampdown for levels 1-5 at [1..5]; governor thresholds at [3..5]

int_list() { # $1 name, $2 value, $3 count: comma-separated integers 0-100, no leading zeros, strictly rising
  local v prev=-1
  local -a vals
  [[ "$2" =~ ^(0|[1-9][0-9]*)(,(0|[1-9][0-9]*))*$ ]] || die "$1='$2': need comma-separated integers"
  IFS=, read -ra vals <<< "$2"
  ((${#vals[@]} == $3)) || die "$1='$2': need $3 values"
  for v in "${vals[@]}"; do
    ((v <= 100)) || die "$1='$2': $v is above 100"
    ((v > prev)) || die "$1='$2': the values must rise strictly"
    prev=$v
  done
}

seconds() { # $1 name, $2 value, $3 least allowed
  [[ "$2" =~ ^(0|[1-9][0-9]*)$ ]] || die "$1='$2': need a whole number of seconds"
  (($2 >= $3)) || die "$1='$2': must be at least $3"
}

load_config() {
  local i L
  local -a a
  DIE_RC=78
  [ -n "${FANS// /}" ] || die "EC_FAN_FANS is empty"
  for i in $FANS; do
    case "$i" in
      1 | 2) ;;
      *) die "fan '$i': only 1 and 2 (the CPU fans) are governed" ;;
    esac
  done
  int_list EC_FAN_RAMPUP_CURVE "$RAMPUP" 5
  int_list EC_FAN_RAMPDOWN_CURVE "$RAMPDOWN" 5
  int_list EC_FAN_GOV_UP "$GOV_UP" 3
  int_list EC_FAN_GOV_DOWN "$GOV_DOWN" 3
  seconds EC_FAN_GOV_TAU_S "$TAU" 1
  seconds EC_FAN_GOV_HOLD_S "$HOLD" 0
  seconds EC_FAN_GOV_INTERVAL_S "$TICK" 1
  seconds EC_FAN_GOV_METRICS_EVERY "$METRICS_EVERY" 1
  IFS=, read -ra a <<< "$RAMPUP"
  RU=(0 "${a[@]}")
  IFS=, read -ra a <<< "$RAMPDOWN"
  RD=(0 "${a[@]}")
  IFS=, read -ra a <<< "$GOV_UP"
  UPT=(0 0 0 "${a[@]}")
  IFS=, read -ra a <<< "$GOV_DOWN"
  DNT=(0 0 0 "${a[@]}")
  for L in 3 4 5; do
    ((DNT[L] < UPT[L])) ||
      die "level $L: governor down ${DNT[L]} must be below its up ${UPT[L]} (or it oscillates)"
    ((UPT[L] < RU[L])) ||
      die "level $L: governor up ${UPT[L]} must be below the curve's rampup ${RU[L]} (the curve's is the instant safety step above it)"
    ((DNT[L] > RD[L])) ||
      die "level $L: governor down ${DNT[L]} must be above the curve's rampdown ${RD[L]} (or the driver steps down before the hold)"
  done
  WARMUP=$(((TAU + TICK - 1) / TICK)) # readings the average needs before it may step up
  DIE_RC=1
}

# ---- the decision ----
# One tick for one fan. Globals in: LEVEL (0-5), AVG (milli-degrees), TEMP (degrees), SINCE_UP (ticks since
# the fan last stepped up, or since the first look at it), SAMPLES (readings in the average). Out: NEXT, the
# level the governor wants (LEVEL when it leaves the fan alone).
WARMUP=0
SAMPLES=0
decide() {
  NEXT=$LEVEL
  if ((SAMPLES >= WARMUP && LEVEL >= 2 && LEVEL < 5 && AVG >= UPT[LEVEL + 1] * 1000)); then
    NEXT=$((LEVEL + 1))
  elif ((LEVEL >= 3 && SINCE_UP * TICK >= HOLD && AVG <= DNT[LEVEL] * 1000 && TEMP < RU[LEVEL])); then
    NEXT=$((LEVEL - 1))
  fi
}

# The average, in milli-degrees, rounded (not truncated) so it settles on the temperature it follows.
# At least one milli-degree toward the reading per tick: plain rounding would stall up to TAU/2 milli-degrees
# short of a steady temperature (85.971 under a steady 86), and never reach a threshold at exactly that value.
AVG=-1
average() { # $1 instantaneous temperature, whole degrees (base 10)
  local d s
  SAMPLES=$((SAMPLES + 1))
  if ((AVG < 0)); then
    AVG=$(($1 * 1000))
    return
  fi
  d=$((($1 * 1000 - AVG) * TICK))
  if ((d > 0)); then
    s=$(((d + TAU / 2) / TAU))
    AVG=$((AVG + (s > 0 ? s : 1)))
  elif ((d < 0)); then
    s=$(((-d + TAU / 2) / TAU))
    AVG=$((AVG - (s > 0 ? s : 1)))
  fi
}

# Milli-degrees as a decimal, into MILLI (printf -v: no subshell, which simulate would pay for per line).
MILLI=""
milli() { printf -v MILLI '%d.%03d' $(($1 / 1000)) $(($1 % 1000)); }

# ---- simulate ----
simulate() {
  local tick=0 line
  LEVEL="${EC_FAN_GOV_SIM_LEVEL:-2}"
  [[ "$LEVEL" =~ ^[0-5]$ ]] || die "EC_FAN_GOV_SIM_LEVEL='$LEVEL': need 0-5"
  SINCE_UP=0 # as run: a first look counts as a step up
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[0-9]{1,3}$ ]] || die "line $((tick + 1)): '$line' is not a temperature"
    TEMP=$((10#$line)) # 10#: a leading zero is not octal
    average "$TEMP"
    # the driver's worker first (once a second in the kernel), then the governor
    if ((LEVEL < 5 && TEMP >= RU[LEVEL + 1])); then
      LEVEL=$((LEVEL + 1))
      SINCE_UP=0
    elif ((LEVEL > 0 && TEMP <= RD[LEVEL])); then
      LEVEL=$((LEVEL - 1))
    fi
    decide
    if ((NEXT > LEVEL)); then SINCE_UP=0; fi
    LEVEL=$NEXT
    tick=$((tick + 1))
    milli "$AVG"
    echo "$tick $TEMP $MILLI $LEVEL"
    SINCE_UP=$((SINCE_UP + 1))
  done
}

# ---- run ----
VALUE=""
read_value() { # $1 path, $2 regex; VALUE, or return 1
  VALUE=""
  { IFS= read -r VALUE < "$1"; } 2> /dev/null
  [[ "$VALUE" =~ $2 ]]
}

STEPS_UP=0
STEPS_DOWN=0
write_metrics() {
  local tmp="$DIR/.ec_fan_governor.prom.$$"
  if ! {
    printf '# HELP ec_fan_governor_average_celsius The smoothed EC temperature ec-fan-governor decides levels 3-5 on.\n'
    printf '# TYPE ec_fan_governor_average_celsius gauge\n'
    if ((AVG >= 0)); then
      milli "$AVG"
      printf 'ec_fan_governor_average_celsius %s\n' "$MILLI"
    fi
    printf '# HELP ec_fan_governor_steps_total CPU-fan level changes ec-fan-governor made since it started.\n'
    printf '# TYPE ec_fan_governor_steps_total counter\n'
    printf 'ec_fan_governor_steps_total{direction="up"} %d\n' "$STEPS_UP"
    printf 'ec_fan_governor_steps_total{direction="down"} %d\n' "$STEPS_DOWN"
    printf '# HELP ec_fan_governor_heartbeat_timestamp_seconds Unix time at which ec-fan-governor last wrote this file.\n'
    printf '# TYPE ec_fan_governor_heartbeat_timestamp_seconds gauge\n'
    printf 'ec_fan_governor_heartbeat_timestamp_seconds %s\n' "${EC_FAN_NOW:-$EPOCHSECONDS}"
  } > "$tmp" 2> /dev/null || ! chmod 0644 "$tmp" || ! mv -f "$tmp" "$OUT"; then
    rm -f "$tmp"
    log "cannot write $OUT" # the heartbeat goes stale and ProxmoxEcFanGovernorStale says so
  fi
}

# Read a fan's state into FAN_STATE: `curve` (in curve mode, its level in FAN_LEVEL), `other` (a valid auto
# or fixed mode: ec-fan-profile stopped or restarting, a hand test, not ours to touch) or `unreadable` (the
# mode or the level could not be read or parsed: the governor cannot control that fan, and the tick counts
# as failing, so the heartbeat goes stale instead of reporting a governor that is not governing).
FAN_STATE=""
FAN_LEVEL=-1
fan_state() { # $1 fan
  FAN_LEVEL=-1
  if ! read_value "$SYSFS/fan$1/mode" '^(auto|fixed|curve)$'; then
    FAN_STATE=unreadable
  elif [ "$VALUE" != curve ]; then
    FAN_STATE=other
  elif read_value "$SYSFS/fan$1/level" '^[0-5]$'; then
    FAN_STATE=curve
    FAN_LEVEL=$VALUE
  else
    FAN_STATE=unreadable
  fi
}

run() {
  local f tick=0 temp_bad=0 failing
  local -A prev since_up fan_bad
  for f in $FANS; do
    prev[$f]=-1
    since_up[$f]=0
    fan_bad[$f]=0
  done
  trap 'exit 0' TERM INT
  log "governing fan(s) $FANS: up at average ${GOV_UP} C (levels 3,4,5), down at ${GOV_DOWN} C from ${HOLD} s after a step up, average over ${TAU} s"
  # The heartbeat says the governor is GOVERNING: it is written now, so the file exists from the start (an
  # enabled host without it is ProxmoxEcFanGovernorMissing), and then only after a tick that read the
  # temperature and wrote every level it meant to. A daemon that keeps failing lets it go stale.
  write_metrics
  while :; do
    failing=0
    if read_value "$SYSFS/temp1/temp" '^[0-9]{1,3}$'; then
      TEMP=$((10#$VALUE))
      ((temp_bad == 0)) || log "EC temperature readable again"
      temp_bad=0
      average "$TEMP"
      for f in $FANS; do
        fan_state "$f"
        if [ "$FAN_STATE" = unreadable ]; then
          ((fan_bad[$f])) || log "fan$f: cannot read its mode or level ($SYSFS/fan$f): not governing it"
          fan_bad[$f]=1
          failing=1
        elif ((fan_bad[$f])); then
          log "fan$f: mode and level readable again"
          fan_bad[$f]=0
        fi
        if [ "$FAN_STATE" != curve ]; then
          prev[$f]=-1
          continue
        fi
        LEVEL=$FAN_LEVEL
        # Up since the last tick (the driver's instant step) or a first look: the hold starts again. A step
        # down by the driver does not restart it.
        if ((prev[$f] < 0 || LEVEL > prev[$f])); then
          since_up[$f]=0
        fi
        prev[$f]=$LEVEL
        SINCE_UP=${since_up[$f]}
        decide
        if ((NEXT != LEVEL)); then
          # Read again right before the write, so a fan put in fixed or auto, or stepped up by the driver,
          # since the first read is not overwritten with a decision made on the old state (the window is
          # then one sysfs read wide, not a whole decision).
          fan_state "$f"
          if [ "$FAN_STATE" = unreadable ]; then
            failing=1
            NEXT=$LEVEL
          elif [ "$FAN_STATE" != curve ] || ((FAN_LEVEL != LEVEL)); then
            NEXT=$LEVEL # moved under us: decide again on the next tick
          fi
        fi
        if ((NEXT != LEVEL)); then
          if printf '%s' "$NEXT" > "$SYSFS/fan$f/level"; then
            milli "$AVG"
            log "fan$f: level $LEVEL -> $NEXT (average $MILLI C, EC $TEMP C, $((SINCE_UP * TICK)) s since a step up)"
            if ((NEXT > LEVEL)); then
              STEPS_UP=$((STEPS_UP + 1))
              since_up[$f]=0
            else
              STEPS_DOWN=$((STEPS_DOWN + 1))
            fi
            prev[$f]=$NEXT
          else
            log "fan$f: writing level $NEXT failed"
            failing=1
          fi
        fi
        since_up[$f]=$((since_up[$f] + 1))
      done
    else
      ((temp_bad > 0)) || log "cannot read the EC temperature ($SYSFS/temp1/temp): leaving the fans to the curve"
      temp_bad=$((temp_bad + 1))
      failing=1
    fi
    tick=$((tick + 1))
    ((tick % METRICS_EVERY != 0 || failing)) || write_metrics
    ((MAX_TICKS == 0 || tick < MAX_TICKS)) || break
    sleep "$SLEEP_S"
  done
  ((failing)) || write_metrics
}

case "${1:-}" in
  run)
    load_config
    run
    ;;
  simulate)
    load_config
    simulate
    ;;
  check) load_config ;;
  *) die "usage: ec-fan-governor run|simulate|check" ;;
esac
