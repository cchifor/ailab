#!/usr/bin/env bash
# MANAGED BY ANSIBLE (role: pve_base, tag lxc_fstrim). Nightly sweep: a chunked, IO-pressure-guarded trim
# of every local-lvm volume (rootfs and mpN) of every RUNNING container on this Proxmox host, so the
# space a container frees goes back to the thin pool.
#
# WHY. The host's fstrim.timer never reaches a container's volumes, and nothing inside an unprivileged
# container may trim them. Freed blocks therefore stayed allocated in pve/data: on 2026-11-20 the LLM
# containers held 62 GiB (5002, ai-node2) and 134 GiB (5003, ai-node3) of them, mostly from model swaps
# on their /models-local volume. Thin-pool fill slows the shared consumer NVMe that also carries etcd.
#
# HOW. Host-side only, through scripts/lxc-fstrim-chunked.sh (installed by this role as
# /usr/local/sbin/lxc-fstrim-chunked.sh), one volume at a time: the volume is mounted privately on the
# host, never through the container, and trimmed CHUNK_GB at a time with the IO-pressure guard and the
# pauses of that script. This sweep only chooses the volumes, their order and the deadline:
# - containers in `pct list` with status running, minus PVE_LXC_FSTRIM_EXCLUDE and any that hold a lock
#   (backup, migrate, snapshot, ...): a stopped container's filesystem is never mounted by this sweep, and
#   each container is checked again right before each of its volumes (a failed read of its config is a
#   failure and the volume is skipped: a lock that could not be checked is not assumed absent);
# - their `rootfs` and `mpN` volumes on a storage in PVE_LXC_FSTRIM_STORAGES (local-lvm); bind mounts
#   (a host path, no storage) and read-only mount points are skipped;
# - DEADLINE = start + PVE_LXC_FSTRIM_RUN_MAX_S, passed down: no chunk starts that could not end by then.
# PROGRESS (PVE_LXC_FSTRIM_STATE_DIR, the unit's StateDirectory): per volume, the inner script's cursor
# (<ctid>-<key>.cursor, CURSOR_FILE), so a volume the deadline cuts is continued at that chunk the next night
# and every pass reaches its tail, and the time its last pass completed (<ctid>-<key>.complete). Volumes go
# least recently completed first, so one that keeps running out of time cannot starve the others.
# The inner script's lock (/run/lxc-fstrim-chunked.lock) is shared with the registry LXC's weekly trim on
# ai-node1 (registry-lxc-fstrim.service, docs/runbooks/registry-cache.md), so the two never trim at once:
# a volume whose trim finds the lock held exits 7 and is skipped for the night. Its stop file
# (/run/lxc-fstrim-chunked.stop) stops this sweep too; one already there when the sweep starts is a leftover
# and makes it fail (exit 3) rather than quietly skip every night.
#
# The inner script's output goes to the journal as it comes (one line per chunk) and to a file the sweep
# reads its total from. The beacon (node_exporter textfile, pve_lxc_fstrim.prom) carries the run's result
# and, per volume, the bytes ext4 reported trimmed, the exit code and the last complete pass.
# pve_lxc_fstrim_last_success_timestamp_seconds is the oldest of the volumes' last complete passes, i.e.
# every volume has completed a pass since then (now when there is nothing to trim); while a volume has
# never completed one, the previous value is kept.
#
# Exit codes:
#   0    no volume failed (a complete pass, or a partial one: deadline, lock held, stop file)
#   1    a volume's trim failed (lxc-fstrim-chunked.sh exit 1-6), pct could not list or read a container,
#        or the state directory is unusable
#   2    invalid settings
#   3    the stop file was already there when the sweep started (a leftover: remove it)
#   7    another sweep holds the lock (no beacon is written: that run owns it)
#   143/130  SIGTERM/SIGINT (after the volume in flight released its mount)
set -uo pipefail

# The role's settings. Exported (set -a): the inner script reads its own (CHUNK_GB, PAUSE_S, PAUSE_IDLE_S,
# PSI_MAX, PSI_WAIT_MAX_S, SEC_PER_GB) from the environment. They win over the caller's environment; a hand
# run with other values points PVE_LXC_FSTRIM_ENV_FILE at /dev/null.
ENV_FILE="${PVE_LXC_FSTRIM_ENV_FILE:-/etc/default/pve-lxc-fstrim}"
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

