#!/usr/bin/env bash
# MANAGED BY ANSIBLE (role: gitea_runner). Timer-driven docker + storage reclamation for the CI runner
# VMs. COMPLEMENTS gitea-runner-reclaim.sh (which reclaims artifact SUBdirs in the reused workspace);
# THIS reclaims the heavy disk accumulators a full CI run leaves on the SHARED Docker daemon — build
# cache (unbounded, 10-33 GB/runner observed), stale image layers, stopped containers, orphaned compose
# networks, and (only under real pressure) the actions cache. A full root disk breaks CI (image pulls +
# artifact writes fail — one runner hit 100%). Disk-pressure escalation keeps it bounded.
#
# SAFETY MODEL (hardened after an adversarial review) -----------------------------------------------
# The Docker daemon is SHARED by the Gitea act_runner AND the co-located GitHub runner, and act_runner's
# "daemon has child processes" idle signal is UNRELIABLE (it reads false between a live job's steps, and
# detached compose stacks / docker-executor jobs have no host child at all). So we do NOT trust idle to
# decide destructive actions. Instead every operation is SAFE-BY-CONSTRUCTION — it can never remove a
# resource a live job (Gitea OR GitHub) is using OR recently created:
#   * containers: only `container prune` (stopped) + reap RUNNING ones OLDER than a job's max wall-clock
#     (REAP_AGE, default 4h > the 3h job timeout), EXCLUDING the persistent buildx/buildkit builder. A
#     live job's containers are younger than its timeout, so they are never touched.
#   * images / build-cache / networks: pruned only with a retention WINDOW (`--filter until=`), so
#     in-use and recently-used entries are always kept (a fresh build reuses recent cache/images).
#   * NO `docker volume prune` (a job's data volumes are irreplaceable — reclaimed only via image/cache).
# The idle check below is therefore only a cheap "skip the work while obviously busy" optimization, not
# the safety mechanism. Best-effort; ALWAYS exits 0.
set -uo pipefail

ENV_FILE="${GITEA_CLEANUP_ENV_FILE:-/etc/gitea-runner-cleanup.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

SERVICE="${GITEA_RUNNER_SERVICE:-gitea-act-runner.service}"
# Co-located runner services to ALSO treat as "busy" (shared Docker daemon). Space-separated.
PEER_SERVICES="${GITEA_CLEANUP_PEER_SERVICES:-actions.runner.cchifor-platform.service}"
DISK_PATH="${GITEA_CLEANUP_DISK_PATH:-/}"
ROUTINE_UNTIL="${GITEA_CLEANUP_ROUTINE_UNTIL:-48h}"     # steady state: keep images/cache used within 48h
PRESSURE_PCT="${GITEA_CLEANUP_PRESSURE_PCT:-80}"        # disk% >= this -> tighten the window
PRESSURE_UNTIL="${GITEA_CLEANUP_PRESSURE_UNTIL:-6h}"
CRITICAL_PCT="${GITEA_CLEANUP_CRITICAL_PCT:-92}"        # disk% >= this -> hard 1h window + actcache trim
CRITICAL_UNTIL="${GITEA_CLEANUP_CRITICAL_UNTIL:-1h}"    # never 0/`-a`: still protects a <1h running build
REAP_AGE_SEC="${GITEA_CLEANUP_REAP_AGE_SEC:-14400}"     # remove RUNNING containers older than this (4h)
INFRA_EXCLUDE_RE="${GITEA_CLEANUP_INFRA_EXCLUDE_RE:-buildkit|buildx}" # never reap the persistent builder
ACTCACHE_DIR="${GITEA_CLEANUP_ACTCACHE_DIR:-/home/runner/act-runner/.cache/actcache}"
ACTCACHE_MAX_MB="${GITEA_CLEANUP_ACTCACHE_MAX_MB:-1536}"
BEACON="${GITEA_CLEANUP_BEACON:-1}"
TEXTFILE_DIR="${GITEA_CLEANUP_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"

