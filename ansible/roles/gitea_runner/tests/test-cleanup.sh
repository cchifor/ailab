#!/usr/bin/env bash
# Self-contained behavioural test for gitea-runner-cleanup.sh (no bats/molecule dependency — plain bash).
# Runs the real script with mocked docker/df/systemctl/pgrep on PATH and asserts WHICH prune commands it
# issues under each (busy, disk%) combination. Pins the fix for the ENOSPC starvation death spiral:
# under disk pressure the window-safe reclaim MUST run even when a co-located runner is busy.
#
# Usage: bash ansible/roles/gitea_runner/tests/test-cleanup.sh   (exit 0 = pass)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../files/gitea-runner-cleanup.sh"
[ -r "$SCRIPT" ] || { echo "FATAL: cannot read $SCRIPT"; exit 2; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"
CALLS="$WORK/calls.log"

# ---- mock binaries -------------------------------------------------------------------------------
# docker: log every invocation; special-case the read subcommands the script parses.
cat >"$BIN/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$*" >> "$CALLS"
case "\$1 \$2" in
  "container prune"|"network prune"|"builder prune"|"image prune") exit 0 ;;
  "buildx prune") exit 0 ;;
  "buildx ls")
    # honour --format '{{.Name}}' (buildx >=0.13): one builder name per line.
    if printf '%s' "\$*" | grep -q -- '--format'; then
      printf '%s\n' default builder-leaked
    else
      printf '%s\n' "NAME/NODE DRIVER/ENDPOINT STATUS BUILDKIT PLATFORMS" \
                    "default* docker" "builder-leaked docker-container running v0.12 linux/amd64"
    fi ;;
  "system df")
    # MOCK_CACHE=<n> reports n GB of build cache so the size cap can be exercised; 0 (default) prints
    # nothing, which is also the "docker wedged / unparseable" case the cap must fail CLOSED on.
    if [ "\${MOCK_CACHE:-0}" != 0 ]; then printf 'Build Cache|%sGB\n' "\$MOCK_CACHE"; fi ;;
  "ps") : ;;          # 'docker ps -q' -> no containers
  *) : ;;
esac
exit 0
EOF

# df --output=pcent -> controlled MOCK_PCT
cat >"$BIN/df" <<EOF
#!/usr/bin/env bash
echo "Use%"; echo " \${MOCK_PCT:-0}%"
EOF

# systemctl show -p MainPID --value <svc> -> a pid iff MOCK_BUSY=1
cat >"$BIN/systemctl" <<EOF
#!/usr/bin/env bash
if [ "\${MOCK_BUSY:-0}" = 1 ]; then echo 4242; else echo 0; fi
EOF

# MOCK_BUSY_DROP_AFTER=<n>: report busy for the first n pgrep calls, then idle — simulates a job
# finishing DURING the sweep, which is the path the busy gate is supposed to wait for.
cat >"$BIN/busystate" <<EOF
#!/usr/bin/env bash
exit 0
EOF

# pgrep -P <pid> -> a child iff still "busy". With MOCK_BUSY_DROP_AFTER=n the mock reports busy for
# the first n calls and idle afterwards (a job that ends mid-sweep).
cat >"$BIN/pgrep" <<EOF
#!/usr/bin/env bash
[ "\${MOCK_BUSY:-0}" = 1 ] || exit 0
if [ -n "\${MOCK_BUSY_DROP_AFTER:-}" ]; then
  n=0; [ -f "$WORK/pgrep.count" ] && n="\$(cat "$WORK/pgrep.count")"
  n=\$((n+1)); echo "\$n" > "$WORK/pgrep.count"
  [ "\$n" -gt "\$MOCK_BUSY_DROP_AFTER" ] && exit 0
fi
echo 4243
exit 0
EOF