STORAGES="${PVE_LXC_FSTRIM_STORAGES:-local-lvm}"   # storage ids, space-separated
EXCLUDE="${PVE_LXC_FSTRIM_EXCLUDE:-}"               # container ids, space-separated
RUN_MAX_S="${PVE_LXC_FSTRIM_RUN_MAX_S:-780}"        # the run's budget; DEADLINE = start + this
TEXTFILE_DIR="${PVE_LXC_FSTRIM_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
STATE_DIR="${PVE_LXC_FSTRIM_STATE_DIR:-/var/lib/pve-lxc-fstrim}"
INNER="${PVE_LXC_FSTRIM_INNER:-/usr/local/sbin/lxc-fstrim-chunked.sh}"
LOCK="${PVE_LXC_FSTRIM_LOCK_FILE:-/run/pve-lxc-fstrim.lock}"
# Shared with lxc-fstrim-chunked.sh (its default), so one `touch` stops both.
STOP_FILE="${STOP_FILE:-/run/lxc-fstrim-chunked.stop}"
# The inner script's own settings, read here only to cost a chunk against the deadline. The rest
# (PAUSE_S, PAUSE_IDLE_S, PSI_MAX, PSI_WAIT_MAX_S) passes through the environment untouched.
CHUNK_GB="${CHUNK_GB:-8}"
SEC_PER_GB="${SEC_PER_GB:-30}"
export STOP_FILE CHUNK_GB SEC_PER_GB

log() { echo "$(date -u +%FT%TZ) pve-lxc-fstrim: $*"; }
now_s() { date +%s; }
posint() { [[ "${1:-}" =~ ^[1-9][0-9]{0,6}$ ]]; }

posint "$RUN_MAX_S" || { log "invalid PVE_LXC_FSTRIM_RUN_MAX_S='$RUN_MAX_S' (positive integer seconds)"; exit 2; }
posint "$CHUNK_GB" || { log "invalid CHUNK_GB='$CHUNK_GB'"; exit 2; }
posint "$SEC_PER_GB" || { log "invalid SEC_PER_GB='$SEC_PER_GB'"; exit 2; }
for s in $STORAGES; do [[ "$s" =~ ^[A-Za-z][A-Za-z0-9._-]*$ ]] || { log "invalid storage id '$s'"; exit 2; }; done
for c in $EXCLUDE; do [[ "$c" =~ ^[1-9][0-9]*$ ]] || { log "invalid container id '$c' in PVE_LXC_FSTRIM_EXCLUDE"; exit 2; }; done
[ -x "$INNER" ] || { log "$INNER is missing or not executable"; exit 2; }

exec 9>"$LOCK" || { log "cannot open lock file $LOCK"; exit 1; }
flock -n 9 || { log "another sweep holds $LOCK - not starting a second one"; exit 7; }

start="$(now_s)"
deadline=$(( start + RUN_MAX_S ))
# Per-volume results, "<ctid> <key> <rc> <bytes>" (rc - = not attempted).
declare -a RESULTS=() VOLS=()
total=0; failed=0; incomplete=0

