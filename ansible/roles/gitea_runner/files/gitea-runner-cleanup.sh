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
# ONE OPERATION IS DELIBERATELY NOT SAFE-BY-CONSTRUCTION: the build-cache SIZE cap in section 3b runs
# `builder prune -af`, which has NO retention window and removes every unused cache record. Nothing
# about the window argument above covers it. Its safety rests entirely on (a) never running from the
# mid-job critical path, (b) a fresh busy re-read immediately before each destructive call, and (c) a
# disk gate — a narrower guarantee than the rest of this script, spelled out at section 3b.
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
# EVERY default below must equal the role default that renders the env file (defaults/main.yml ->
# templates/gitea-runner-cleanup.env.j2). They are not decoration: the test suite runs this script with
# GITEA_CLEANUP_ENV_FILE=/nonexistent, so these ARE the values under test, and where they disagreed
# with the role the deployed values were exercised by nothing at all. ROUTINE_UNTIL (48h vs the shipped
# 24h) and PRESSURE_PCT (80 vs the shipped 75) had both drifted that way. Section [M] of the test suite
# now derives the mapping from the env template and fails on any future divergence.
ROUTINE_UNTIL="${GITEA_CLEANUP_ROUTINE_UNTIL:-24h}"     # steady state: keep images/cache used within 24h
PRESSURE_PCT="${GITEA_CLEANUP_PRESSURE_PCT:-75}"        # disk% >= this -> tighten the window
PRESSURE_UNTIL="${GITEA_CLEANUP_PRESSURE_UNTIL:-6h}"
CRITICAL_PCT="${GITEA_CLEANUP_CRITICAL_PCT:-92}"        # disk% >= this -> hard window + actcache trim
CRITICAL_UNTIL="${GITEA_CLEANUP_CRITICAL_UNTIL:-4h}"    # clamped up to MIN_UNTIL_SEC — see clamp_win()
REAP_AGE_SEC="${GITEA_CLEANUP_REAP_AGE_SEC:-14400}"     # remove RUNNING containers older than this (4h)
# HARD FLOOR for every `--filter until=` window, in seconds. Under the containerd image store `until`
# compares c8dimages.Image.CreatedAt — the LOCAL record time, i.e. when THIS host pulled or tagged the
# image, NOT the image's build date. And in HOST mode an act_runner job creates no container, so
# filterImagesUsedByContainers() protects nothing: the window is the ONLY thing standing between a live
# job and an image it pulled at minute 0 to run at minute 50. Any window shorter than the job timeout
# (gitea_runner_job_timeout, 3h) can therefore delete an image out from under a running job.
# CRITICAL_UNTIL was 1h — below the job timeout — and section 4 applies it on the mid-job path, so the
# old comment there ("still protects a <1h running build") asserted a guarantee the code did not have.
# This is the same invariant REAP_AGE_SEC already documents for containers ("MUST exceed the job
# timeout"); the prune windows simply never had it applied.
MIN_UNTIL_SEC="${GITEA_CLEANUP_MIN_UNTIL_SEC:-14400}"   # 4h > 3h job timeout
INFRA_EXCLUDE_RE="${GITEA_CLEANUP_INFRA_EXCLUDE_RE:-buildkit|buildx}" # never reap the persistent builder
ACTCACHE_DIR="${GITEA_CLEANUP_ACTCACHE_DIR:-/home/runner/act-runner/.cache/actcache}"
ACTCACHE_MAX_MB="${GITEA_CLEANUP_ACTCACHE_MAX_MB:-1536}"
WORKDIR_PARENT="${GITEA_RUNNER_WORKDIR_PARENT:-/home/runner/act-runner/work}"
WS_PRUNE_AGE_H="${GITEA_CLEANUP_WS_PRUNE_AGE_H:-48}" # 0 disables the stale-workspace prune
# Busy+pressure: how long to wait for the between-jobs gap before giving up (see the busy gate below).
# MUST stay below the unit's TimeoutStartSec minus the worst-case prune time.
IDLE_WAIT_SEC="${GITEA_CLEANUP_IDLE_WAIT_SEC:-600}"
# 1s, not 10s. Measured 2026-08-08 over 6h of real CI: ~96% of the gaps between consecutive
# jobs on a runner are <=10s (ci-runner-4: 272 of 283), so a 10s poll missed almost every one
# and every pressure sweep deferred forever while the disk climbed. The reclaim timer in this
# same role learned this already (30s -> 5s, for the same reason).
IDLE_POLL_SEC="${GITEA_CLEANUP_IDLE_POLL_SEC:-1}"
# ...but polling fast alone would make the OTHER failure worse: the busy signal reads false
# BETWEEN a live job's steps, and a 1s poll samples those sub-second blips constantly. So idle
# must be CONFIRMED across this many consecutive reads (~IDLE_CONFIRM_POLLS seconds of
# continuous quiet). An inter-step blip is far shorter than an inter-job gap; requiring
# persistence separates them without needing a signal act_runner does not offer.
IDLE_CONFIRM_POLLS="${GITEA_CLEANUP_IDLE_CONFIRM_POLLS:-3}"
# Hard SIZE cap for the docker build cache (bytes; 0 disables). Enforced with a full `builder prune -af`
# on the idle path only, re-checking the busy signal first and only once disk is at/over PRESSURE_PCT —
# see section 3b for why nothing cheaper bounds a continuously-reused cache, and for the residual race.
CACHE_MAX_BYTES="${GITEA_CLEANUP_CACHE_MAX_BYTES:-20000000000}"
# Disk% at which the full (unwindowed) image prune becomes available, on the idle path only. This
# REPLACES the old IMAGE_MAX_BYTES cap, which could not bound the disk: an absolute byte cap and the
# filesystem are independent quantities, so "images under cap" and "disk at 88%" were simultaneously
# true and the sweep had no lever at all between PRESSURE_PCT and CRITICAL_PCT (see section 3c).
# Sits between the two so the cheap windowed prunes always get first refusal.
# The `--filter until=` windows cannot retire images on their own — the measured age histogram was
# 35 images at 2h, 21 at 7h, 8 at 1h, i.e. almost everything younger than the pressure window, because
# CI pulls and builds faster than any age rule. So an unwindowed prune has to exist somewhere; keeping
# it idle-only and above this threshold is what makes it affordable.
FULL_PRUNE_PCT="${GITEA_CLEANUP_FULL_PRUNE_PCT:-88}"
BEACON="${GITEA_CLEANUP_BEACON:-1}"
TEXTFILE_DIR="${GITEA_CLEANUP_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"

