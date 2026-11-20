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
# pauses of that script. This sweep only chooses the volumes and the deadline:
# - containers in `pct list` with status running, minus PVE_LXC_FSTRIM_EXCLUDE and any that hold a lock
#   (backup, migrate, snapshot, ...): a stopped container's filesystem is never mounted by this sweep, and
#   each container is checked again right before each of its volumes;
# - their `rootfs` and `mpN` volumes on a storage in PVE_LXC_FSTRIM_STORAGES (local-lvm); bind mounts
#   (a host path, no storage) and read-only mount points are skipped;
# - DEADLINE = start + PVE_LXC_FSTRIM_RUN_MAX_S, passed down: no chunk starts that could not end by then.
#   ext4 remembers which block groups it has trimmed since the mount, so the next night continues where
#   this one stopped instead of starting over.
# The inner script's lock (/run/lxc-fstrim-chunked.lock) is shared with the registry LXC's weekly trim on
# ai-node1 (registry-lxc-fstrim.service, docs/runbooks/registry-cache.md), so the two never trim at once:
# a volume whose trim finds the lock held exits 7 and is skipped for the night. Its stop file
# (/run/lxc-fstrim-chunked.stop) stops this sweep too.
#
# The beacon (node_exporter textfile, pve_lxc_fstrim.prom) carries the run's result and, per volume, the
# bytes ext4 reported trimmed and the exit code. pve_lxc_fstrim_last_success_timestamp_seconds moves only
# on a COMPLETE pass (every volume exit 0); a pass cut by the deadline or the lock is partial.
#
# Exit codes:
#   0    no volume failed (a complete pass, or a partial one: deadline, lock held, stop file)
#   1    a volume's trim failed (lxc-fstrim-chunked.sh exit 1-6), or pct could not list or read a container
#   2    invalid settings
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
declare -a RESULTS=()
total=0; failed=0; incomplete=0

prom_escape() { local v="${1//\\/\\\\}"; v="${v//\"/\\\"}"; printf '%s' "$v"; }
write_beacon() { # <rc> <reason>
  [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/pve_lxc_fstrim.prom" tmp end last_ok r
  end="$(now_s)"
  last_ok="$(sed -nE 's/^pve_lxc_fstrim_last_success_timestamp_seconds ([0-9]+)$/\1/p' "$f" 2>/dev/null | tail -1)"
  [ "$2" = done ] && last_ok="$end"
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
    echo "pve_lxc_fstrim_last_run_exit_code $1"
    echo "# HELP pve_lxc_fstrim_last_run_reason Why the last sweep ended: done (complete), partial, failed, stopped, terminated."
    echo "# TYPE pve_lxc_fstrim_last_run_reason gauge"
    echo "pve_lxc_fstrim_last_run_reason{reason=\"$2\"} 1"
    echo "# HELP pve_lxc_fstrim_trimmed_bytes Bytes ext4 reported trimmed by the last sweep (submitted to the thin pool)."
    echo "# TYPE pve_lxc_fstrim_trimmed_bytes gauge"
    echo "pve_lxc_fstrim_trimmed_bytes $total"
    echo "# HELP pve_lxc_fstrim_volumes Volumes the last sweep found to trim."
    echo "# TYPE pve_lxc_fstrim_volumes gauge"
    echo "pve_lxc_fstrim_volumes ${#RESULTS[@]}"
    if [ -n "$last_ok" ]; then
      echo "# HELP pve_lxc_fstrim_last_success_timestamp_seconds When a sweep last covered every volume completely."
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
  } > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}
finish() { # <rc> <reason> <message>
  trap - TERM INT
  log "$3 - exit $1 ($2): volumes=${#RESULTS[@]} trimmed_total=$total failed=$failed"
  write_beacon "$1" "$2"
  [ -n "${outf:-}" ] && rm -f "$outf"
  exit "$1"
}

# The inner script runs as a tracked child, so a SIGTERM/SIGINT reaches this trap at once. systemd's
# control-group kill signals the child too: it waits for its in-flight FITRIM and releases its mount;
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

# ---- choose the volumes ---------------------------------------------------------------------------
in_list() { local x="$1"; shift; local y; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }
listing="$(pct list 2>/dev/null)" || finish 1 failed "pct list failed"
# shellcheck disable=SC2207
running=($(awk 'NR > 1 && $2 == "running" { print $1 }' <<< "$listing" | sort -n))
declare -a VOLS=()
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
    VOLS+=("$ct $key")
  done <<< "$cfg"
done
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
  # The content decides, not a pipeline's status (sed quitting at a snapshot section can SIGPIPE pct).
  lock="$(pct config "$ct" 2>/dev/null | sed -nE '/^\[/q; s/^lock: (.*)$/\1/p')"
  if [ "$st" != "status: running" ] || [ -n "$lock" ]; then
    log "ct $ct is no longer running or now holds a lock (${st:-no status}${lock:+, lock $lock}) - $key skipped"
    RESULTS+=("$ct $key - 0"); incomplete=1; continue
  fi
  log "ct $ct $key: trimming"
  DEADLINE="$deadline" "$INNER" "$ct" "$key" > "$outf" 2>&1 &
  child=$!; wait "$child"; rc=$?; child=""
  cat "$outf"
  bytes="$(grep -oE 'trimmed_total=[0-9]+' "$outf" | tail -1 | cut -d= -f2)"
  bytes="${bytes:-0}"; total=$(( total + bytes ))
  RESULTS+=("$ct $key $rc $bytes")
  case "$rc" in
    0) ;;
    7) incomplete=1; log "ct $ct $key: another trim holds the shared lock (the registry's weekly run?) - skipped tonight" ;;
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
[ "$incomplete" -eq 0 ] && finish 0 done "complete pass"
finish 0 partial "partial pass"