complete_of() { # <ctid> <key> -> the time its last pass completed, or nothing
  local t
  t="$(head -n 1 "$STATE_DIR/$1-$2.complete" 2>/dev/null)"
  [[ "$t" =~ ^[0-9]+$ ]] && printf '%s' "$t"
}
prom_escape() { local v="${1//\\/\\\\}"; v="${v//\"/\\\"}"; printf '%s' "$v"; }
write_beacon() { # <rc> <reason>
  local rc="$1" why="$2" # before any `set --` below
  [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/pve_lxc_fstrim.prom" tmp end last_ok r v t min all=1
  end="$(now_s)"
  last_ok="$(sed -nE 's/^pve_lxc_fstrim_last_success_timestamp_seconds ([0-9]+)$/\1/p' "$f" 2>/dev/null | tail -1)"
  if [ "${listed:-0}" = 1 ]; then
    if [ "${#VOLS[@]}" -eq 0 ]; then
      # nothing to trim is a success, unless a container's config could not be read
      [ "$failed" -eq 0 ] && last_ok="$end"
    else
      min=""
      for v in "${VOLS[@]}"; do
        set -- $v; t="$(complete_of "$1" "$2")"
        [ -n "$t" ] || { all=0; break; }
        { [ -z "$min" ] || [ "$t" -lt "$min" ]; } && min="$t"
      done
      [ "$all" = 1 ] && last_ok="$min"
    fi
  fi
  tmp="$(mktemp "$TEXTFILE_DIR/.pve_lxc_fstrim.prom.XXXXXX" 2>/dev/null)" || return 0
  {
    echo "# HELP pve_lxc_fstrim_last_run_timestamp_seconds When the nightly LXC trim sweep last ended."
    echo "# TYPE pve_lxc_fstrim_last_run_timestamp_seconds gauge"
    echo "pve_lxc_fstrim_last_run_timestamp_seconds $end"
    echo "# HELP pve_lxc_fstrim_last_run_duration_seconds How long the last sweep ran."
    echo "# TYPE pve_lxc_fstrim_last_run_duration_seconds gauge"
    echo "pve_lxc_fstrim_last_run_duration_seconds $(( end - start ))"
    echo "# HELP pve_lxc_fstrim_last_run_exit_code The last sweep's exit code (0 = no volume failed)."
    echo "# TYPE pve_lxc_fstrim_last_run_exit_code gauge"
    echo "pve_lxc_fstrim_last_run_exit_code $rc"
    echo "# HELP pve_lxc_fstrim_last_run_reason Why the last sweep ended: done (every volume completed), partial, failed, stopped, terminated."
    echo "# TYPE pve_lxc_fstrim_last_run_reason gauge"
    echo "pve_lxc_fstrim_last_run_reason{reason=\"$why\"} 1"
    echo "# HELP pve_lxc_fstrim_trimmed_bytes Bytes ext4 reported trimmed by the last sweep (submitted to the thin pool)."
    echo "# TYPE pve_lxc_fstrim_trimmed_bytes gauge"
    echo "pve_lxc_fstrim_trimmed_bytes $total"
    echo "# HELP pve_lxc_fstrim_volumes Volumes the last sweep found to trim."
    echo "# TYPE pve_lxc_fstrim_volumes gauge"
    echo "pve_lxc_fstrim_volumes ${#VOLS[@]}"
    if [ -n "$last_ok" ]; then
      echo "# HELP pve_lxc_fstrim_last_success_timestamp_seconds Every volume has completed a pass since this time (the oldest last complete pass)."
      echo "# TYPE pve_lxc_fstrim_last_success_timestamp_seconds gauge"
      echo "pve_lxc_fstrim_last_success_timestamp_seconds $last_ok"
    fi
    if [ "${#RESULTS[@]}" -gt 0 ]; then
      echo "# HELP pve_lxc_fstrim_volume_exit_code lxc-fstrim-chunked.sh's exit code for this volume in the last sweep (-1 = not reached)."
      echo "# TYPE pve_lxc_fstrim_volume_exit_code gauge"
      for r in "${RESULTS[@]}"; do
        set -- $r
        echo "pve_lxc_fstrim_volume_exit_code{ctid=\"$1\",volume=\"$(prom_escape "$2")\"} $([ "$3" = - ] && echo -1 || echo "$3")"
      done
      echo "# HELP pve_lxc_fstrim_volume_trimmed_bytes Bytes ext4 reported trimmed on this volume in the last sweep."
      echo "# TYPE pve_lxc_fstrim_volume_trimmed_bytes gauge"
      for r in "${RESULTS[@]}"; do
        set -- $r
        echo "pve_lxc_fstrim_volume_trimmed_bytes{ctid=\"$1\",volume=\"$(prom_escape "$2")\"} $4"
      done
    fi
    if [ "${#VOLS[@]}" -gt 0 ]; then
      echo "# HELP pve_lxc_fstrim_volume_last_complete_timestamp_seconds When this volume's last pass reached its last chunk."
      echo "# TYPE pve_lxc_fstrim_volume_last_complete_timestamp_seconds gauge"
      for v in "${VOLS[@]}"; do
        set -- $v; t="$(complete_of "$1" "$2")"
        [ -n "$t" ] && echo "pve_lxc_fstrim_volume_last_complete_timestamp_seconds{ctid=\"$1\",volume=\"$(prom_escape "$2")\"} $t"
      done
    fi
  } > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}
finish() { # <rc> <reason> <message>
  trap - TERM INT
  log "$3 - exit $1 ($2): volumes=${#VOLS[@]} trimmed_total=$total failed=$failed"
  write_beacon "$1" "$2"
  [ -n "${outf:-}" ] && rm -f "$outf"
  exit "$1"
}

# The inner script runs inside a tracked child, so a SIGTERM/SIGINT reaches this trap at once. systemd's
# control-group kill signals the inner script too: it waits for its in-flight FITRIM and releases its mount;
# this waits for it, then records the stop.
child=""
on_signal() {
  trap - TERM INT
  log "terminated by signal - waiting for the volume in flight ${child:+(pid $child) }to release its mount"
  [ -n "$child" ] && wait "$child" 2>/dev/null
  finish "$1" terminated "stopped by signal"
}
trap 'on_signal 143' TERM
trap 'on_signal 130' INT

if [ -e "$STOP_FILE" ]; then
  finish 3 stopped "the stop file $STOP_FILE was already there at the start ($(date -u -r "$STOP_FILE" +%FT%TZ 2>/dev/null)): a leftover? remove it (rm -f $STOP_FILE) for the trim to run"
fi
mkdir -p "$STATE_DIR" 2>/dev/null && [ -w "$STATE_DIR" ] || finish 1 failed "the state directory $STATE_DIR is not writable"

# ---- choose the volumes ---------------------------------------------------------------------------
in_list() { local x="$1"; shift; local y; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }
listing="$(pct list 2>/dev/null)" || finish 1 failed "pct list failed"
# shellcheck disable=SC2207
running=($(awk 'NR > 1 && $2 == "running" { print $1 }' <<< "$listing" | sort -n))
declare -a FOUND=()
for ct in "${running[@]}"; do
  [[ "$ct" =~ ^[0-9]+$ ]] || continue
  # shellcheck disable=SC2086
  if in_list "$ct" $EXCLUDE; then log "ct $ct excluded (PVE_LXC_FSTRIM_EXCLUDE)"; continue; fi
  cfg="$(pct config "$ct" 2>/dev/null)" || { log "pct config $ct failed"; failed=$((failed + 1)); continue; }
  lock="$(sed -nE '/^\[/q; s/^lock: (.*)$/\1/p' <<< "$cfg")"
  if [ -n "$lock" ]; then log "ct $ct holds lock '$lock' - skipped tonight"; incomplete=1; continue; fi
  # The main section only (a snapshot's copy follows a `[name]` line); rootfs and mpN, nothing else.
  while IFS= read -r line; do
    [[ "$line" =~ ^\[ ]] && break
    [[ "$line" =~ ^(rootfs|mp[0-9]+):\ ([^,]+)(,(.*))?$ ]] || continue
    key="${BASH_REMATCH[1]}"; volid="${BASH_REMATCH[2]}"; opts=",${BASH_REMATCH[4]},"
    if [[ "$volid" != *:* ]]; then log "ct $ct $key: bind mount $volid - skipped"; continue; fi
    # shellcheck disable=SC2086
    if ! in_list "${volid%%:*}" $STORAGES; then log "ct $ct $key: $volid not on ${STORAGES// /,} - skipped"; continue; fi
    if [[ "$opts" == *,ro=1,* ]]; then log "ct $ct $key: read-only mount point - skipped"; continue; fi
    FOUND+=("$ct $key")
  done <<< "$cfg"
done
# Least recently completed first (never = 0); ties keep the listing order.
while read -r _ n; do VOLS+=("${FOUND[$n]}"); done < <(
  for n in "${!FOUND[@]}"; do
    set -- ${FOUND[$n]}; t="$(complete_of "$1" "$2")"
    echo "${t:-0} $n"
  done | sort -n -k1,1 -k2,2
)
listed=1
log "start: ${#VOLS[@]} volume(s) on ${STORAGES// /,} in ${#running[@]} running container(s); deadline $(date -u -d "@$deadline" +%TZ 2>/dev/null || echo "$deadline")"

# ---- trim them, one at a time ---------------------------------------------------------------------
outf="$(mktemp "${TMPDIR:-/tmp}/pve-lxc-fstrim.XXXXXX")" || finish 1 failed "mktemp failed"
reason=""
for v in "${VOLS[@]}"; do
  set -- $v; ct="$1"; key="$2"
  if [ -n "$reason" ]; then RESULTS+=("$ct $key - 0"); continue; fi
  if [ -e "$STOP_FILE" ]; then reason=stopped; RESULTS+=("$ct $key - 0"); log "stop file $STOP_FILE present - stopping"; continue; fi
  if [ $(( $(now_s) + CHUNK_GB * SEC_PER_GB )) -gt "$deadline" ]; then
    reason=deadline; RESULTS+=("$ct $key - 0"); log "deadline reached - the remaining volumes wait for the next night"; continue
  fi
  st="$(pct status "$ct" 2>/dev/null)"
  # Read the config again, and check that the read worked: a lock that could not be checked is not absent.
  if ! cfg="$(pct config "$ct" 2>/dev/null)"; then
    log "ct $ct: pct config failed - $key skipped (its lock could not be checked)"
    RESULTS+=("$ct $key - 0"); failed=$((failed + 1)); continue
  fi
  lock="$(sed -nE '/^\[/q; s/^lock: (.*)$/\1/p' <<< "$cfg")"
  if [ "$st" != "status: running" ] || [ -n "$lock" ]; then
    log "ct $ct is no longer running or now holds a lock (${st:-no status}${lock:+, lock $lock}) - $key skipped"
    RESULTS+=("$ct $key - 0"); incomplete=1; continue
  fi
  log "ct $ct $key: trimming"
  cursor="$STATE_DIR/$ct-$key.cursor"
  # Live to the journal and into $outf. tee ignores TERM (an ignored signal survives exec), so a stop cannot kill
  # it under the inner script, whose last lines (and unmount) would then die of SIGPIPE; it ends at EOF. The
  # subshell's `:` trap makes it wait for the pipeline, then exit with the inner script's status (pipefail).
  (
    trap : TERM INT
    DEADLINE="$deadline" CURSOR_FILE="$cursor" "$INNER" "$ct" "$key" 2>&1 | (trap '' TERM INT; exec tee "$outf")
    exit $?
  ) &
  child=$!; wait "$child"; rc=$?; child=""
  bytes="$(grep -oE 'trimmed_total=[0-9]+' "$outf" | tail -1 | cut -d= -f2)"
  bytes="${bytes:-0}"; total=$(( total + bytes ))
  RESULTS+=("$ct $key $rc $bytes")
  # Complete = the inner script reached the volume's last chunk, i.e. exit 0 and no cursor left behind.
  if [ "$rc" -eq 0 ] && [ ! -e "$cursor" ]; then
    printf '%s\n' "$(now_s)" > "$STATE_DIR/$ct-$key.complete" || log "warning: cannot record the complete pass of ct $ct $key"
  else
    incomplete=1
  fi
  case "$rc" in
    0) ;;
    7) log "ct $ct $key: another trim holds the shared lock (the registry's weekly run?) - skipped tonight" ;;
    8) reason=deadline ;;
    *) failed=$((failed + 1)); log "ct $ct $key: lxc-fstrim-chunked.sh exit $rc" ;;
  esac
  [ -e "$STOP_FILE" ] && [ -z "$reason" ] && reason=stopped
done

if [ "$failed" -gt 0 ]; then finish 1 failed "done with failures"; fi
case "$reason" in
  stopped) finish 0 stopped "stop file" ;;
  deadline) finish 0 partial "deadline" ;;
esac
[ "$incomplete" -eq 0 ] && finish 0 done "every volume completed"
finish 0 partial "partial pass"
