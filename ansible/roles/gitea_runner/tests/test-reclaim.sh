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
printf '#!/usr/bin/env bash\necho 0\n' >"$BIN/systemctl"
# docker ps -q -> no containers (no bind-mount busy paths).
printf '#!/usr/bin/env bash\nexit 0\n' >"$BIN/docker"
printf '#!/usr/bin/env bash\nexit 0\n' >"$BIN/logger"
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
    GITEA_RUNNER_WORKDIR_PARENT="$WSROOT" \
    GITEA_RUNNER_USER="$(id -un)" GITEA_RUNNER_GROUP="$(id -gn 2>/dev/null || id -g)" \
    GITEA_RUNNER_CONFIG_DIR="$WORK/cfg" \
    GITEA_RECLAIM_BEACON=0 GITEA_RECLAIM_TEXTFILE_DIR=/nonexistent \
    PATH="$BIN:$PATH" bash "$SCRIPT" >/dev/null 2>&1
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

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; else echo "$fails CHECK(S) FAILED"; exit 1; fi
