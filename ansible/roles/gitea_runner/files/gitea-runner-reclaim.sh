#!/usr/bin/env bash
# MANAGED BY ANSIBLE (role: gitea_runner). Between-jobs / on-restart workspace reclaim for the
# PERSISTENT act_runner daemon (HOST mode).
#
# WHY THIS EXISTS -------------------------------------------------------------------------------
# Unlike the GitHub ephemeral runner, act_runner (0.6.x) has NO per-job hook (no
# ACTIONS_RUNNER_HOOK_JOB_STARTED) and NO "fresh/clean workspace" option: in HOST mode it REUSES the
# per-repo workspace  work/<repo-hash>/hostexecutor  across every job. Job containers (Playwright,
# the Hatchet bootstrap, coverage) write artifact dirs (.hatchet-config, coverage-data,
# tests/e2e/*) into that bind-mounted workspace as uid 0. The root-owned leftovers accumulate until
# a NEW job cannot even enter its working directory and fails its FIRST step with the MISLEADING
# error:  `fork/exec /usr/bin/bash: no such file or directory`  (bash exists — the job's *cwd* is
# root-owned/inaccessible, so act_runner's Go exec cannot chdir into it to launch the step shell).
# The job's own "Reclaim workspace" step can't fix it: that step is the very step that can't launch.
# So the reclaim MUST happen OUT of band, as root, at a point act_runner controls.
#
# HOW IT RUNS (see the gitea_runner role) ------------------------------------------------------
#   1. ExecStartPre of gitea-act-runner.service  -> race-free clean slate on every (re)start
#      (daemon not yet polling => no job can be in flight).
#   2. gitea-runner-reclaim.timer (~every 30s)   -> between-jobs sweep during long daemon uptimes,
#      IDLE-GUARDED so it NEVER touches an in-flight job's workspace.
# Both invocations run as ROOT (ExecStartPre uses the systemd '+' prefix; the timer's oneshot has no
# User=). Root is required to delete root-owned leftovers and to chown-heal them back to the runner.
#
# SAFETY: best-effort, ALWAYS exits 0 (never blocks the daemon from starting). It only ever removes a
# fixed allow-list of ARTIFACT subdirs (never the checkout, never .git) and chowns foreign-owned
# leftovers back to the runner; it never deletes the workspace itself.
#
# Keep GITEA_RECLAIM_DIRS in sync with cchifor/platform:.github/runner-hooks/job-started.sh and the
# github_runner role's job-started.sh (the GitHub pool's equivalent hook).
set -uo pipefail

# ---- config (rendered by the role: /etc/gitea-runner-reclaim.env) ----------------------------
ENV_FILE="${GITEA_RECLAIM_ENV_FILE:-/etc/gitea-runner-reclaim.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

SERVICE="${GITEA_RUNNER_SERVICE:-gitea-act-runner.service}"
WORKDIR_PARENT="${GITEA_RUNNER_WORKDIR_PARENT:-/home/runner/act-runner/work}"
RUNNER_USER="${GITEA_RUNNER_USER:-runner}"
RUNNER_GROUP="${GITEA_RUNNER_GROUP:-runner}"
CONFIG_DIR="${GITEA_RUNNER_CONFIG_DIR:-/home/runner/act-runner}"
DIRS="${GITEA_RECLAIM_DIRS:-.hatchet-config coverage-data tests/e2e/test-results tests/e2e/blob-report tests/e2e/playwright-report}"
BEACON="${GITEA_RECLAIM_BEACON:-1}"
TEXTFILE_DIR="${GITEA_RECLAIM_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
# Job-boundary leftover reaper (gitea-runner-reap.py). Runs on every timer tick BEFORE the busy gate:
# the moment a new task starts is "busy" by definition, and its safety comes from creation time, not
# from idleness. Under ExecStartPre it does nothing: there is no act_runner MainPID yet, and the reaper
# refuses before any journal or docker call (so the 2026-07-28 start-path shape cannot recur). It also
# handles mode `off` itself, so the kill switch still publishes a mode=0 beacon.
REAP_BIN="${GITEA_REAP_BIN:-/usr/local/bin/gitea-runner-reap.py}"
REAP_TIMEOUT="${GITEA_REAP_TIMEOUT_SEC:-60}"
CACHE_VERIFY_BIN="${GITEA_CACHE_VERIFY_BIN:-/usr/local/bin/gitea-runner-cache-verify.py}"
CACHE_PURGE_QUEUE="${GITEA_CACHE_PURGE_QUEUE:-/var/lib/gitea-runner-cache/purge.queue}"
CACHE_PURGE_ATTEMPT="${GITEA_CACHE_PURGE_ATTEMPT_STAMP:-/var/lib/gitea-runner-reclaim/cache-purge.attempt}"

log() { logger -t gitea-runner-reclaim -- "$*" 2>/dev/null || true; }