command -v docker >/dev/null 2>&1 || exit 0
log() { logger -t gitea-runner-cleanup -- "$*" 2>/dev/null || true; }
disk_pct() { df --output=pcent "$DISK_PATH" 2>/dev/null | tail -1 | tr -dc '0-9'; }
is_num() { case "${1:-}" in '' | *[!0-9]*) return 1 ;; *) return 0 ;; esac; }

# Clamp a `--filter until=` window UP to MIN_UNTIL_SEC and emit it in seconds (docker accepts a bare
# duration like `14400s`). Fails SAFE in both directions: anything unparseable becomes the floor rather
# than being passed through, so a typo'd override can never widen the deletion set.
clamp_win() {
  local w="${1:-}" n u s floor
  # The FLOOR itself must be validated before it is used as one. An empty or non-numeric override
  # otherwise propagates straight into the filter: `MIN_UNTIL_SEC='' clamp_win garbage` emitted the
  # bare string `s`, docker rejected `--filter until=s`, and every prune call site swallows its own
  # errors (`|| true`) — so a single typo'd override would have silently disabled ALL window prunes
  # while the sweep still reported success. Fall back to the built-in 4h rather than trusting it.
  floor="$MIN_UNTIL_SEC"
  if ! is_num "$floor" || [ "$floor" -le 0 ]; then
    log "MIN_UNTIL_SEC='${MIN_UNTIL_SEC}' is not a positive integer -> using the built-in 14400s floor"
    floor=14400
  fi
  n="${w%[smhd]}"; u="${w#"$n"}"
  if ! is_num "$n"; then printf '%ss' "$floor"; return 0; fi
  case "$u" in
    s) s=$(( n )) ;; m) s=$(( n * 60 )) ;; h) s=$(( n * 3600 )) ;; d) s=$(( n * 86400 )) ;;
    *) printf '%ss' "$floor"; return 0 ;;
  esac
  if [ "$s" -lt "$floor" ]; then
    log "window ${w} is below the ${floor}s job-timeout floor -> clamped"
    s="$floor"
  fi
  printf '%ss' "$s"
}

