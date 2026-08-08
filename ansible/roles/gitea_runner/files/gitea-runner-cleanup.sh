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
#     NB the window bounds WHAT IS DELETED — it does NOT make a prune safe to run alongside a job.
#     The prune's containerd GC pass can reap an in-flight pull's lease regardless of the filter, so
#     concurrency is governed by the busy gate below, never by the window. (2026-08-08 RCA.)
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
WORKDIR_PARENT="${GITEA_RUNNER_WORKDIR_PARENT:-/home/runner/act-runner/work}"
WS_PRUNE_AGE_H="${GITEA_CLEANUP_WS_PRUNE_AGE_H:-48}" # 0 disables the stale-workspace prune
# Busy+pressure: how long to wait for the between-jobs gap before giving up (see the busy gate below).
# MUST stay below the unit's TimeoutStartSec minus the worst-case prune time.
IDLE_WAIT_SEC="${GITEA_CLEANUP_IDLE_WAIT_SEC:-600}"
IDLE_POLL_SEC="${GITEA_CLEANUP_IDLE_POLL_SEC:-10}"
# Hard SIZE cap for the docker build cache (bytes; 0 disables). Enforced ONLY on an idle runner, with a
# full `builder prune -af` — see section 3b for why nothing cheaper bounds a continuously-reused cache.
CACHE_MAX_BYTES="${GITEA_CLEANUP_CACHE_MAX_BYTES:-20000000000}"
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
busy=0; any_job_running && busy=1 || true # `|| true`: keep the compound exit 0 (robust if `set -e` is ever added)

# ---- busy gate: two failure modes to avoid AT ONCE --------------------------------------------
# (a) ENOSPC starvation. A runner kept busy by back-to-back jobs never gets swept and climbs to 100%,
#     after which EVERY job dies at actions/checkout (ci-runner-2; the platform ENOSPC incident). That
#     is why the original unconditional busy-exit was replaced by an override that pruned anyway once
#     disk >= PRESSURE_PCT.
# (b) Mid-job prune damage — what that override then caused. ANY docker prune triggers a containerd
#     content-store GC pass, and that pass reaps the leases protecting an IN-FLIGHT `docker pull`. The
#     job dies with `lease does not exist: not found`, a manifest 404, or a daemon API timeout,
#     depending on where it was when the GC landed (which is why the symptom appeared to move between
#     subsystems). Crucially this happens even when the prune frees NOTHING, so the `--filter until=`
#     retention window does NOT prevent it: the window governs what is SELECTED for deletion, not the
#     GC pass itself. Measured 2026-08-08: 472 mid-job prunes in 7d across the fleet, and a job with a
#     prune inside its window failed ~3x more often than one without (5-15min jobs: 52.6% vs 19.0%) —
#     roughly 10 avoidable red CI jobs a day.
# Resolution: disk pressure no longer buys the right to prune THROUGH a job. It buys the right to WAIT
# for the gap between jobs (capacity=1, so gaps are frequent — ~74 idle sweeps/day observed per runner)
# and sweep there, race-free. Only a genuinely CRITICAL disk with no gap available still prunes through
# a live job: one red job beats ENOSPC reding every job. The disk should rarely get there at all now
# that the build cache is capped at the daemon (builder.gc maxUsedSpace, github_runner/daemon.json.j2)
# instead of being chased by this timer.
if [ "$busy" -eq 1 ] && [ "$before" -lt "$PRESSURE_PCT" ]; then
  log "skip routine sweep: co-located runner busy and disk ${before}% < ${PRESSURE_PCT}% pressure"
  write_beacon "busy_skip 1" "midjob_prune 0" "last_run_seconds $(date +%s)" "disk_used_percent ${before}"
  exit 0
fi

midjob=0
if [ "$busy" -eq 1 ]; then
  # Under pressure AND busy: wait for the between-jobs gap instead of racing the daemon. The unit's
  # TimeoutStartSec must exceed IDLE_WAIT_SEC + the prune itself; the timer is OnUnitInactiveSec (from
  # FINISH), so waiting here just spaces the next tick — it can never overlap this run.
  waited=0
  while [ "$waited" -lt "$IDLE_WAIT_SEC" ]; do
    sleep "$IDLE_POLL_SEC"
    waited=$(( waited + IDLE_POLL_SEC ))
    if ! any_job_running; then busy=0; break; fi
  done
  if [ "$busy" -eq 0 ]; then
    log "pressure sweep waited ${waited}s for the job gap -> sweeping race-free (disk ${before}%)"
  else
    pct_now="$(disk_pct)"; is_num "$pct_now" || pct_now="$before"
    if [ "$pct_now" -ge "$CRITICAL_PCT" ]; then
      midjob=1
      log "CRITICAL disk ${pct_now}% and no job gap in ${IDLE_WAIT_SEC}s -> pruning THROUGH a live job (ENOSPC would red every job)"
    else
      log "defer: disk ${pct_now}% >= ${PRESSURE_PCT}% but < ${CRITICAL_PCT}% and busy for ${IDLE_WAIT_SEC}s -> skip; next tick retries"
      write_beacon "busy_skip 1" "midjob_prune 0" "last_run_seconds $(date +%s)" "disk_used_percent ${pct_now}"
      exit 0
    fi
  fi
fi

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