for stub in logger; do printf '#!/usr/bin/env bash\nexit 0\n' >"$BIN/$stub"; done
chmod +x "$BIN"/*

run_case() { # <busy> <pct>  (optional globals: WSP, WSAGE, MOCK_CACHE, MOCK_BUSY_DROP_AFTER, CACHECAP)
  # IDLE_WAIT_SEC is forced tiny here: the real default is 600s, because under pressure the busy gate
  # WAITS for the between-jobs gap instead of pruning through a live job. Left at its default, every
  # busy+pressure case below would stall this suite for 10 minutes.
  : > "$CALLS"; rm -f "$WORK/pgrep.count"
  MOCK_BUSY="$1" MOCK_PCT="$2" PATH="$BIN:$PATH" \
    MOCK_CACHE="${MOCK_CACHE:-0}" MOCK_BUSY_DROP_AFTER="${MOCK_BUSY_DROP_AFTER:-}" \
    GITEA_CLEANUP_ENV_FILE=/nonexistent \
    GITEA_RUNNER_WORKDIR_PARENT="${WSP:-/nonexistent/work}" \
    GITEA_CLEANUP_WS_PRUNE_AGE_H="${WSAGE:-48}" \
    GITEA_CLEANUP_IDLE_WAIT_SEC="${IDLEWAIT:-2}" GITEA_CLEANUP_IDLE_POLL_SEC=1 \
    GITEA_CLEANUP_CACHE_MAX_BYTES="${CACHECAP:-20000000000}" \
    GITEA_CLEANUP_BEACON=0 GITEA_CLEANUP_TEXTFILE_DIR=/nonexistent \
    bash "$SCRIPT" >/dev/null 2>&1 || true
}
calls_has() { grep -qF "$1" "$CALLS"; }

fails=0
ok()   { echo "  PASS: $1"; }
bad()  { echo "  FAIL: $1"; fails=$((fails+1)); echo "    --- calls ---"; sed 's/^/    /' "$CALLS"; }
assert_has()  { if calls_has "$1"; then ok "$2"; else bad "$2 (expected call: $1)"; fi; }
assert_none() { if [ -s "$CALLS" ] && grep -q 'prune' "$CALLS"; then bad "$1 (unexpected prune ran)"; else ok "$1"; fi; }

echo "[A] idle + low disk (50%) -> routine window prune (until=48h) runs"
run_case 0 50
assert_has "image prune -af --filter until=48h" "A: routine image prune @48h"

# 2026-08-08: this case USED to assert the opposite — that the pressure reclaim runs THROUGH a live
# job. That behaviour was the mid-job containerd-GC race (it reaps in-flight `docker pull` leases and
# reded ~10 CI jobs/day), so the script now waits for the between-jobs gap instead and simply defers
# when none appears. See the busy gate in gitea-runner-cleanup.sh.
echo "[B] BUSY + PRESSURE (85%), job never ends -> DEFERS, prunes nothing"
run_case 1 85
assert_none "B: no prune while busy at pressure (waits for the gap, then defers)"

echo "[B2] BUSY + PRESSURE (85%), job ends during the wait -> sweeps race-free"
MOCK_BUSY_DROP_AFTER=1 run_case 1 85
assert_has "image prune -af --filter until=6h" "B2: pressure sweep runs once the gap appears"
unset MOCK_BUSY_DROP_AFTER

echo "[C] BUSY + CRITICAL (95%) -> critical 1h-window reclaim runs"
run_case 1 95
assert_has "image prune -af --filter until=1h"  "C: critical image prune @1h while busy"

echo "[D] non-default buildx builders are pruned too (docker buildx prune --builder)"
run_case 0 85
assert_has "buildx prune -f --filter until=6h --builder builder-leaked" "D: per-builder buildx prune"

echo "[E] BUSY + low disk (50%) -> routine sweep SKIPPED (busy optimization preserved)"
run_case 1 50
assert_none "E: no prune while busy + below pressure"

echo "[H] build-cache SIZE cap"
# The `until=` windows cannot bound a continuously-REUSED cache (measured: until=1h reclaimed 1.7GB of
# 26GB), so an over-cap idle sweep escalates to a full `builder prune -af`. It must NEVER do that while
# a job runs, and must fail CLOSED when the size is unreadable.
MOCK_CACHE=26 run_case 0 85
assert_has "builder prune -af" "H1: over-cap + idle + pressure -> full prune"

MOCK_CACHE=12 run_case 0 85
if grep -q 'builder prune -af' "$CALLS"; then bad "H2: under-cap must NOT full-prune"; else ok "H2: under-cap leaves warm cache alone"; fi

MOCK_CACHE=26 run_case 0 50
if grep -q 'builder prune -af' "$CALLS"; then bad "H3: healthy disk must NOT full-prune"; else ok "H3: over-cap but disk healthy -> no full prune"; fi

MOCK_CACHE=26 run_case 1 95
if grep -q 'builder prune -af' "$CALLS"; then bad "H4: full prune must never run on the mid-job path"; else ok "H4: busy+critical (mid-job path) never full-prunes"; fi

MOCK_CACHE=0 run_case 0 85
if grep -q 'builder prune -af' "$CALLS"; then bad "H5: unreadable cache size must fail closed"; else ok "H5: unparseable size -> no full prune (fails closed)"; fi

CACHECAP=0 MOCK_CACHE=26 run_case 0 85
if grep -q 'builder prune -af' "$CALLS"; then bad "H6: cap=0 must disable the size cap"; else ok "H6: cap=0 disables the size cap"; fi
unset MOCK_CACHE CACHECAP

echo "[F] stale-workspace prune: no-activity dirs removed, fresh + partially-fresh kept"
# act_runner never deletes work/<repo-hash>; agentforge's ephemeral per-repo CI grew it to ~350-400
# dirs/VM, which is what made the reclaim script's per-workspace /proc scan blow its start-pre budget
# (2026-07-28 fleet outage — see test-reclaim.sh). The cleanup timer prunes any workspace whose ENTIRE
# tree is older than the age gate (48h >> the 3h job timeout, so a live/recent job always trips it).
WSP="$WORK/wsroot/work"
mkdir -p "$WSP/oldrepo/hostexecutor/sub" "$WSP/freshrepo/hostexecutor" "$WSP/agedrepo/hostexecutor"
echo x > "$WSP/oldrepo/hostexecutor/sub/f"; echo x > "$WSP/freshrepo/hostexecutor/f"
echo x > "$WSP/agedrepo/hostexecutor/stale"; find "$WSP/oldrepo" "$WSP/agedrepo" -exec touch -d '4 days ago' {} +
echo x > "$WSP/agedrepo/hostexecutor/one-fresh-file" # one recent write anywhere must protect the tree
run_case 0 50
[ ! -e "$WSP/oldrepo" ]   && ok "F: fully-stale workspace pruned"        || bad "F: fully-stale workspace pruned"
[ -e "$WSP/freshrepo" ]   && ok "F: fresh workspace kept"                || bad "F: fresh workspace kept"
[ -e "$WSP/agedrepo" ]    && ok "F: workspace with one fresh file kept"  || bad "F: workspace with one fresh file kept"

echo "[G] prune age 0 disables the stale-workspace prune"
mkdir -p "$WSP/oldrepo2/hostexecutor"; echo x > "$WSP/oldrepo2/hostexecutor/f"
find "$WSP/oldrepo2" -exec touch -d '4 days ago' {} +
WSAGE=0 run_case 0 50
[ -e "$WSP/oldrepo2" ] && ok "G: prune disabled at age 0" || bad "G: prune disabled at age 0"
unset WSP

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; else echo "$fails CHECK(S) FAILED"; exit 1; fi