# Refuse to operate on an implausible parent (guards against a broken env file): require an absolute
# path with at least two components so we can never rm under '/' or a top-level dir.
case "$WORKDIR_PARENT" in
  /*/*) : ;;
  *) log "abort: implausible WORKDIR_PARENT='$WORKDIR_PARENT'"; exit 0 ;;
esac

# ---- GLOBAL idle gate: is a gitea job in flight? ---------------------------------------------
# capacity=1, so the daemon has child processes (the job's step: bash/git/node/docker CLI) IFF a job
# is running; when truly idle it has none. This signal is gitea-SPECIFIC: the co-located GitHub
# runner's job processes are children of a DIFFERENT service, so they never false-trigger here.
daemon_busy() {
  local mainpid
  mainpid="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
  [ -n "$mainpid" ] && [ "$mainpid" -gt 0 ] 2>/dev/null || return 1  # daemon down (e.g. ExecStartPre) => not busy
  pgrep -P "$mainpid" >/dev/null 2>&1                                # has children => busy
}

# ---- PER-WORKSPACE guard: containers bind-mounting the gitea workdir --------------------------
# A job container writes the workspace through a HOST bind mount; a container process's /proc/<pid>/cwd
# points INSIDE the container (e.g. /w), not at the host path, so /proc alone can't see it. Collect the
# set of host workspace paths currently bind-mounted by any running container. GitHub-pool containers
# mount /home/runner/actions-runner/_work (NOT our parent), so they are correctly ignored.
BUSY_MOUNTS=""
collect_busy_mounts() {
  command -v docker >/dev/null 2>&1 || return 0
  local cids
  cids="$(timeout 15 docker ps -q 2>/dev/null)" || return 0
  [ -n "$cids" ] || return 0
  # shellcheck disable=SC2086
  BUSY_MOUNTS="$(timeout 15 docker inspect $cids \
      --format '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null \
      | grep -F "$WORKDIR_PARENT/" || true)"
}

# ---- PER-RUN /proc snapshot: processes with cwd/root under the workdir parent -----------------
# ONE readlink invocation resolves every /proc/<pid>/{cwd,root} (one line per resolvable link;
# vanished/unreadable entries are silently skipped), filtered to targets under WORKDIR_PARENT — the
# only ones that can ever match a workspace. This MUST stay a single pass: the old code re-scanned
# /proc per workspace (2 readlink forks x nproc x workspaces). agentforge's ephemeral per-repo CI
# grows work/ without bound (~350-400 dirs/VM by 2026-07-28), so ExecStartPre burned >90s of fork
# overhead, systemd's default start timeout killed it, and Restart=always looped — the whole runner
# fleet offline. Pinned by tests/test-reclaim.sh. (Snapshot-vs-check race is unchanged from the old
# code; the authoritative guards remain daemon_busy + the container-mount scan.)
BUSY_PROC_PATHS=""
collect_busy_proc_paths() {
  BUSY_PROC_PATHS="$(readlink /proc/[0-9]*/cwd /proc/[0-9]*/root 2>/dev/null \
      | grep -F "$WORKDIR_PARENT/" || true)"
}

# A workspace is in-use if a running container bind-mounts it OR a live process has cwd/root under it.
# Both checks are fork-free string matches against the per-run snapshots collected above.
ws_in_use() {
  local ws="$1" tgt
  if [ -n "$BUSY_MOUNTS" ] && printf '%s\n' "$BUSY_MOUNTS" | grep -qF -- "$ws"; then
    return 0
  fi
  while IFS= read -r tgt; do
    case "$tgt" in "$ws"|"$ws"/*) return 0 ;; esac
  done <<< "$BUSY_PROC_PATHS"
  return 1
}

# ---- reclaim one workspace (already confirmed NOT in use) ------------------------------------
reclaim_ws() {
  local ws="$1" d n
  # 1) remove the known artifact dirs (repo root + one nested monorepo level, e.g. apps/<x>/...),
  #    matching the platform job-started hook. rm as root removes root/foreign-owned trees that the
  #    runner-uid job could not.
  for d in $DIRS; do
    rm -rf -- "$ws/$d" 2>/dev/null || true
    for n in "$ws"/*/"$d"; do
      [ -e "$n" ] && rm -rf -- "$n" 2>/dev/null || true
    done
  done
  # 2) heal ownership of ANY remaining foreign-owned leftover so the next job can enter its cwd (and
  #    its own git-clean / reclaim step can run). Touches ONLY not-runner-owned paths (minimal writes).
  find "$ws" \( ! -user "$RUNNER_USER" -o ! -group "$RUNNER_GROUP" \) \
    -exec chown "$RUNNER_USER:$RUNNER_GROUP" -- {} + 2>/dev/null || true
}

