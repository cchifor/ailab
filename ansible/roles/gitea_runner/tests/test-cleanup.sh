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
  "image prune") : > "$WORK/phase.image"; exit 0 ;;
  "builder prune")
    # the UNWINDOWED cap prune (-af) is the phase the per-builder re-check must follow
    printf '%s' "\$*" | grep -q -- '-af' && : > "$WORK/phase.afprune"
    exit 0 ;;
  "container prune"|"network prune") exit 0 ;;
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

# systemctl: `show -p MainPID` -> always a live pid (the daemon is always running; what varies is
# whether it has job children). `is-active --quiet <svc>` -> success, i.e. the co-located peer unit
# exists, which is what the peer branch gates on before probing for its per-job worker.
cat >"$BIN/systemctl" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *is-active*) exit 0 ;;
  *) echo 4242 ;;
esac
EOF

# pgrep. Two DISTINCT uses in the script, and the mock must honour both, including exit codes —
# real pgrep exits 1 when nothing matches, and the peer branch tests the EXIT STATUS rather than
# the output. An earlier version of this mock always exited 0, which made every peer probe read
# "busy" and silently broke five cases.
#   pgrep -P <pid>  -> OUR act_runner's job children. Honours MOCK_BUSY / _DROP_AFTER / _FROM / _UNTIL.
#   pgrep -f <re>   -> the PEER GitHub agent's per-job worker (Runner.Worker). Honours MOCK_PEER_JOB
#                      only: its supervisor always has a helper child, so "has children" is NOT a
#                      valid busy signal for it and the script must not use one.
cat >"$BIN/pgrep" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *-f*)
    [ "\${MOCK_PEER_JOB:-0}" = 1 ] && { echo 5150; exit 0; }
    exit 1 ;;
esac
if [ -n "\${MOCK_BUSY_FROM:-}" ]; then
  [ -f "$WORK/phase.\$MOCK_BUSY_FROM" ] && { echo 4243; exit 0; }
  exit 1
fi
if [ -n "\${MOCK_BUSY_UNTIL:-}" ]; then
  [ -f "$WORK/phase.\$MOCK_BUSY_UNTIL" ] && exit 1
  echo 4243; exit 0
fi
[ "\${MOCK_BUSY:-0}" = 1 ] || exit 1
if [ -n "\${MOCK_BUSY_DROP_AFTER:-}" ]; then
  n=0; [ -f "$WORK/pgrep.count" ] && n="\$(cat "$WORK/pgrep.count")"
  n=\$((n+1)); echo "\$n" > "$WORK/pgrep.count"
  [ "\$n" -gt "\$MOCK_BUSY_DROP_AFTER" ] && exit 1
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
  : > "$CALLS"; rm -f "$WORK/pgrep.count" "$WORK"/phase.*
  MOCK_BUSY="$1" MOCK_PCT="$2" PATH="$BIN:$PATH" \
    MOCK_CACHE="${MOCK_CACHE:-0}" MOCK_BUSY_DROP_AFTER="${MOCK_BUSY_DROP_AFTER:-}" \
    MOCK_BUSY_FROM="${MOCK_BUSY_FROM:-}" MOCK_BUSY_UNTIL="${MOCK_BUSY_UNTIL:-}" \
    MOCK_PEER_JOB="${MOCK_PEER_JOB:-0}" \
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

echo "[H7/H8] the size cap's busy RE-CHECKS (mutation-proven, not merely present)"
# The #253 review mutation-tested the first re-check and found the suite stayed green without it —
# H4 short-circuits on midjob=1 long before the re-check runs, so nothing covered it. Both cases here
# start IDLE (the busy gate lets the sweep proceed) and a job appears at a named phase afterwards.
MOCK_CACHE=26 MOCK_BUSY_FROM=image run_case 0 85
if grep -q 'builder prune -af' "$CALLS"; then bad "H7: a job arriving before the cap must abort the full prune"; else ok "H7: job arriving mid-sweep aborts the full prune"; fi

MOCK_CACHE=26 MOCK_BUSY_FROM=afprune run_case 0 85
if ! grep -q 'builder prune -af' "$CALLS"; then bad "H8: the default-builder prune should still have run"
elif grep -q 'buildx prune -af' "$CALLS"; then bad "H8: a job arriving after the default prune must abort the per-builder prunes"
else ok "H8: job arriving after the default prune aborts the per-builder prunes"; fi
unset MOCK_BUSY_FROM MOCK_CACHE

echo "[H9] the midjob GATE itself (not the re-check that shadows it)"
# The #254 review mutation-tested the `[ "$midjob" -eq 0 ]` gate and the suite stayed ALL PASS: H4
# names the mid-job path but its mock is busy for the WHOLE sweep, so the busy re-check satisfies the
# assertion and the gate could be deleted unnoticed. The gate is load-bearing precisely where the
# re-check is weakest — the file documents that pgrep reads false between a job's steps, which is
# exactly a job that looks idle at cap time while a critical mid-job sweep is in flight.
# Busy at the gate (-> wait -> still busy -> critical -> midjob=1), then idle from the image prune on,
# so the re-check would say "go". Only the midjob gate stops the unwindowed prune here.
MOCK_CACHE=26 MOCK_BUSY_UNTIL=image run_case 1 95
if grep -q 'builder prune -af' "$CALLS"; then bad "H9: the midjob gate must block the full prune even when the re-check reads idle"; else ok "H9: midjob gate blocks the full prune when the re-check reads idle"; fi
unset MOCK_BUSY_UNTIL MOCK_CACHE

echo "[I] the PEER runner's busy signal is its per-job worker, not its supervisor's child"
# The co-located GitHub agent runs a supervisor (run.sh) that ALWAYS has a run-helper.sh child. Using
# "MainPID has children" for it made the peer read BUSY permanently, so no idle window ever existed
# and this timer starved while disks climbed to 93% (measured 2026-08-08, zero gap-catches fleet-wide).
# The mock reflects that shape: pgrep -f matches ONLY when a peer job is actually running.
MOCK_PEER_JOB=0 run_case 0 50
assert_has "image prune -af --filter until=48h" "I1: peer idle (supervisor child only) -> sweep runs"

MOCK_PEER_JOB=1 run_case 0 50
assert_none "I2: peer running a real job -> sweep skipped (shared daemon)"
unset MOCK_PEER_JOB

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