write_beacon() {
  [ "$BEACON" = 1 ] && [ -d "$TEXTFILE_DIR" ] || return 0
  local f="$TEXTFILE_DIR/gitea_runner_cleanup.prom" tmp
  tmp="$(mktemp "$f.XXXX" 2>/dev/null)" || return 0
  for kv in "$@"; do printf 'gitea_runner_cleanup_%s %s\n' "${kv%% *}" "${kv#* }"; done > "$tmp"
  chmod 0644 "$tmp" 2>/dev/null || true # mktemp is 0600; node_exporter (non-root) must be able to read it
  mv -f "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}

# Busy check across the co-located runners sharing this Docker daemon. The two runners need DIFFERENT
# signals, and using one for both is what starved this timer:
#
#   * act_runner (ours) spawns the job's step processes directly under its MainPID, so "MainPID has
#     children" really does mean "a job is running". (It reads false briefly BETWEEN steps — hence
#     idle_confirmed() below, which requires the quiet to persist.)
#   * The GitHub agent does NOT work that way. Its unit runs a supervisor, `run.sh`, which ALWAYS has
#     a `run-helper.sh` child whether or not a job is running. Applying the children test to it made
#     the peer read BUSY permanently, so the sweep could never see an idle window: measured
#     2026-08-08 on ci-runner-2/4/5 — gitea children 0 (idle), peer children 1 (its helper), and
#     ZERO gap-catches fleet-wide in 90 minutes while disks climbed to 93%. The agent's real per-job
#     process is Runner.Worker, which exists only while a job runs, so match that instead.
#
# This is deliberately narrow: a wrong answer here either starves reclamation (what happened) or
# prunes through a live job (the containerd-GC race). Every uncertain read below therefore returns
# BUSY — see the per-branch notes in any_job_running(), which implement that rather than assert it.
PEER_JOB_PROCESS_RE="${GITEA_CLEANUP_PEER_JOB_PROCESS_RE:-Runner\.Worker}"

# EVERY uncertain answer below returns BUSY. That is not decoration: an unreadable signal misread as
# idle is how a prune reaches a live job, which is the failure this entire gate exists to prevent.
# Costs of the two directions are asymmetric — a false BUSY delays reclamation by one tick (~15min),
# a false IDLE can red a running job and, at worst, corrupt an in-flight pull.
any_job_running() {
  local svc mp state rc
  # OURS: children of MainPID. `systemctl show` failing, or returning something non-numeric, means we
  # do not know what the daemon is doing — so assume it is working.
  if ! mp="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)"; then
    log "cannot read $SERVICE MainPID -> assuming BUSY"; return 0
  fi
  if ! is_num "$mp"; then
    log "unreadable $SERVICE MainPID '$mp' -> assuming BUSY"; return 0
  fi
  # MainPID 0 is a definite answer: the daemon is not running, so it has no job.
  if [ "$mp" -gt 0 ] && [ -n "$(pgrep -P "$mp" 2>/dev/null || true)" ]; then return 0; fi

  for svc in $PEER_SERVICES; do
    # Only a CONFIRMED `inactive` proves the peer has no job. active/activating/deactivating/failed
    # can all coexist with a live worker, and a systemctl/DBus failure tells us nothing at all — so
    # everything except `inactive` falls through to the worker probe rather than being skipped.
    # NB `systemctl is-active` PRINTS the state and EXITS NONZERO for anything but active — 3 for
    # `inactive`. A `cmd || fallback` assignment therefore clobbers the very value we want, making the
    # `inactive` test below unreachable and turning this into a host-global probe on every tick.
    # Capture first, and only fall back when nothing was printed at all (systemctl/DBus truly failed).
    state="$(systemctl is-active "$svc" 2>/dev/null)"; [ -n "$state" ] || state="unreadable"
    [ "$state" = "inactive" ] && continue
    # pgrep: 0 = matched (busy), 1 = definitively no match (idle). ANY other rc is an error — a bad
    # regex override, a /proc read failure — and must NOT be read as "no job", because idle_confirmed()
    # calls this repeatedly and a persistent error would otherwise look like stable quiet.
    pgrep -f "$PEER_JOB_PROCESS_RE" >/dev/null 2>&1; rc=$?
    case "$rc" in
      0) return 0 ;;
      1) : ;;
      *) log "peer job probe failed for $svc (pgrep rc=$rc) -> assuming BUSY"; return 0 ;;
    esac
  done
  return 1
}