command -v docker >/dev/null 2>&1 || exit 0
log() { logger -t gitea-runner-cleanup -- "$*" 2>/dev/null || true; }
disk_pct() { df --output=pcent "$DISK_PATH" 2>/dev/null | tail -1 | tr -dc '0-9'; }
is_num() { case "${1:-}" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

write_beacon() {
  [ "$BEACON" = 1 ] && [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/gitea_runner_cleanup.prom" tmp
  tmp="$(mktemp "$f.XXXX" 2>/dev/null)" || return 0
  for kv in "$@"; do printf 'gitea_runner_cleanup_%s %s\n' "${kv%% *}" "${kv#* }"; done > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true # mktemp is 0600; node_exporter (non-root) must be able to read it
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}

# Best-effort "obviously busy" check across ALL co-located runners (shared daemon). NOT the safety
# mechanism (see header) — just avoids doing work mid-job. A runner is busy iff its daemon has children.
any_job_running() {
  local svc mp
  for svc in "$SERVICE" $PEER_SERVICES; do
    mp="$(systemctl show -p MainPID --value "$svc" 2>/dev/null || echo 0)"
    if is_num "$mp" && [ "$mp" -gt 0 ] && [ -n "$(pgrep -P "$mp" 2>/dev/null || true)" ]; then return 0; fi
  done
  return 1
}

# Prune the build cache of every NON-default buildx builder. `docker builder prune` only reaches the
# default builder; CI's setup-buildx-action creates docker-container-driver builders (buildx_buildkit_*)
# whose cache it never sees and which are EXCLUDED from container reaping (INFRA_EXCLUDE_RE) — so without
# this their cache grows unbounded (the ~41 GB of build cache seen on the wedged ci-runner-2). Window-safe
# (same `--filter until=` retention as the default-builder prune), so it can run under the pressure gate.
prune_extra_builders() {
  local win="$1" b
  docker buildx ls --format '{{.Name}}' 2>/dev/null | grep -vE '^$|^default$' | sort -u | while read -r b; do
    docker buildx prune -f --filter "until=$win" --builder "$b" >/dev/null 2>&1 || true
  done
}

before="$(disk_pct)"; is_num "$before" || before=0
busy=0; any_job_running && busy=1

# Pressure-aware busy gate (fix for the ENOSPC starvation death spiral). Every prune below is
# safe-by-construction: it uses a retention window (`--filter until=`) LONGER than a job's max wall-clock
# (the pressure window 6h > the 3h job timeout, mirroring REAP_AGE 4h > 3h), so it can never remove a
# resource a live/recent build is using. The busy check is therefore ONLY an optimisation that suppresses
# the ROUTINE (non-pressure) sweep — it must NEVER suppress the pressure/critical reclaim. The old code
# exited unconditionally when busy, so on a runner kept continuously busy by back-to-back jobs the
# pressure reclaim NEVER ran and the disk climbed to 100% (ci-runner-2). See the cchifor/platform CI
# ENOSPC incident.
if [ "$busy" -eq 1 ] && [ "$before" -lt "$PRESSURE_PCT" ]; then
  log "skip routine sweep: co-located runner busy and disk ${before}% < ${PRESSURE_PCT}% pressure"
  write_beacon "busy_skip 1" "last_run_seconds $(date +%s)" "disk_used_percent ${before}"
  exit 0
fi
[ "$busy" -eq 1 ] && log "pressure override: disk ${before}% >= ${PRESSURE_PCT}% -> window-safe reclaim despite busy runner"

# 1. stopped containers — always safe (never touches running).
docker container prune -f >/dev/null 2>&1 || true

# 2. reap LEAKED running containers: older than REAP_AGE (a live job's are younger than its timeout) AND
#    not the persistent buildx/buildkit builder. This is the ONLY op that removes a running container,
#    and by the age gate it can never hit a live Gitea/GitHub job or the shared build infra.
now="$(date +%s)"; reaped=0
for cid in $(docker ps -q 2>/dev/null || true); do
  meta="$(docker inspect -f '{{.Config.Image}} {{.Name}}' "$cid" 2>/dev/null || true)"
  [ -z "$meta" ] && continue
  printf '%s' "$meta" | grep -qiE "$INFRA_EXCLUDE_RE" && continue
  started="$(docker inspect -f '{{.State.StartedAt}}' "$cid" 2>/dev/null || true)"
  st="$(date -d "$started" +%s 2>/dev/null || true)"
  is_num "$st" || continue
  if [ $(( now - st )) -gt "$REAP_AGE_SEC" ]; then
    log "reap stale running container $cid ($meta), age $(( (now-st)/3600 ))h"
    docker rm -f "$cid" >/dev/null 2>&1 && reaped=$(( reaped + 1 ))
  fi
done
docker container prune -f >/dev/null 2>&1 || true

# 3. window-safe prunes: images / build cache / orphaned networks OLDER than the retention window. Never
#    removes anything in use or recently used, so it is safe regardless of the idle read. Tighten under
#    disk pressure. (Build cache is the big one — 10-33 GB/runner.)
pct="$(disk_pct)"; is_num "$pct" || pct=0
win="$ROUTINE_UNTIL"; [ "$pct" -ge "$PRESSURE_PCT" ] && win="$PRESSURE_UNTIL"
docker network prune -f --filter "until=$win" >/dev/null 2>&1 || true
docker builder prune -f --filter "until=$win" >/dev/null 2>&1 || true
prune_extra_builders "$win"
docker image prune -af --filter "until=$win" >/dev/null 2>&1 || true

# 4. critical: still very high -> hard 1h window (NOT `-af`: a <1h running build is still protected) +
#    trim the actions cache (act_runner re-fills it). Path-guarded so we can never rm the wrong tree.
pct="$(disk_pct)"; is_num "$pct" || pct=0
if [ "$pct" -ge "$CRITICAL_PCT" ]; then
  log "critical disk ${pct}% -> ${CRITICAL_UNTIL} window + actcache trim"
  docker builder prune -f --filter "until=$CRITICAL_UNTIL" >/dev/null 2>&1 || true
  prune_extra_builders "$CRITICAL_UNTIL"
  docker image prune -af --filter "until=$CRITICAL_UNTIL" >/dev/null 2>&1 || true
  case "$ACTCACHE_DIR" in
    /*/*) # require an absolute path with >=2 components so we can never rm '/' or a top-level dir
      if [ -d "$ACTCACHE_DIR" ]; then
        sz="$(du -sm "$ACTCACHE_DIR" 2>/dev/null | cut -f1)"
        if is_num "$sz" && [ "$sz" -gt "$ACTCACHE_MAX_MB" ]; then
          log "actcache ${sz}MB > ${ACTCACHE_MAX_MB}MB -> clearing"
          find "$ACTCACHE_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
        fi
      fi ;;
    *) log "abort actcache trim: implausible ACTCACHE_DIR='$ACTCACHE_DIR'" ;;
  esac
fi

after="$(disk_pct)"; is_num "$after" || after=0
freed=$(( before - after )); [ "$freed" -lt 0 ] && freed=0
# Post-cleanup docker resource sizes for the beacon (build cache is the dominant accumulator). Docker's
# HumanSize is base-1000, so numfmt --from=si. Best-effort; fallback 0 so a parse failure never fabricates.
dfsize() { docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null | awk -F'|' -v t="$1" '$1==t{sub(/B$/,"",$2);print $2}' | numfmt --from=si 2>/dev/null; }
bc_bytes="$(dfsize 'Build Cache')"; is_num "$bc_bytes" || bc_bytes=0
img_bytes="$(dfsize 'Images')"; is_num "$img_bytes" || img_bytes=0
log "done: disk ${before}% -> ${after}%, reaped ${reaped} stale container(s), build-cache ${bc_bytes}B"
write_beacon "busy_skip 0" "last_run_seconds $(date +%s)" "disk_used_percent ${after}" \
  "disk_freed_percent ${freed}" "reaped_containers ${reaped}" \
  "build_cache_bytes ${bc_bytes}" "images_bytes ${img_bytes}"
exit 0