# 3b. SIZE cap for the build cache — the only thing that actually bounds it. The `until=` windows above
#     cannot: the cache is actively REUSED, so its last-used stamps keep refreshing. Measured 2026-08-08
#     on ci-runner-3 with a 26 GB cache: `builder prune --filter until=1h` reclaimed 1.7 GB, and the CLI
#     size-target flags (`--reserved-space` / `--max-used-space`) reclaimed 0 B. Bounding it at the daemon
#     does not work either on Docker 29.x (builder.gc top-level knobs are silently inert; a multi-value
#     policy filter kills dockerd at startup — see github_runner/defaults/main.yml).
#     So: an ALL-unused prune, which is the lever that does move it (same host, same day: 18.12 GB
#     reclaimed, disk 74% -> 62%, cache -> 0 B). It is destructive to warm cache — the next build has
#     nothing to reuse — so it is deliberately rare: only when the cache is over CACHE_MAX_BYTES, and
#     NEVER on the mid-job path (a full prune through a live job is exactly the GC race we removed).
if [ "$midjob" -eq 0 ] && [ "${CACHE_MAX_BYTES:-0}" -gt 0 ]; then
  bc_now="$(docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null \
    | awk -F'|' '$1=="Build Cache"{sub(/B$/,"",$2);print $2}' | numfmt --from=si 2>/dev/null)"
  if is_num "$bc_now" && [ "$bc_now" -gt "$CACHE_MAX_BYTES" ]; then
    log "build cache ${bc_now}B > cap ${CACHE_MAX_BYTES}B on an idle runner -> full builder prune"
    docker builder prune -af >/dev/null 2>&1 || true
    docker buildx ls --format '{{.Name}}' 2>/dev/null | grep -vE '^$|^default$' | sort -u \
      | while read -r b; do docker buildx prune -af --builder "$b" >/dev/null 2>&1 || true; done
  fi
fi

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

# 5. stale act_runner workspaces. act_runner keys work/<repo-hash>/ by REPO and never deletes it;
#    agentforge's ephemeral per-repo CI mints a fresh repo (=> a fresh workspace dir) continuously, so
#    the parent grew to ~350-400 dirs/VM by 2026-07-28 — which is exactly what made the reclaim
#    script's per-workspace /proc scan blow the daemon's start-pre budget and take the whole fleet
#    offline (see gitea-runner-reclaim.sh + tests/test-reclaim.sh). Prune any workspace whose ENTIRE
#    tree is older than the age gate. SAFE-BY-CONSTRUCTION like every op above: the gate (48h default)
#    >> the 3h job timeout and a job freshens its checkout at start, so a live/recent workspace always
#    trips the -newermt probe (which short-circuits on the first fresh entry). Path-guarded like the
#    actcache trim. Reached only from an idle sweep, or from the critical last-resort path above.
ws_pruned=0; ws_count=0
if is_num "$WS_PRUNE_AGE_H" && [ "$WS_PRUNE_AGE_H" -gt 0 ] && [ -d "$WORKDIR_PARENT" ]; then
  case "$WORKDIR_PARENT" in
    /*/*)
      cutoff=$(( $(date +%s) - WS_PRUNE_AGE_H * 3600 ))
      for ws in "$WORKDIR_PARENT"/*/; do
        ws="${ws%/}"
        [ -d "$ws" ] || continue
        ws_count=$(( ws_count + 1 ))
        if [ -z "$(find "$ws" -newermt "@$cutoff" -print -quit 2>/dev/null)" ]; then
          log "prune stale workspace $ws (no activity in ${WS_PRUNE_AGE_H}h)"
          rm -rf -- "$ws" 2>/dev/null && { ws_pruned=$(( ws_pruned + 1 )); ws_count=$(( ws_count - 1 )); }
        fi
      done ;;
    *) log "abort workspace prune: implausible WORKDIR_PARENT='$WORKDIR_PARENT'" ;;
  esac
fi

after="$(disk_pct)"; is_num "$after" || after=0
freed=$(( before - after )); [ "$freed" -lt 0 ] && freed=0
# Post-cleanup docker resource sizes for the beacon (build cache is the dominant accumulator). Docker's
# HumanSize is base-1000, so numfmt --from=si. Best-effort; fallback 0 so a parse failure never fabricates.
dfsize() { docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null | awk -F'|' -v t="$1" '$1==t{sub(/B$/,"",$2);print $2}' | numfmt --from=si 2>/dev/null; }
bc_bytes="$(dfsize 'Build Cache')"; is_num "$bc_bytes" || bc_bytes=0
img_bytes="$(dfsize 'Images')"; is_num "$img_bytes" || img_bytes=0
log "done: disk ${before}% -> ${after}%, reaped ${reaped} stale container(s), pruned ${ws_pruned} workspace(s) (${ws_count} left), build-cache ${bc_bytes}B"
write_beacon "busy_skip 0" "midjob_prune ${midjob}" "last_run_seconds $(date +%s)" "disk_used_percent ${after}" \
  "disk_freed_percent ${freed}" "reaped_containers ${reaped}" \
  "workspaces ${ws_count}" "workspaces_pruned ${ws_pruned}" \
  "build_cache_bytes ${bc_bytes}" "images_bytes ${img_bytes}"
exit 0
