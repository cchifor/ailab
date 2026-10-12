#!/usr/bin/env bash
# Self-contained behavioural test for gitea-runner-reclaim.sh (plain bash, mirrors test-cleanup.sh —
# mocked systemctl/docker/logger on PATH; real find/rm/chown against a throwaway fixture tree).
#
# Pins the 2026-07-28 fleet-outage fix: the /proc cwd/root in-use scan must be a SINGLE pass. The old
# code re-scanned /proc PER WORKSPACE (2 readlink forks x nproc x workspaces): agentforge's ephemeral
# per-repo CI had grown work/ to ~350-400 dirs per VM, so ExecStartPre burned >90s of pure fork()
# overhead, systemd killed it (default start timeout), and Restart=always looped — all 5 runners
# offline. Assertion [B] counts readlink INVOCATIONS via a delegating mock: O(1), not O(workspaces).
#
# Usage: bash ansible/roles/gitea_runner/tests/test-reclaim.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../files/gitea-runner-reclaim.sh"
[ -r "$SCRIPT" ] || { echo "FATAL: cannot read $SCRIPT"; exit 2; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"
CALLS="$WORK/calls.log"; : > "$CALLS"
REAL_READLINK="$(command -v readlink)"

# ---- mock binaries -------------------------------------------------------------------------------
# readlink: count invocations (the O(1)-scan pin), then delegate to the real binary so the script's
# actual /proc resolution still works.
cat >"$BIN/readlink" <<EOF
#!/usr/bin/env bash
echo "readlink \$#" >> "$CALLS"
exec "$REAL_READLINK" "\$@"
EOF
# systemctl show -p MainPID --value -> 0: daemon down (the ExecStartPre situation) => not busy.
# MOCK_MAINPID unset -> 0: daemon down (the ExecStartPre situation) => not busy.
printf '#!/usr/bin/env bash\necho "${MOCK_MAINPID:-0}"\n' >"$BIN/systemctl"
# docker ps -q -> no containers (no bind-mount busy paths).
printf '#!/usr/bin/env bash\nexit 0\n' >"$BIN/docker"
printf '#!/usr/bin/env bash\nexit 0\n' >"$BIN/logger"
# The job-boundary reaper: a recording mock. The runner VMs this suite runs on have the REAL reaper at
# /usr/local/bin/gitea-runner-reap.py, so every run_reclaim must point GITEA_REAP_BIN here.
printf '#!/usr/bin/env bash\necho "reap $*" >> "%s"\n' "$CALLS" >"$BIN/reap-mock"
# The cache purge applier: a recording mock (the real one is root-only on the host).
# It rewrites the queue (touch) the way a failing real apply can, which must not defeat the retry limit.
printf '#!/usr/bin/env bash\necho "cacheverify $*" >> "%s"\ntouch "$GITEA_CACHE_PURGE_QUEUE"\n' "$CALLS" >"$BIN/cacheverify-mock"
# pgrep: the co-located GitHub job probe (`pgrep -f Runner\.Worker`) answers MOCK_GH_WORKER, so a real GitHub
# job on the VM running this suite cannot skew [E]; every other call is the real pgrep.
REAL_PGREP="$(command -v pgrep)"
cat >"$BIN/pgrep" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "-f" ] && [ "\$2" = 'Runner\.Worker' ]; then [ "\${MOCK_GH_WORKER:-0}" = 1 ]; exit; fi
exec "$REAL_PGREP" "\$@"
EOF
# getent: the runner user's passwd home is $WORK/pwhome, so the reclaim's DEFAULT ~/.docker derivation
# can be exercised without touching the real home of whoever runs this suite.
cat >"$BIN/getent" <<EOF
#!/usr/bin/env bash
[ "\$1" = passwd ] && echo "\$2:x:1000:1000::$WORK/pwhome:/bin/bash"
EOF
chmod +x "$BIN"/*

# ---- fixture: many reused workspaces, like a runner after weeks of agentforge ephemeral repos ----
WSROOT="$WORK/parent/work" # two path components below /, so the script's WORKDIR_PARENT guard passes
N_WS=120
for i in $(seq 1 "$N_WS"); do
  ws="$WSROOT/repohash$i/hostexecutor"
  mkdir -p "$ws/.hatchet-config" "$ws/coverage-data" "$ws/tests/e2e/test-results" \
    "$ws/nested/coverage-data" "$ws/src"
  echo keep > "$ws/src/checkout-file"
done

run_reclaim() {
  GITEA_RECLAIM_ENV_FILE=/nonexistent \
    GITEA_REAP_BIN="${REAP_BIN_OVERRIDE:-$BIN/reap-mock}" \
    GITEA_CACHE_VERIFY_BIN="$BIN/cacheverify-mock" GITEA_CACHE_PURGE_QUEUE="${QUEUE_OVERRIDE:-$WORK/no-queue}" \
    GITEA_CACHE_PURGE_ATTEMPT_STAMP="$WORK/purge.attempt" GITEA_RECLAIM_CGROUP_FILE="${CGROUP_OVERRIDE:-$WORK/cg.timer}" \
    GITEA_RUNNER_WORKDIR_PARENT="$WSROOT" \
    GITEA_RUNNER_USER="$(id -un)" GITEA_RUNNER_GROUP="$(id -gn 2>/dev/null || id -g)" \
    GITEA_RUNNER_CONFIG_DIR="$WORK/cfg" \
    GITEA_RECLAIM_BEACON="${BEACON_OVERRIDE:-0}" GITEA_RECLAIM_TEXTFILE_DIR="${TF_OVERRIDE:-/nonexistent}" \
    GITEA_RECLAIM_DOCKER_CFG="${DOCKERCFG_OVERRIDE-$WORK/home/.docker}" \
    GITEA_RECLAIM_GITHUB_RECLAIM_BIN="${GHRECLAIM_OVERRIDE:-$WORK/no-github-reclaim}" \
    PATH="$BIN:$PATH" bash "$SCRIPT" "$@" >/dev/null 2>&1
}

fails=0
ok()  { echo "  PASS: $1"; }
bad() { echo "  FAIL: $1"; fails=$((fails+1)); }

echo "[A] reclaim removes the artifact allow-list (top-level + one nested level), keeps the checkout"
run_reclaim
a="$WSROOT/repohash7/hostexecutor"
{ [ ! -e "$a/.hatchet-config" ] && [ ! -e "$a/coverage-data" ] && [ ! -e "$a/tests/e2e/test-results" ]; } \
  && ok "A: artifact dirs removed" || bad "A: artifact dirs removed"
[ ! -e "$a/nested/coverage-data" ] && ok "A: nested artifact dir removed" || bad "A: nested artifact dir removed"
[ -f "$a/src/checkout-file" ] && ok "A: checkout content kept" || bad "A: checkout content kept"

echo "[B] the /proc in-use scan is ONE pass, not one per workspace  (THE 2026-07-28 OUTAGE FIX)"
rl_calls="$(grep -c '^readlink' "$CALLS" || true)"
if [ "${rl_calls:-0}" -le 5 ]; then
  ok "B: readlink invoked ${rl_calls}x for ${N_WS} workspaces (O(1) scan)"
else
  bad "B: readlink invoked ${rl_calls}x for ${N_WS} workspaces — per-workspace /proc rescan is back"
fi

echo "[C] a workspace with a live process cwd inside it is SKIPPED"
if [ -e "/proc/$$/cwd" ]; then
  busy="$WSROOT/repohash3/hostexecutor"
  mkdir -p "$busy/coverage-data"
  ( cd "$busy" && exec sleep 20 ) & guard=$!
  sleep 1 # let the subshell exec so /proc/<pid>/cwd points into the workspace
  run_reclaim
  kill "$guard" 2>/dev/null || true
  [ -d "$busy/coverage-data" ] && ok "C: in-use workspace left alone" || bad "C: in-use workspace left alone"
else
  echo "  SKIP: /proc/<pid>/cwd not available on this platform (runs on the Linux runner VMs)"
fi

echo "[D] every reclaim run calls the job-boundary reaper once (it refuses on its own without a MainPID)"
: > "$CALLS"; run_reclaim
[ "$(grep -c '^reap' "$CALLS")" -eq 1 ] && ok "D: tick calls the reaper once" || bad "D: tick calls the reaper once"
: > "$CALLS"; REAP_BIN_OVERRIDE="$WORK/no-such-reaper" run_reclaim
[ -d "$WSROOT" ] && ok "D: a missing reaper binary does not break the reclaim" || bad "D: a missing reaper binary does not break the reclaim"
unset REAP_BIN_OVERRIDE

echo "[E] the cache purge queue is applied only as act_runner's ExecStartPre (its start transaction), and only when non-empty"
printf '0::/system.slice/gitea-act-runner.service\n' > "$WORK/cg.start"
printf '0::/system.slice/gitea-runner-reclaim.service\n' > "$WORK/cg.timer"
CGROUP_OVERRIDE="$WORK/cg.start"
echo "npm sha512:$(printf 'a%.0s' $(seq 128))" > "$WORK/queue"
rm -f "$WORK/purge.attempt"; : > "$CALLS"; QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify apply' "$CALLS" && ok "E: inside act_runner's start (ExecStartPre) a non-empty queue is applied" || bad "E: inside act_runner's start (ExecStartPre) a non-empty queue is applied"
: > "$CALLS"; QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify' "$CALLS" && bad "E: an unchanged queue must not be retried every tick" || ok "E: unchanged queue -> not retried within 10m"
touch -d 'now + 5 seconds' "$WORK/queue"; : > "$CALLS"; QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify apply' "$CALLS" && ok "E: a queue rewritten since the last attempt is applied at once" || bad "E: a queue rewritten since the last attempt is applied at once"
rm -f "$WORK/purge.attempt"
: > "$CALLS"; MOCK_GH_WORKER=1 QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify' "$CALLS" && bad "E: a running GitHub job must defer the purge" || ok "E: co-located GitHub job running -> purge deferred"
rm -f "$WORK/purge.attempt"
: > "$CALLS"; MOCK_MAINPID=1 QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify' "$CALLS" && bad "E: a running daemon must not get the queue applied" || ok "E: daemon running -> queue left alone"
: > "$WORK/queue"; : > "$CALLS"; QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify' "$CALLS" && bad "E: an empty queue must not run the applier" || ok "E: an empty queue is a no-op"
echo "npm sha512:$(printf 'a%.0s' $(seq 128))" > "$WORK/queue"; rm -f "$WORK/purge.attempt"
: > "$CALLS"; CGROUP_OVERRIDE="$WORK/cg.timer" QUEUE_OVERRIDE="$WORK/queue" run_reclaim
grep -q '^cacheverify' "$CALLS" && bad "E: the TIMER tick must never purge, even with the daemon down" || ok "E: timer tick with daemon down -> no purge (not inside a start)"
unset QUEUE_OVERRIDE CGROUP_OVERRIDE

echo "[G] ~/.docker self-heal (moved here from the retired GitHub agent's reclaim) + its beacon"
mkdir -p "$WORK/home/.docker/buildx" "$WORK/tf"; : > "$WORK/home/.docker/buildx/instances"
: > "$CALLS"; BEACON_OVERRIDE=1 TF_OVERRIDE="$WORK/tf" run_reclaim
grep -q '^runner_docker_config_root_owned 0$' "$WORK/tf/gitea_runner_docker_config.prom" 2>/dev/null \
  && ok "G: beacon published (0 root-owned after the heal)" || bad "G: no runner_docker_config_root_owned beacon"
[ -f "$WORK/home/.docker/buildx/instances" ] && ok "G: the config itself is kept" || bad "G: the heal removed config files"
mkdir -p "$WORK/elsewhere"; rm -rf "$WORK/home2"; mkdir -p "$WORK/home2"; ln -s "$WORK/elsewhere" "$WORK/home2/.docker"
DOCKERCFG_OVERRIDE="$WORK/home2/.docker" run_reclaim
[ ! -L "$WORK/home2/.docker" ] && [ -d "$WORK/elsewhere" ] && ok "G: a symlinked ~/.docker is dropped, its target untouched" \
  || bad "G: symlinked ~/.docker not dropped"
# Busy (MainPID 1 has children): the beacon dir EXISTS and ~/.docker is a symlink, so a heal that ran
# anyway would both write the beacon and drop the link. Neither may happen.
mkdir -p "$WORK/tf2" "$WORK/elsewhere3"; rm -rf "$WORK/home3"; mkdir -p "$WORK/home3"; ln -s "$WORK/elsewhere3" "$WORK/home3/.docker"
: > "$CALLS"; MOCK_MAINPID=1 BEACON_OVERRIDE=1 TF_OVERRIDE="$WORK/tf2" DOCKERCFG_OVERRIDE="$WORK/home3/.docker" run_reclaim
[ ! -e "$WORK/tf2/gitea_runner_docker_config.prom" ] && [ -L "$WORK/home3/.docker" ] \
  && ok "G: no heal while a job runs (no beacon, symlink untouched)" || bad "G: healed under a running job"
# The DEFAULT path: no override, so ~/.docker comes from the (mocked) passwd home $WORK/pwhome.
mkdir -p "$WORK/pwhome/.docker" "$WORK/tf3"; : > "$WORK/tf3/runner_health.prom"
: > "$CALLS"; BEACON_OVERRIDE=1 TF_OVERRIDE="$WORK/tf3" DOCKERCFG_OVERRIDE="" run_reclaim
grep -q '^runner_docker_config_root_owned 0$' "$WORK/tf3/gitea_runner_docker_config.prom" 2>/dev/null \
  && ok "G: default ~/.docker from the passwd home -> healed + beacon" || bad "G: the default DOCKER_CFG path does not heal/publish"
[ ! -e "$WORK/tf3/runner_health.prom" ] && ok "G: the retired agent's runner_health.prom (same gauge) is removed" \
  || bad "G: stale runner_health.prom left (duplicate series)"
# While the GitHub agent's reclaim still EXISTS (deployed before the retire), it owns the gauge: this
# must not write a second file with the same series, nor delete the agent's.
mkdir -p "$WORK/tf4"; : > "$WORK/tf4/runner_health.prom"; : > "$WORK/fake-github-reclaim"
: > "$CALLS"; BEACON_OVERRIDE=1 TF_OVERRIDE="$WORK/tf4" GHRECLAIM_OVERRIDE="$WORK/fake-github-reclaim" run_reclaim
[ ! -e "$WORK/tf4/gitea_runner_docker_config.prom" ] && [ -e "$WORK/tf4/runner_health.prom" ] \
  && ok "G: agent still present -> no duplicate gauge file, its runner_health.prom kept" \
  || bad "G: wrote a duplicate runner_docker_config_root_owned while the agent's file exists"

echo "[F] act_runner's start budget covers the reclaim's worst case at ExecStartPre"
UNIT_T="$(dirname "$SCRIPT")/../templates/gitea-act-runner.service.j2"
tss="$(sed -n 's/^TimeoutStartSec=\([0-9]*\)min$/\1/p' "$UNIT_T")"
# reaper 60+5, cache purge 300+5, and the workspace pass: the unit must allow at least 7 minutes.
[ -n "$tss" ] && [ "$tss" -ge 7 ] && ok "F: TimeoutStartSec=${tss}min >= 7min" || bad "F: act_runner TimeoutStartSec must stay >= 7min (got '${tss}')"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; else echo "$fails CHECK(S) FAILED"; exit 1; fi