# ---- health beacon -> node_exporter textfile collector (no-op if the dir is absent) ----------
# Separate file + metric names from the github_runner beacon (runner_health.prom) to avoid clobbering
# it (both roles are co-located on these VMs). "last_run" not "last_success": it is bumped every tick,
# incl. an idle-skip, so ABSENCE (not staleness) means the reclaim mechanism is broken.
write_beacon() {
  [ "$BEACON" = "1" ] || return 0
  [ -d "$TEXTFILE_DIR" ] || return 0
  local status="$1" foreign="${2:-0}" cleaned="${3:-0}" tmp
  tmp="$TEXTFILE_DIR/gitea_runner_reclaim.prom.$$"
  {
    echo '# HELP gitea_runner_reclaim_last_run_seconds Unix time the act_runner workspace reclaim last ran (incl. idle-skips).'
    echo '# TYPE gitea_runner_reclaim_last_run_seconds gauge'
    echo "gitea_runner_reclaim_last_run_seconds $(date +%s)"
    echo '# HELP gitea_runner_reclaim_busy_skip 1 if the last reclaim tick was skipped because a job was in flight.'
    echo '# TYPE gitea_runner_reclaim_busy_skip gauge'
    echo "gitea_runner_reclaim_busy_skip $([ "$status" = busy ] && echo 1 || echo 0)"
    echo '# HELP gitea_runner_workspace_foreign_owned_files Non-runner-owned files found under the act_runner workdir before reclaim.'
    echo '# TYPE gitea_runner_workspace_foreign_owned_files gauge'
    echo "gitea_runner_workspace_foreign_owned_files ${foreign:-0}"
  } > "$tmp" 2>/dev/null && mv -f "$tmp" "$TEXTFILE_DIR/gitea_runner_reclaim.prom" 2>/dev/null \
    || rm -f "$tmp" 2>/dev/null
}

run_reaper() {
  [ -x "$REAP_BIN" ] || return 0
  # Bounded (and the reaper keeps its own 45s deadline) so a slow docker daemon cannot hold this
  # oneshot past its TimeoutStartSec (120s) together with the reclaim's own docker/find/chown work.
  timeout -k 5 "$REAP_TIMEOUT" "$REAP_BIN" || log "reaper exited rc=$?"
}

# Shared-cache purge queue: applied here ONLY while act_runner is DOWN (MainPID 0): its ExecStartPre,
# or any stretch the daemon is stopped, so no Gitea job can be installing from an entry while it is
# removed. The cleanup never purges; in an idle gap it restarts act_runner to get here. A co-located
# GitHub agent shares the caches: a GitHub job (Runner.Worker) running when the purge would start
# defers it (checked once, at the start; the agent is dormant on every runner as of 2026-10-12). The
# stamp is written AFTER apply (which leaves an unchanged queue untouched), so while the daemon stays
# down an unchanged queue is retried at most every 10 minutes. 300s fits act_runner's 10min start.
daemon_down() {
  local mainpid
  mainpid="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null)" || return 1
  [ "$mainpid" = 0 ]
}

apply_cache_purges() {
  [ -s "$CACHE_PURGE_QUEUE" ] && [ -x "$CACHE_VERIFY_BIN" ] || return 0
  daemon_down || return 0
  pgrep -f 'Runner\.Worker' >/dev/null 2>&1 && return 0
  if [ -e "$CACHE_PURGE_ATTEMPT" ] && ! [ "$CACHE_PURGE_QUEUE" -nt "$CACHE_PURGE_ATTEMPT" ] \
     && [ $(( $(date +%s) - $(stat -c %Y "$CACHE_PURGE_ATTEMPT" 2>/dev/null || echo 0) )) -lt 600 ]; then
    return 0
  fi
  timeout -k 5 300 "$CACHE_VERIFY_BIN" apply 2>&1 | logger -t gitea-runner-cache-purge 2>/dev/null || true
  mkdir -p "$(dirname "$CACHE_PURGE_ATTEMPT")" 2>/dev/null; touch "$CACHE_PURGE_ATTEMPT" 2>/dev/null || true
}

main() {
  local status=ok foreign=0 cleaned=0 ws
  run_reaper
  apply_cache_purges
  if daemon_busy; then
    status=busy
    log "skip: gitea job in flight (daemon has child processes)"
  else
    collect_busy_mounts
    collect_busy_proc_paths
    if [ -d "$WORKDIR_PARENT" ]; then
      foreign="$(find "$WORKDIR_PARENT" ! -user "$RUNNER_USER" 2>/dev/null | wc -l | tr -d ' ')"
      for ws in "$WORKDIR_PARENT"/*/hostexecutor; do
        [ -d "$ws" ] || continue
        if ws_in_use "$ws"; then
          log "skip in-use workspace: $ws"
          continue
        fi
        reclaim_ws "$ws"
        cleaned=$((cleaned + 1))
      done
    fi
    # act creates the actcache parent (.cache) as root on first run — heal it so the runner-owned
    # daemon can manage its cache dir.
    [ -d "$CONFIG_DIR/.cache" ] && chown -R "$RUNNER_USER:$RUNNER_GROUP" "$CONFIG_DIR/.cache" 2>/dev/null || true
    log "reclaim ok: workspaces_cleaned=$cleaned foreign_owned_before=$foreign"
  fi
  write_beacon "$status" "$foreign" "$cleaned"
  exit 0
}

main "$@"