# Idle CONFIRMED: not merely "one read said no children", but quiet across IDLE_CONFIRM_POLLS
# consecutive reads. Returns 0 (idle) only if every read is idle. Used for the wait loop's exit and
# for the re-check before the destructive cap prune, so both reject the between-steps false idle.
idle_confirmed() {
  local i=0
  while [ "$i" -lt "$IDLE_CONFIRM_POLLS" ]; do
    any_job_running && return 1
    i=$(( i + 1 ))
    [ "$i" -lt "$IDLE_CONFIRM_POLLS" ] && sleep "$IDLE_POLL_SEC"
  done
  return 0
}

# Prune the build cache of every NON-default buildx builder. `docker builder prune` only reaches the
# default builder; CI's setup-buildx-action creates docker-container-driver builders (buildx_buildkit_*)
# whose cache it never sees and which are EXCLUDED from container reaping (INFRA_EXCLUDE_RE) — so without
# this their cache grows unbounded (the ~41 GB of build cache seen on the wedged ci-runner-2). Window-safe
# (same `--filter until=` retention as the default-builder prune), so it can run under the pressure gate.
prune_extra_builders() {
  local win="$1" b
  docker buildx ls --format '{{.Name}}' 2>/dev/null | grep -vE '^$|^default$' | sort -u | while read -r b; do
    timeout 300 docker buildx prune -f --filter "until=$win" --builder "$b" >/dev/null 2>&1 || true
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
  # pressure_defer 0: this is the HEALTHY early exit — busy, but the disk does not need the space.
  # busy_skip alone cannot distinguish it from the starvation exit below, and 88% of busy_skip writes
  # are THIS one (952 of 1087 over 7d/5 runners), which is why alerting on busy_skip paged four
  # healthy runners. See the pressure_defer note at the deferral branch.
  write_beacon "busy_skip 1" "pressure_defer 0" "midjob_prune 0" "last_run_seconds $(date +%s)" \
    "disk_used_percent ${before}"
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
    # idle_confirmed() consumes IDLE_CONFIRM_POLLS-1 extra sleeps when it starts idle; count them so
    # a long confirm cannot overrun IDLE_WAIT_SEC (and thus the unit's TimeoutStartSec).
    if idle_confirmed; then busy=0; break; fi
    waited=$(( waited + (IDLE_CONFIRM_POLLS - 1) * IDLE_POLL_SEC ))
  done
  if [ "$busy" -eq 0 ]; then
    log "pressure sweep waited ${waited}s for the job gap -> sweeping race-free (disk ${before}%)"
  else
    pct_now="$(disk_pct)"; is_num "$pct_now" || pct_now="$before"
    if [ "$pct_now" -ge "$CRITICAL_PCT" ]; then
      midjob=1
      log "CRITICAL disk ${pct_now}% and no job gap in ${IDLE_WAIT_SEC}s -> pruning THROUGH a live job (ENOSPC would red every job)"
    else
      # THIS `exit 0` SKIPS THE WHOLE SWEEP, including the docker-free stale-workspace prune (section
      # 5) and the age-gated container reap (section 2), both of which are safe under load. That is a
      # deliberate keep, not an oversight — a codex cross-review raised it, and it was measured before
      # being closed (7d journal + `du`, all five runners, 2026-08-12):
      #   * this branch fires ~19x/day fleet-wide (42/15/16/51/11 over 7d on ci-runner-1..5), so it is
      #     real, not theoretical;
      #   * but STALE WORKSPACES ARE ZERO on every runner (work/ 2.6-4.0 GB across 27-33 dirs, none
      #     untouched for 48h) and `reaped` has been 0 on every sweep, so the reclaim this branch
      #     withholds would free exactly 0 bytes. The ~74 idle sweeps/day already keep both current —
      #     the workspace count is stable at ~30/VM, down from the 350-400 that caused the 2026-07-28
      #     fleet outage.
      # Everything else it withholds is a docker prune, which is precisely what must not run here: any
      # prune triggers the containerd GC pass that reaps an in-flight pull's lease (52.6% vs 19.0% job
      # failure, measured above). So widening this path buys nothing measurable and spends it in the
      # one place where a wrong answer reds live CI jobs. Revisit only if stale workspaces reappear.
      log "defer: disk ${pct_now}% >= ${PRESSURE_PCT}% but < ${CRITICAL_PCT}% and busy for ${IDLE_WAIT_SEC}s -> skip; next tick retries"
      # pressure_defer 1: THE starvation signal, and the only beacon field that means it. This branch
      # is the one worth paging on — the disk needs the space and the sweep could not get a gap.
      # It exists because the alert cannot be reconstructed from the other fields after the fact:
      # busy_skip is 1 on the healthy exit too, and inferring the difference from a disk THRESHOLD in
      # PromQL failed twice. Instantaneously, the `for` clock reset on every dip (these disks swing up
      # to 23 points an hour once the caches fire), so the rule could not sustain. Averaged over 3h, a
      # trailing mean under `for: 3h` stacks two windows and lags 5.5-6h behind a real jump, and never
      # fires at all for a slow climb. Both are statistics standing in for a branch the script already
      # knows it took — so it records the branch instead, and the rule needs no threshold at all.
      # NB the alert on this metric is silent until this script is DEPLOYED (the series does not exist
      # before then); CIRunnerCleanupStale still covers a beacon that stops updating entirely.
      write_beacon "busy_skip 1" "pressure_defer 1" "midjob_prune 0" "last_run_seconds $(date +%s)" \
        "disk_used_percent ${pct_now}"
      exit 0
    fi
  fi
fi

# 1. stopped containers — always safe (never touches running).
timeout 120 docker container prune -f >/dev/null 2>&1 || true

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
timeout 120 docker container prune -f >/dev/null 2>&1 || true

# ORDER NOTE: the two SIZE caps below run BEFORE section 3's window prunes, and that ordering is
# load-bearing rather than cosmetic. They are the only steps that can reclaim a continuously-churned
# cache or image store, they are the only ones gated on a CONFIRMED idle window, and they were
# previously last — so on a busy runner the window prunes burned the idle window first and both caps
# then logged "over cap but a job is running" every single sweep. Measured 2026-08-09 on
# ci-runner-2: cache 28 GB and images 36.6 GB, both over cap, both skipped, disk parked at 88-92%.
# Running them first spends the freshly-confirmed idle window on the work that actually reclaims.
# 3b. SIZE cap for the build cache — the only thing that actually bounds it. The `until=` windows above
#     cannot: the cache is actively REUSED, so its last-used stamps keep refreshing. Measured 2026-08-08
#     on ci-runner-3 with a 26 GB cache: `builder prune --filter until=1h` reclaimed 1.7 GB, and the CLI
#     size-target flags (`--reserved-space` / `--max-used-space`) reclaimed 0 B. Bounding it at the daemon
#     does not work either on Docker 29.x (builder.gc top-level knobs are silently inert; a multi-value
#     policy filter kills dockerd at startup — see github_runner/defaults/main.yml).
#     So: an ALL-unused prune, which is the lever that does move it (same host, same day: 18.12 GB
#     reclaimed, disk 74% -> 62%, cache -> 0 B). It is destructive to warm cache — the next build has
#     nothing to reuse — so it is deliberately rare: only when the cache is over CACHE_MAX_BYTES, and
#     never from the mid-job critical path (a full prune through a live job is the GC race we removed).
#     CONCURRENCY, stated honestly: `midjob` records the DECISION taken at the busy gate, not the
#     daemon's state right now — the window prunes above can take minutes, and a job may well have
#     started meanwhile. So the busy signal is RE-READ immediately before the destructive prune. That
#     narrows the exposure to the moments between that read and exec; it does NOT close it, because
#     act_runner offers no admission lock and the pgrep signal reads false between a job's steps.
#     Closing it properly needs a lock held for a whole job's lifetime by BOTH co-located runners
#     (gitea + the GitHub agent share this daemon) — deliberately not attempted here.
#     Gating on disk too means that residual risk is only taken when the disk actually needs the
#     space, rather than on every cache overshoot on a healthy runner.
pct_cap="$(disk_pct)"; is_num "$pct_cap" || pct_cap=0
if [ "$midjob" -eq 0 ] && [ "${CACHE_MAX_BYTES:-0}" -gt 0 ] && [ "$pct_cap" -ge "$PRESSURE_PCT" ]; then
  bc_now="$(timeout 60 docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null \
    | awk -F'|' '$1=="Build Cache"{sub(/B$/,"",$2);print $2}' | numfmt --from=si 2>/dev/null)"
  if is_num "$bc_now" && [ "$bc_now" -gt "$CACHE_MAX_BYTES" ]; then
    if ! idle_confirmed; then
      log "build cache ${bc_now}B over cap but a job is running -> skipping full prune"
    else
      log "build cache ${bc_now}B > cap ${CACHE_MAX_BYTES}B at disk ${pct_cap}%, idle -> full builder prune"
      timeout 900 docker builder prune -af >/dev/null 2>&1 || true
      # Collect first, then iterate: a `grep` matching nothing exits 1, which would poison a piped
      # `while` if this script ever gained `set -e`. (`set -o pipefail` is already on at the top and
      # is inert here without `set -e` — the hazard is future-proofing, not current behaviour.)
      # `|| true` keeps a missing or hung buildx from failing the sweep.
      extra_builders="$(timeout 30 docker buildx ls --format '{{.Name}}' 2>/dev/null \
        | grep -vE '^$|^default$' | sort -u || true)"
      # The default-builder prune above can take MINUTES, so the read that authorised it is stale by
      # now. Re-read before the per-builder prunes rather than letting one check cover N+1 destructive
      # calls. (These hit each builder's own buildkitd rather than the host containerd store, so the
      # blast radius differs — but it is the same class of race, and a second read costs nothing.)
      if [ -n "$extra_builders" ] && ! idle_confirmed; then
        log "job arrived during the default-builder prune -> skipping the per-builder prunes"
      else
        for xb in $extra_builders; do
          timeout 300 docker buildx prune -af --builder "$xb" >/dev/null 2>&1 || true
        done
      fi
    fi
  fi
fi

# 3c. DISK-GATED full image prune — the last-resort lever for the accumulator that overtook the cache
#     (2026-08-09, ci-runner-4: 35 GB images vs 28.6 GB cache). The retention window cannot retire
#     images: most are younger than the pressure window because CI pulls and builds faster than any age
#     rule. Same safety shape as 3b, deliberately: idle path only, NEVER from the mid-job critical path,
#     and the busy signal re-read immediately before the destructive call.
#     `image prune -af` removes every image no container is using, so the next job re-pulls — which is
#     why it fires only above FULL_PRUNE_PCT rather than routinely. Note that under HOST mode no job
#     ever holds a container, so "no container is using it" is true of EVERY image here; the idle gate,
#     not a container reference, is what keeps this from landing on a live job.
#
#     GATED ON THE FILESYSTEM, NOT ON A BYTE CAP. An absolute cap does not compose with the disk: with
#     cache and image caps both at 20 GB, "every cap satisfied" and "disk at 88%" are simultaneously
#     true, and then this sweep completes having done nothing while the disk stays full. Measured on
#     ci-runner-4 2026-08-11 — 04:22 the cap fired correctly (images 30.6 GB > 20 GB) and took the disk
#     75% -> 63%; by 10:26 images were back under the cap, so the sweep logged `done: disk 88% -> 88%`
#     with no lever left to pull. Between "caps satisfied" and CRITICAL_PCT the script had nothing at
#     all, which is precisely the band the disks then oscillated in for weeks.
#     disk_pct() is a df(1) read and cannot fail the way `docker system df` can (slow metadata walk of
#     the whole snapshot tree on the containerd store, and an unreadable result fails CLOSED with no
#     log — indistinguishable from "nothing to do"). The size read is kept for the LOG LINE only, so a
#     failure to read it can no longer suppress the prune.
if [ "$midjob" -eq 0 ] && [ "$pct_cap" -ge "$FULL_PRUNE_PCT" ]; then
  img_now="$(timeout 60 docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null | awk -F'|' '$1=="Images"{sub(/B$/,"",$2);print $2}' | numfmt --from=si 2>/dev/null)"
  if is_num "$img_now"; then img_desc="images ${img_now}B"; else
    img_desc="images unreadable"
    log "docker system df unreadable -> pruning on disk% anyway (it no longer gates this)"
  fi
  if ! idle_confirmed; then
    log "disk ${pct_cap}% >= ${FULL_PRUNE_PCT}% (${img_desc}) but a job is running -> skipping the full image prune"
  else
    log "disk ${pct_cap}% >= ${FULL_PRUNE_PCT}% (${img_desc}), idle -> full image prune"
    timeout 900 docker image prune -af >/dev/null 2>&1 || true
    after_fp="$(disk_pct)"; is_num "$after_fp" || after_fp="$pct_cap"
    log "full image prune done: disk ${pct_cap}% -> ${after_fp}%"
    if [ $(( pct_cap - after_fp )) -lt 2 ]; then
      log "WARNING: full image prune freed <2% — bytes are in containerd state docker cannot reach (moby_history leases, orphaned snapshots)"
    fi
  fi
fi

# 3. window-safe prunes: images / build cache / orphaned networks OLDER than the retention window. Never
#    removes anything in use or recently used, so it is safe regardless of the idle read. Tighten under
#    disk pressure. (Build cache is the big one — 10-33 GB/runner.)
pct="$(disk_pct)"; is_num "$pct" || pct=0
win="$(clamp_win "$ROUTINE_UNTIL")"
[ "$pct" -ge "$PRESSURE_PCT" ] && win="$(clamp_win "$PRESSURE_UNTIL")"
# `timeout` on each: a wedged daemon otherwise blocks the unit to TimeoutStartSec (the sweep would
# hang here rather than at the size cap, which is where the timeouts used to start).
timeout 120 docker network prune -f --filter "until=$win" >/dev/null 2>&1 || true
timeout 600 docker builder prune -f --filter "until=$win" >/dev/null 2>&1 || true
prune_extra_builders "$win"
timeout 600 docker image prune -af --filter "until=$win" >/dev/null 2>&1 || true

# 4. critical: still very high -> tightest ALLOWED window + trim the actions cache (act_runner re-fills
#    it). Path-guarded so we can never rm the wrong tree. The window is clamped to MIN_UNTIL_SEC like
#    every other one: this block runs on the MID-JOB path too, so it is the single most dangerous place
#    to go below the job timeout, not a place to make an exception.
pct="$(disk_pct)"; is_num "$pct" || pct=0
if [ "$pct" -ge "$CRITICAL_PCT" ]; then
  cwin="$(clamp_win "$CRITICAL_UNTIL")"
  log "critical disk ${pct}% -> ${cwin} window + actcache trim"
  timeout 600 docker builder prune -f --filter "until=$cwin" >/dev/null 2>&1 || true
  prune_extra_builders "$cwin"
  timeout 600 docker image prune -af --filter "until=$cwin" >/dev/null 2>&1 || true
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
# pressure_defer 0: reaching here means the sweep RAN. That includes the mid-job critical path, which
# is recorded separately by midjob_prune — starved means "could not sweep", not "swept riskily".
write_beacon "busy_skip 0" "pressure_defer 0" "midjob_prune ${midjob}" "last_run_seconds $(date +%s)" "disk_used_percent ${after}" \
  "disk_freed_percent ${freed}" "reaped_containers ${reaped}" \
  "workspaces ${ws_count}" "workspaces_pruned ${ws_pruned}" \
  "build_cache_bytes ${bc_bytes}" "images_bytes ${img_bytes}"
exit 0
