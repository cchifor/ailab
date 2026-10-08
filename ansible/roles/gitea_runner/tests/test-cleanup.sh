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
  "image prune")
    : > "$WORK/phase.image"
    # MOCK_HANG_HEAL holds the heal's UNWINDOWED image prune long enough for the test to SIGTERM the
    # script mid-heal (case N10).
    if [ "\${MOCK_HANG_HEAL:-0}" = 1 ] && ! printf '%s' "\$*" | grep -q -- '--filter'; then
      : > "$WORK/phase.hang"; sleep 3
    fi
    exit 0 ;;
  "builder prune")
    # the UNWINDOWED cap prune (-af) is the phase the per-builder re-check must follow
    printf '%s' "\$*" | grep -q -- '-af' && : > "$WORK/phase.afprune"
    # MOCK_DU_CLEARS: the full prune removes the dangling record from BuildKit's record list, as the
    # real one does once dockerd has restarted (section 0 heal).
    if [ "\${MOCK_DU_CLEARS:-0}" = 1 ] && printf '%s' "\$*" | grep -q -- '-af' && [ -n "\${MOCK_DU:-}" ]; then
      : > "\$MOCK_DU"
    fi
    exit 0 ;;
  "builder du")
    # BuildKit's record list (section 0's confirmation): the contents of MOCK_DU; MOCK_DU_FAIL = unreadable.
    [ "\${MOCK_DU_FAIL:-0}" = 1 ] && exit 1
    [ -n "\${MOCK_DU:-}" ] && cat "\$MOCK_DU" 2>/dev/null
    exit 0 ;;
  "container prune") : > "$WORK/phase.container"; exit 0 ;;
  "network prune") exit 0 ;;
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
    # MOCK_CACHE / MOCK_IMAGES = size in GB for that row; 0 (default) prints nothing for it, which is
    # also the "docker wedged / unparseable" case each cap must fail CLOSED on.
    if [ "\${MOCK_CACHE:-0}" != 0 ]; then printf 'Build Cache|%sGB\n' "\$MOCK_CACHE"; fi
    if [ "\${MOCK_IMAGES:-0}" != 0 ]; then printf 'Images|%sGB\n' "\$MOCK_IMAGES"; fi ;;
  "ps") : ;;          # 'docker ps -q' -> no containers
  *) : ;;
esac
exit 0
EOF

# df --output=pcent -> controlled MOCK_PCT.
# MOCK_PCT_AFTER makes the disk CHANGE mid-run: reads after the first one return it instead. The script
# re-reads the disk after waiting up to IDLE_WAIT_SEC for a job gap, and in 600s the reclaim timer or a
# finishing job really can free space — so "pressure cleared while we waited" is a reachable state that
# a single fixed value cannot express at all. Case P5 depends on this.
cat >"$BIN/df" <<EOF
#!/usr/bin/env bash
echo "Use%"
n=0; [ -f "$WORK/df.count" ] && n="\$(cat "$WORK/df.count" 2>/dev/null)"
case "\$n" in '' | *[!0-9]*) n=0 ;; esac
n=\$(( n + 1 )); echo "\$n" > "$WORK/df.count"
if [ -n "\${MOCK_PCT_AFTER:-}" ] && [ "\$n" -gt 1 ]; then echo " \${MOCK_PCT_AFTER}%"; else echo " \${MOCK_PCT:-0}%"; fi
EOF

# systemctl mock.
#   show -p MainPID  -> a live pid, unless MOCK_SYSTEMCTL_FAIL=1 (then rc!=0, an unreadable signal)
#   is-active <svc>  -> MOCK_PEER_STATE (default "active"); "unreadable" makes it exit nonzero,
#                       which is what a systemctl/DBus failure looks like to the script. Our own
#                       runner unit answers from a STATE FILE ($WORK/runner.state, seeded from
#                       MOCK_RUNNER_STATE by run_case) that stop/start really change, so the heal's
#                       "seen stopped" / "seen active" reads are exercised, not just the commands.
#   stop/start/restart -> logged to the call log (the section 0 heal). `stop` of the runner leaves it
#                       active when MOCK_STOP_FAIL=1; `start` leaves it failed when MOCK_START_FAIL=1;
#                       `restart docker.service` exits MOCK_DOCKER_RESTART_RC.
cat >"$BIN/systemctl" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *is-active*gitea-act-runner*)
    st="\$(cat "$WORK/runner.state" 2>/dev/null)"; st="\${st:-active}"
    [ "\$st" = unreadable ] && exit 3
    echo "\$st"; [ "\$st" = active ] && exit 0 || exit 3 ;;
  *is-active*)
    st="\${MOCK_PEER_STATE:-active}"
    [ "\$st" = unreadable ] && exit 3
    echo "\$st"; [ "\$st" = active ] && exit 0 || exit 3 ;;
  *MainPID*)
    [ "\${MOCK_SYSTEMCTL_FAIL:-0}" = 1 ] && exit 1
    # A stopped unit has MainPID 0, as the real one does — the heal re-reads idle with it stopped.
    st="\$(cat "$WORK/runner.state" 2>/dev/null)"; st="\${st:-active}"
    if [ "\$st" = active ]; then echo "\${MOCK_MAINPID:-4242}"; else echo 0; fi ;;
  *"restart docker.service"*)
    echo "systemctl \$*" >> "$CALLS"; : > "$WORK/phase.dockerrestart"; exit "\${MOCK_DOCKER_RESTART_RC:-0}" ;;
  *stop*gitea-act-runner*)
    echo "systemctl \$*" >> "$CALLS"; : > "$WORK/phase.runnerstop"
    [ "\${MOCK_STOP_FAIL:-0}" = 1 ] && exit 1
    echo inactive > "$WORK/runner.state" ;;
  *start*gitea-act-runner*)
    echo "systemctl \$*" >> "$CALLS"
    if [ "\${MOCK_START_FAIL:-0}" = 1 ]; then echo failed > "$WORK/runner.state"; else echo active > "$WORK/runner.state"; fi ;;
  *stop* | *start*) echo "systemctl \$*" >> "$CALLS" ;;
  *) : ;;
esac
EOF

# journalctl -u docker.service -> the contents of MOCK_JOURNAL (default: an empty journal);
# MOCK_JOURNAL_FAIL=1 = the read fails.
cat >"$BIN/journalctl" <<EOF
#!/usr/bin/env bash
[ "\${MOCK_JOURNAL_FAIL:-0}" = 1 ] && exit 1
[ -n "\${MOCK_JOURNAL:-}" ] && cat "\$MOCK_JOURNAL" 2>/dev/null
exit 0
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
    # rc 2 models a pgrep ERROR (bad regex, /proc failure) — distinct from rc 1 "no match".
    [ "\${MOCK_PEER_PROBE_RC:-}" = 2 ] && exit 2
    [ "\${MOCK_PEER_JOB:-0}" = 1 ] && { echo 5150; exit 0; }
    # MOCK_PEER_BUSY_FROM=<phase>: the peer starts a job once the script reaches that phase.
    [ -n "\${MOCK_PEER_BUSY_FROM:-}" ] && [ -f "$WORK/phase.\$MOCK_PEER_BUSY_FROM" ] && { echo 5150; exit 0; }
    exit 1 ;;
  # Like the real one, pgrep -P 0 matches init and kthreadd: a script that probed a stopped unit's
  # MainPID 0 would read it as busy forever. (No backticks in this heredoc: it is unquoted.)
  "-P 0") echo 1; exit 0 ;;
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

# mv: the real one, except that MOCK_MV_FAIL=1 makes the rename of the heal STATE file fail — the
# last step of state_set, after its temp file was fully written (case N19).
REAL_MV="$(command -v mv)"
cat >"$BIN/mv" <<EOF
#!/usr/bin/env bash
if [ "\${MOCK_MV_FAIL:-0}" = 1 ]; then case "\$*" in *heal.state*) exit 1 ;; esac; fi
exec "$REAL_MV" "\$@"
EOF

# sort: the real one, except that MOCK_SORT_FAIL=1 makes it fail with no output (a temp-file I/O
# error, say) — the journal parse in section 0 must read that as unknown, not "none" (case N21).
REAL_SORT="$(command -v sort)"
cat >"$BIN/sort" <<EOF
#!/usr/bin/env bash
[ "\${MOCK_SORT_FAIL:-0}" = 1 ] && { cat > /dev/null; exit 2; }
exec "$REAL_SORT" "\$@"
EOF

# logger: keep the script's log lines (the last argument) in $WORK/logger.log, so a case can assert WHICH
# record a heal acted on, not only that one ran.
cat >"$BIN/logger" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do last="\$a"; done
printf '%s\n' "\${last:-}" >> "$WORK/logger.log"
exit 0
EOF
chmod +x "$BIN"/*

run_case() { # <busy> <pct>  (optional globals: WSP, WSAGE, MOCK_CACHE, MOCK_BUSY_DROP_AFTER, CACHECAP)
  # IDLE_WAIT_SEC is forced tiny here: the real default is 600s, because under pressure the busy gate
  # WAITS for the between-jobs gap instead of pruning through a live job. Left at its default, every
  # busy+pressure case below would stall this suite for 10 minutes.
  : > "$CALLS"; : > "$WORK/logger.log"; rm -f "$WORK/pgrep.count" "$WORK/df.count" "$WORK"/phase.*
  # KEEP_RUNNER_STATE=1 carries the runner's state over from the previous run (multi-tick cases).
  [ "${KEEP_RUNNER_STATE:-0}" = 1 ] || echo "${MOCK_RUNNER_STATE:-active}" > "$WORK/runner.state"
  MOCK_BUSY="$1" MOCK_PCT="$2" MOCK_PCT_AFTER="${MOCK_PCT_AFTER:-}" PATH="$BIN:$PATH" \
    MOCK_CACHE="${MOCK_CACHE:-0}" MOCK_IMAGES="${MOCK_IMAGES:-0}" MOCK_BUSY_DROP_AFTER="${MOCK_BUSY_DROP_AFTER:-}" \
    MOCK_BUSY_FROM="${MOCK_BUSY_FROM:-}" MOCK_BUSY_UNTIL="${MOCK_BUSY_UNTIL:-}" \
    MOCK_PEER_JOB="${MOCK_PEER_JOB:-0}" MOCK_PEER_STATE="${MOCK_PEER_STATE:-active}" \
    MOCK_PEER_PROBE_RC="${MOCK_PEER_PROBE_RC:-}" MOCK_SYSTEMCTL_FAIL="${MOCK_SYSTEMCTL_FAIL:-0}" \
    MOCK_MAINPID="${MOCK_MAINPID:-4242}" \
    GITEA_CLEANUP_ENV_FILE=/nonexistent \
    GITEA_RUNNER_WORKDIR_PARENT="${WSP:-/nonexistent/work}" \
    GITEA_CLEANUP_WS_PRUNE_AGE_H="${WSAGE:-48}" \
    GITEA_CLEANUP_IDLE_WAIT_SEC="${IDLEWAIT:-2}" GITEA_CLEANUP_IDLE_POLL_SEC=1 \
    GITEA_CLEANUP_CACHE_MAX_BYTES="${CACHECAP:-20000000000}" \
    GITEA_CLEANUP_CRITICAL_UNTIL="${CRITUNTIL:-4h}" \
    GITEA_CLEANUP_MIN_UNTIL_SEC="${MINUNTIL-14400}" \
    GITEA_CLEANUP_BEACON="${BEACON:-0}" GITEA_CLEANUP_TEXTFILE_DIR="${TEXTDIR:-/nonexistent}" \
    GITEA_CLEANUP_HEAL_DANGLING="${HEALON:-1}" GITEA_CLEANUP_HEAL_STATE="${HEALSTATE:-$WORK/heal.state}" \
    MOCK_JOURNAL="${MOCK_JOURNAL:-}" MOCK_JOURNAL_FAIL="${MOCK_JOURNAL_FAIL:-0}" \
    MOCK_DU="${MOCK_DU:-}" MOCK_DU_CLEARS="${MOCK_DU_CLEARS:-0}" MOCK_DU_FAIL="${MOCK_DU_FAIL:-0}" \
    MOCK_STOP_FAIL="${MOCK_STOP_FAIL:-0}" MOCK_START_FAIL="${MOCK_START_FAIL:-0}" \
    MOCK_DOCKER_RESTART_RC="${MOCK_DOCKER_RESTART_RC:-0}" MOCK_HANG_HEAL="${MOCK_HANG_HEAL:-0}" \
    MOCK_PEER_BUSY_FROM="${MOCK_PEER_BUSY_FROM:-}" MOCK_MV_FAIL="${MOCK_MV_FAIL:-0}" \
    MOCK_SORT_FAIL="${MOCK_SORT_FAIL:-0}" \
    bash "$SCRIPT" ${SCRIPT_ARGS:-} >/dev/null 2>&1 &
  local pid=$! i=0
  # TERM_AT_HANG: SIGTERM the script once the mock reports it is inside the heal (case N10) — what
  # systemd does to this unit at TimeoutStartSec.
  if [ "${TERM_AT_HANG:-0}" = 1 ]; then
    while [ ! -f "$WORK/phase.hang" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
    kill -TERM "$pid" 2>/dev/null
  fi
  wait "$pid" 2>/dev/null || true
}
calls_has() { grep -qF "$1" "$CALLS"; }

fails=0
ok()   { echo "  PASS: $1"; }
bad()  { echo "  FAIL: $1"; fails=$((fails+1)); echo "    --- calls ---"; sed 's/^/    /' "$CALLS"; }
assert_has()  { if calls_has "$1"; then ok "$2"; else bad "$2 (expected call: $1)"; fi; }
assert_none() { if [ -s "$CALLS" ] && grep -q 'prune' "$CALLS"; then bad "$1 (unexpected prune ran)"; else ok "$1"; fi; }

echo "[A] idle + low disk (50%) -> routine window prune (24h -> 86400s) runs"
# 24h, not 48h: run_case sources no env file, so the SCRIPT defaults are what this suite exercises —
# and those had drifted from the role defaults that actually ship (48h/80 vs the deployed 24h/75), so
# the deployed values were tested by nothing. Section [M] below now pins them together.
run_case 0 50
assert_has "image prune -af --filter until=86400s" "A: routine image prune @24h (emitted in seconds)"

# 2026-08-08: this case USED to assert the opposite — that the pressure reclaim runs THROUGH a live
# job. That behaviour was the mid-job containerd-GC race (it reaps in-flight `docker pull` leases and
# reded ~10 CI jobs/day), so the script now waits for the between-jobs gap instead and simply defers
# when none appears. See the busy gate in gitea-runner-cleanup.sh.
echo "[B] BUSY + PRESSURE (85%), job never ends -> DEFERS, prunes nothing"
run_case 1 85
assert_none "B: no prune while busy at pressure (waits for the gap, then defers)"

echo "[B2] BUSY + PRESSURE (85%), job ends during the wait -> sweeps race-free"
MOCK_BUSY_DROP_AFTER=1 run_case 1 85
assert_has "image prune -af --filter until=21600s" "B2: pressure sweep runs once the gap appears"
unset MOCK_BUSY_DROP_AFTER

echo "[C] BUSY + CRITICAL (95%) -> critical window reclaim runs, CLAMPED to the job-timeout floor"
run_case 1 95
assert_has "image prune -af --filter until=14400s" "C: critical window CLAMPED up to the 4h job-timeout floor"

echo "[D] non-default buildx builders are pruned too (docker buildx prune --builder)"
run_case 0 85
assert_has "buildx prune -f --filter until=21600s --builder builder-leaked" "D: per-builder buildx prune"

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
MOCK_CACHE=26 MOCK_BUSY_FROM=container run_case 0 85
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
MOCK_CACHE=26 MOCK_BUSY_UNTIL=container run_case 1 95
if grep -q 'builder prune -af' "$CALLS"; then bad "H9: the midjob gate must block the full prune even when the re-check reads idle"; else ok "H9: midjob gate blocks the full prune when the re-check reads idle"; fi
unset MOCK_BUSY_UNTIL MOCK_CACHE

echo "[I] the PEER runner's busy signal is its per-job worker, not its supervisor's child"
# The co-located GitHub agent runs a supervisor (run.sh) that ALWAYS has a run-helper.sh child. Using
# "MainPID has children" for it made the peer read BUSY permanently, so no idle window ever existed
# and this timer starved while disks climbed to 93% (measured 2026-08-08, zero gap-catches fleet-wide).
# The mock reflects that shape: pgrep -f matches ONLY when a peer job is actually running.
MOCK_PEER_JOB=0 run_case 0 50
assert_has "image prune -af --filter until=86400s" "I1: peer idle (supervisor child only) -> sweep runs"

MOCK_PEER_JOB=1 run_case 0 50
assert_none "I2: peer running a real job -> sweep skipped (shared daemon)"
unset MOCK_PEER_JOB

echo "[J] every UNCERTAIN busy signal must read BUSY, never idle"
# Codex review of the peer-signal change: the comment promised "fail toward busy" while three
# branches failed toward IDLE. A signal we cannot read, misread as idle, is exactly how a prune
# reaches a live job — the failure this whole gate exists to prevent. Cost is asymmetric: a false
# BUSY delays reclamation one tick; a false IDLE can red a running job.
MOCK_PEER_STATE=failed MOCK_PEER_JOB=1 run_case 0 50
assert_none "J1: peer unit 'failed' with a live worker -> BUSY, no prune"

MOCK_PEER_STATE=activating MOCK_PEER_JOB=1 run_case 0 50
assert_none "J2: peer unit 'activating' with a live worker -> BUSY, no prune"

MOCK_PEER_STATE=unreadable MOCK_PEER_JOB=1 run_case 0 50
assert_none "J3: peer state unreadable (systemctl/DBus failure) -> BUSY, no prune"

MOCK_PEER_PROBE_RC=2 run_case 0 50
assert_none "J4: peer probe ERROR (pgrep rc=2, not 'no match') -> BUSY, no prune"

MOCK_SYSTEMCTL_FAIL=1 run_case 0 50
assert_none "J5: our own MainPID unreadable -> BUSY, no prune"

# MOCK_PEER_JOB=1 is what makes this bite: with no peer job the probe returns "no match" and the sweep
# would run whether or not the `inactive` branch fires, so the case passed vacuously and a deleted
# `continue` went unnoticed. With a worker present, ONLY the inactive skip can let the sweep proceed.
MOCK_PEER_STATE=inactive MOCK_PEER_JOB=1 run_case 0 50
assert_has "image prune -af --filter until=86400s" "J6: peer CONFIRMED inactive -> idle even with a stray worker match"

# The own-arm has TWO fail-safe modes and J5 only covers one: a read that FAILS. A read that succeeds
# but returns junk is caught further on by is_num, and nothing exercised it.
MOCK_MAINPID=notanumber run_case 0 50
assert_none "J8: non-numeric MainPID -> BUSY, no prune"
unset MOCK_MAINPID
unset MOCK_PEER_STATE MOCK_PEER_JOB MOCK_PEER_PROBE_RC MOCK_SYSTEMCTL_FAIL

echo "[K] full image prune — gated on DISK%, not on a byte cap"
# Images became the dominant consumer (2026-08-09, ci-runner-4: 35 GB images vs 28.6 GB cache) and the
# `until=` windows cannot retire them: most images are younger than the pressure window because CI
# pulls and builds faster than any age rule. So an idle sweep escalates to `image prune -af`, under the
# same guards as 3b: never mid-job, and the busy signal re-read immediately before.
#
# The TRIGGER changed from `images > IMAGE_MAX_BYTES` to `disk% >= FULL_PRUNE_PCT`. An absolute byte
# cap and the filesystem are independent quantities, so "every cap satisfied" and "disk at 88%" were
# simultaneously true and the sweep had NO lever between PRESSURE_PCT and CRITICAL_PCT. Measured on
# ci-runner-4 2026-08-11: the cap fired correctly at 04:22 (disk 75% -> 63%), then by 10:26 images were
# back under cap and the sweep logged `done: disk 88% -> 88%` with nothing left to pull. K1/K2 below
# encode exactly that: the byte size no longer decides, the disk does.
# NOTE these assert on `image prune -af` with NO `--filter`, which is what distinguishes this from the
# routine windowed prune that every sweep already runs.
capfired() { grep -qE 'docker image prune -af *$' "$CALLS"; }

MOCK_IMAGES=26 run_case 0 90
if capfired; then ok "K1: disk >= FULL_PRUNE_PCT + idle -> full image prune"; else bad "K1: disk >= FULL_PRUNE_PCT + idle -> full image prune"; fi

# The case the byte cap could not express: images comfortably UNDER any cap while the disk is full.
# This is the exact steady state the runners were stuck in, and it must now prune.
MOCK_IMAGES=12 run_case 0 90
if capfired; then ok "K2: disk full but images under the old cap -> STILL prunes (the byte cap could not)"; else bad "K2: disk full but images under the old cap -> STILL prunes (the byte cap could not)"; fi

MOCK_IMAGES=26 run_case 0 85
if capfired; then bad "K3: below FULL_PRUNE_PCT must NOT full-prune images"; else ok "K3: pressure but below FULL_PRUNE_PCT -> no full image prune"; fi

# K4 as first written was VACUOUS — mutation proved it: dropping the midjob gate left the suite green,
# because with the runner busy for the whole sweep the BUSY RE-CHECK answers first and the gate it
# names is never reached. Same shadowing that made H4/J6 decorative. MOCK_BUSY_UNTIL makes the runner
# busy at the gate (so the sweep takes the critical path with midjob=1) and IDLE by the time the cap
# is reached, so the re-check would say "go" and only the midjob gate can stop the prune.
MOCK_IMAGES=26 MOCK_BUSY_UNTIL=container run_case 1 95
if capfired; then bad "K4: the midjob gate must block the full image prune even when the re-check reads idle"; else ok "K4: midjob gate blocks the full image prune when the re-check reads idle"; fi
unset MOCK_BUSY_UNTIL

# K5 INVERTED DELIBERATELY. It used to assert that an unreadable size fails CLOSED (no prune). That
# was correct when the size WAS the trigger, but it is exactly how the lever went silent for weeks:
# `docker system df` on the containerd snapshotter walks the whole snapshot tree, is slow enough to hit
# its own timeout, and an empty read then skipped the prune with no log and no metric — indistinguish-
# able from "nothing to do". The size is now used for the LOG LINE only, so an unreadable one must NOT
# be able to suppress reclamation on a disk that df(1) says is full.
MOCK_IMAGES=0 run_case 0 90
if capfired; then ok "K5: unreadable image size still prunes (df decides, not docker system df)"; else bad "K5: unreadable image size still prunes (df decides, not docker system df)"; fi

MOCK_IMAGES=26 MOCK_BUSY_FROM=container run_case 0 90
if capfired; then bad "K6: a job arriving mid-sweep must abort the full image prune"; else ok "K6: job arriving mid-sweep aborts the full image prune"; fi
unset MOCK_IMAGES

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

echo "[L] every --filter until= window is clamped to at least the job timeout"
# Under the containerd image store `until=` compares the LOCAL record time (when THIS host pulled or
# tagged the image), and a host-mode job holds no container, so filterImagesUsedByContainers() protects
# nothing. With a 3h job timeout, ANY window under 3h can delete an image out from under a running job.
# CRITICAL_UNTIL shipped at 1h and section 4 applies it on the MID-JOB path, so this was the single
# most dangerous window in the script. The clamp is what makes the old comment's promise ("still
# protects a running build") actually true. L1 is the mutation guard: remove clamp_win from the
# critical call site and this goes red, because the raw 5m would reach docker.
CRITUNTIL=5m run_case 1 95
assert_has "image prune -af --filter until=14400s" "L1: a dangerously short critical override is clamped UP to the floor"
if grep -q 'until=5m' "$CALLS"; then bad "L2: the raw sub-timeout window must never reach docker"; else ok "L2: raw sub-timeout window never reaches docker"; fi
unset CRITUNTIL

# An unparseable window must not silently pass through as-is either: it becomes the floor.
CRITUNTIL=garbage run_case 1 95
assert_has "image prune -af --filter until=14400s" "L3: an unparseable window falls back to the floor, not to no filter"
unset CRITUNTIL

# L4/L5 (codex cross-review): the FLOOR is an override too, so it must be validated before being used
# as one. NOTE an EMPTY override is NOT the reachable case — `${GITEA_CLEANUP_MIN_UNTIL_SEC:-14400}`
# already substitutes on empty, so it never reaches clamp_win. Asserting on empty passed with the
# guard deliberately removed, i.e. it was a vacuous test. The two reachable shapes are non-empty:
#   abc -> `--filter until=abcs`, which docker rejects; every prune call site swallows its own errors
#          with `|| true`, so one typo would silently disable ALL window prunes while the sweep still
#          logged success — the same silent-no-op class this whole PR exists to remove.
#   0   -> `--filter until=0s` (delete EVERYTHING) AND it disables the clamp itself, so a 1h critical
#          window passes through untouched onto the MID-JOB path. That is the dangerous one.
MINUNTIL=abc CRITUNTIL=garbage run_case 1 95
assert_has "image prune -af --filter until=14400s" "L4: a non-numeric MIN_UNTIL_SEC falls back to the built-in floor"
if grep -qE 'until=abcs' "$CALLS"; then bad "L4b: a malformed floor must never reach docker"; else ok "L4b: malformed floor never reaches docker"; fi
unset MINUNTIL CRITUNTIL

MINUNTIL=0 CRITUNTIL=1h run_case 1 95
assert_has "image prune -af --filter until=14400s" "L5: a zero floor cannot disable the clamp (1h must still be raised)"
if grep -qE 'until=(0s|3600s)' "$CALLS"; then bad "L5b: a zero floor must not let a sub-timeout window through mid-job"; else ok "L5b: zero floor cannot leak a sub-timeout window"; fi
unset MINUNTIL CRITUNTIL

echo "[P] the beacon must distinguish the STARVATION exit from the HEALTHY one"
# CIRunnerCleanupStarved alerts on gitea_runner_cleanup_pressure_defer == 1. That metric exists because
# busy_skip cannot carry the meaning: it is 1 on BOTH early exits, and 88% of its writes are the
# healthy one (952 of 1087 over 7d/5 runners), so a rule on it paged four healthy runners. Two attempts
# to recover the distinction in PromQL from a disk THRESHOLD both failed — instantaneous could not
# sustain `for: 3h` against 23-point hourly swings, and a 3h average lagged 5.5-6h. The script knows
# which branch it took, so it records it, and these cases are what make that trustworthy.
# Nothing else in this suite runs with the beacon enabled, so without them the field could be dropped,
# inverted, or written on the wrong branch and every other check would stay green.
BEACONDIR="$WORK/textfile"; mkdir -p "$BEACONDIR"
beacon_field() { # <field> -> value written by the last run, or the empty string
  sed -n "s/^gitea_runner_cleanup_$1 \(.*\)$/\1/p" "$BEACONDIR/gitea_runner_cleanup.prom" 2>/dev/null
}
run_beacon() { rm -f "$BEACONDIR/gitea_runner_cleanup.prom"; BEACON=1 TEXTDIR="$BEACONDIR" run_case "$@"; }

# P1: busy + BELOW pressure -> the healthy skip. busy_skip 1 (it did nothing) but NOT a starvation.
run_beacon 1 50
[ "$(beacon_field busy_skip)" = 1 ] && ok "P1: healthy skip still sets busy_skip=1" || bad "P1: healthy skip sets busy_skip=1 (got '$(beacon_field busy_skip)')"
[ "$(beacon_field pressure_defer)" = 0 ] && ok "P1: healthy skip sets pressure_defer=0 (must NOT alert)" || bad "P1: healthy skip must set pressure_defer=0 (got '$(beacon_field pressure_defer)')"

# P2: busy + AT pressure + no gap ever appears -> the deferral branch. This is the one worth paging on.
IDLEWAIT=2 run_beacon 1 85
[ "$(beacon_field pressure_defer)" = 1 ] && ok "P2: pressure deferral sets pressure_defer=1" || bad "P2: pressure deferral must set pressure_defer=1 (got '$(beacon_field pressure_defer)')"

# P3: a sweep that actually RAN is not starved, however it got there.
run_beacon 0 50
[ "$(beacon_field pressure_defer)" = 0 ] && ok "P3: a completed sweep sets pressure_defer=0" || bad "P3: completed sweep must set pressure_defer=0 (got '$(beacon_field pressure_defer)')"

# P4: the mid-job critical path SWEPT — riskily, but it swept. midjob_prune records that separately;
# starved must mean "could not sweep", never "swept in a way we would rather it had not".
run_beacon 1 95
[ "$(beacon_field pressure_defer)" = 0 ] && ok "P4: the mid-job critical sweep is not 'starved'" || bad "P4: mid-job sweep must set pressure_defer=0 (got '$(beacon_field pressure_defer)')"
[ "$(beacon_field midjob_prune)" = 1 ] && ok "P4: ...and is still recorded as midjob_prune=1" || bad "P4: mid-job sweep must set midjob_prune=1 (got '$(beacon_field midjob_prune)')"

# P5 (codex review of #324): PRESSURE CLEARED DURING THE WAIT. The gate is entered on `before` >=
# PRESSURE_PCT, but the deferral branch acts on a disk re-read up to IDLE_WAIT_SEC (600s) later, and
# the reclaim timer and finishing jobs both free space in that window. The first version of this fix
# wrote pressure_defer=1 for ANY non-critical value, so every "the disk recovered while we waited"
# exit would have paged — reintroducing the false positive the whole metric exists to remove, on a
# path no fixed-disk test case can reach.
MOCK_PCT_AFTER=60 IDLEWAIT=2 run_beacon 1 85
[ "$(beacon_field pressure_defer)" = 0 ] && ok "P5: pressure cleared during the wait -> NOT starved" || bad "P5: pressure cleared during the wait must set pressure_defer=0 (got '$(beacon_field pressure_defer)')"
[ "$(beacon_field busy_skip)" = 1 ] && ok "P5: ...but it is still a busy skip" || bad "P5: pressure-cleared exit must still set busy_skip=1 (got '$(beacon_field busy_skip)')"
unset MOCK_PCT_AFTER IDLEWAIT

echo "[N] dangling BuildKit record heal (section 0)"
# 2026-10-07, ci-runner-1: a build lost its client mid-commit and left an immutable cache record Y
# whose mutable ref X has no snapshot. Every later build reusing Y failed at finalize, whatever the PR,
# until a heal by hand (image prune -af, restart dockerd, builder prune -af). Section 0 runs that heal
# itself, but only when the journal names the pair AND BuildKit still lists Y, only in a confirmed job
# gap, only with the attempt persisted and the runner seen stopped, and it gets the runner back on
# every path. Every NEGATIVE case below also asserts positive evidence that the run reached its branch
# (the sweep ran, or the heal beacon was written), so a script that crashed early cannot pass it.
JRNL="$WORK/docker.journal"; DU="$WORK/builder-du.txt"
X=96jcs8mnru1kpem80tk95udfv; Y=5n25clekmoo4u81kp8c9cu4a8
# Verbatim from ci-runner-1's journal (-o cat), 2026-10-07 21:30:17.
FINALIZE_LINE="time=\"2026-10-07T21:30:17.525922783+03:00\" level=error msg=/moby.buildkit.v1.Control/Solve error=\"rpc error: code = Unknown desc = failed to commit $X to $Y during finalize: failed to stat active key during commit: snapshot $X does not exist: not found\" spanID=cafbe78542e860d0"
heal_field() { sed -n "s/^gitea_runner_buildkit_$1 \(.*\)$/\1/p" "$BEACONDIR/gitea_runner_buildkit_heal.prom" 2>/dev/null; }
state_val() { sed -n "s/^$1=\(.*\)$/\1/p" "$WORK/heal.state" 2>/dev/null | tail -1; }
heal_ran() { calls_has "systemctl restart docker.service"; }
swept() { calls_has "image prune -af --filter until="; }
du_lists() { # <record id>... : a `docker builder du --verbose` listing of those records
  local id
  for id in "$@"; do printf 'ID:           %s\nMutable:      false\nDescription:  [builder 17/20] COPY src/ src/\n\n' "$id"; done > "$DU"
}
dangle() { # the journal names (X, Y) twice, BuildKit lists Y, no earlier heal
  printf '%s\n%s\n' "$FINALIZE_LINE" "$FINALIZE_LINE" > "$JRNL"
  du_lists oldrecord111 "$Y"
  rm -f "$WORK/heal.state" "$BEACONDIR/gitea_runner_buildkit_heal.prom"
}
in_order() { # <label> <call>... : every call present, each strictly after the one before
  local label="$1" prev=0 n c; shift
  for c in "$@"; do
    n="$(grep -nF -- "$c" "$CALLS" | head -1 | cut -d: -f1)"
    if [ -z "$n" ] || [ "$n" -le "$prev" ]; then bad "$label (missing or out of order: $c)"; return; fi
    prev="$n"
  done
  ok "$label"
}
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
MOCK_JOURNAL="$JRNL"; MOCK_DU="$DU"

dangle; MOCK_DU_CLEARS=1 run_beacon 0 50
in_order "N1: heal order: stop runner, image prune, restart docker, builder prune, start runner" \
  "systemctl stop gitea-act-runner.service" "docker image prune -af" "systemctl restart docker.service" \
  "docker builder prune -af" "systemctl start --no-block gitea-act-runner.service"
check "N1: the sweep is skipped after a heal" '! swept'
check "N1: the GitHub agent's unit is left alone" '! grep -q actions.runner "$CALLS"'
check "N1: the runner is running again" '[ "$(cat "$WORK/runner.state")" = active ]'
check "N1: beacon dangling_record=0, runner_held=0, heals_total=1" \
  '[ "$(heal_field dangling_record)" = 0 ] && [ "$(heal_field runner_held)" = 0 ] && [ "$(heal_field heals_total)" = 1 ]'
check "N1: the state file records the attempt and the released runner" \
  '[ "$(state_val heals_total)" = 1 ] && [ "$(state_val runner_held)" = 0 ] && [ -n "$(state_val last_heal)" ]'
check "N1: the cleanup beacon is still written (healthy field set)" \
  '[ "$(beacon_field busy_skip)" = 0 ] && [ "$(beacon_field pressure_defer)" = 0 ]'

# The confirm step is what keeps a journal line about an ALREADY-healed pair from re-healing the runner
# every tick for the whole lookback.
dangle; du_lists oldrecord111; run_beacon 0 50
check "N2: a pair whose Y BuildKit no longer lists is not healed" '! heal_ran'
check "N2: ...the routine sweep runs as usual" swept
check "N2: ...and the beacon says clean" '[ "$(heal_field dangling_record)" = 0 ]'

dangle; IDLEWAIT=2 run_beacon 1 50
check "N3: no heal and no runner stop while a job runs" '! heal_ran && ! calls_has "systemctl stop"'
check "N3: beacon dangling_record=1 while the heal waits for a gap" '[ "$(heal_field dangling_record)" = 1 ]'
check "N3: ...and the busy gate still ran (busy_skip=1)" '[ "$(beacon_field busy_skip)" = 1 ]'
check "N3: no attempt is recorded" '[ -z "$(state_val heals_total)" ]'

dangle; MOCK_BUSY_DROP_AFTER=1 IDLEWAIT=8 MOCK_DU_CLEARS=1 run_beacon 1 50
check "N3b: a job gap during the wait -> heal runs" heal_ran
unset MOCK_BUSY_DROP_AFTER

dangle; printf 'last_heal=%s\nheals_total=1\n' "$(( $(date +%s) - 60 ))" > "$WORK/heal.state"; run_beacon 0 50
check "N4: cooldown: no second heal within the window" '! heal_ran'
check "N4: ...the record is reported (dangling_record=1) and heals_total carries over" \
  '[ "$(heal_field dangling_record)" = 1 ] && [ "$(heal_field heals_total)" = 1 ]'
check "N4: ...and the sweep runs" swept

dangle; printf 'last_heal=%s\nheals_total=1\n' "$(( $(date +%s) - 21601 ))" > "$WORK/heal.state"; MOCK_DU_CLEARS=1 run_beacon 0 50
check "N5: once the cooldown has passed, a heal runs again" heal_ran
check "N5: heals_total=2" '[ "$(heal_field heals_total)" = 2 ]'

# A record that is still listed after a FULL heal is not retried: the same steps would fail the same
# way, and each attempt costs the runner its warm images and cache.
dangle; printf 'last_heal=%s\nheals_total=1\nfailed_record=%s\n' "$(( $(date +%s) - 99999 ))" "$Y" > "$WORK/heal.state"; run_beacon 0 50
check "N5b: a record an earlier heal left in place is not healed again, cooldown or not" '! heal_ran'
check "N5b: ...it stays on the alert (dangling_record=1) and the sweep runs" '[ "$(heal_field dangling_record)" = 1 ] && swept'

# Read failures are UNKNOWN: no heal, and the beacon keeps its last value instead of reading "clean".
dangle; printf 'dangling=1\n' > "$WORK/heal.state"; MOCK_DU_FAIL=1 run_beacon 0 50
check "N6: BuildKit's record list unreadable -> no heal" '! heal_ran'
check "N6: ...the last known dangling_record=1 is kept, not cleared" '[ "$(heal_field dangling_record)" = 1 ]'
check "N6: ...and the sweep runs" swept

dangle; printf 'dangling=1\n' > "$WORK/heal.state"; MOCK_JOURNAL_FAIL=1 run_beacon 0 50
check "N6b: docker journal unreadable -> no heal, last dangling_record=1 kept, sweep runs" \
  '! heal_ran && [ "$(heal_field dangling_record)" = 1 ] && swept'

# Disabling must also retract an earlier positive beacon, or node_exporter keeps serving the stale 1.
dangle; printf 'gitea_runner_buildkit_dangling_record 1\n' > "$BEACONDIR/gitea_runner_buildkit_heal.prom"; HEALON=0 run_beacon 0 50
check "N7: HEAL_DANGLING=0 -> no heal" '! heal_ran'
check "N7: ...an existing heal beacon is removed" '[ ! -e "$BEACONDIR/gitea_runner_buildkit_heal.prom" ]'
check "N7: ...and the sweep runs" swept

dangle; MOCK_DOCKER_RESTART_RC=1 run_beacon 0 50
check "N8: a failed docker restart still gets the runner running again" \
  'calls_has "systemctl start --no-block gitea-act-runner.service" && [ "$(cat "$WORK/runner.state")" = active ]'
# The step believed to release the record never happened, so this is NOT a record that survived a
# heal: no builder prune, no failed mark, and the cooldown (not a permanent block) governs the retry.
check "N8: ...no builder prune and NOT marked failed (retried after the cooldown)" \
  '! grep -qE "docker builder prune -af *$" "$CALLS" && [ -z "$(state_val "failed_$Y")" ]'
check "N8: the record stays reported and the attempt counts" \
  '[ "$(heal_field dangling_record)" = 1 ] && [ "$(heal_field heals_total)" = 1 ] && [ -n "$(state_val last_heal)" ]'
check "N8: runner_held is released once the runner is seen active" '[ "$(heal_field runner_held)" = 0 ]'

# A FULL heal (restart succeeded) that leaves the record listed is the case the failed mark is for.
dangle; run_beacon 0 50
check "N8b: a record still listed after the full heal is marked failed (never retried)" \
  'heal_ran && grep -qE "docker builder prune -af *$" "$CALLS" && [ "$(state_val "failed_$Y")" = 1 ] && [ "$(heal_field dangling_record)" = 1 ]'

# A runner someone else stopped stays stopped: the heal only restarts what it stopped itself.
dangle; MOCK_RUNNER_STATE=inactive MOCK_DU_CLEARS=1 run_beacon 0 50
check "N9: the heal still runs with the runner already stopped" heal_ran
check "N9: ...and an already-stopped runner is neither stopped nor started" \
  '! calls_has "systemctl start" && ! calls_has "systemctl stop gitea" && [ "$(cat "$WORK/runner.state")" = inactive ]'

# A runner in transition gives no admission guarantee either way: wait for the next tick.
dangle; MOCK_RUNNER_STATE=activating run_beacon 0 50
check "N9b: runner 'activating' -> no stop, no heal" '! heal_ran && ! calls_has "systemctl stop"'
check "N9b: ...no attempt recorded, record reported, sweep runs" \
  '[ -z "$(state_val heals_total)" ] && [ "$(heal_field dangling_record)" = 1 ] && swept'

# SIGTERM at TimeoutStartSec: the trap queues the start; the persisted marker covers the rest.
dangle; MOCK_HANG_HEAL=1 TERM_AT_HANG=1 run_beacon 0 50
check "N10: SIGTERM mid-heal still queues the runner's start (trap)" 'calls_has "systemctl start --no-block gitea-act-runner.service"'
check "N10: ...the heal stops there" '! heal_ran'
check "N10: ...and the attempt was already persisted (cooldown holds), runner still marked held" \
  '[ "$(state_val heals_total)" = 1 ] && [ "$(state_val runner_held)" = 1 ]'
KEEP_RUNNER_STATE=1 run_beacon 0 50
check "N10: the next tick releases runner_held and does not heal again (cooldown)" \
  '[ "$(state_val runner_held)" = 0 ] && ! heal_ran'

# The stop is the admission barrier. Not seen stopped = nothing destructive runs.
dangle; MOCK_STOP_FAIL=1 run_beacon 0 50
check "N11: runner still active after the stop -> no image prune, no docker restart" \
  '! heal_ran && ! grep -qE "docker image prune -af *$" "$CALLS"'
check "N11: ...the record stays reported" '[ "$(heal_field dangling_record)" = 1 ]'
check "N11: ...the cooldown is kept (an unstoppable runner is not drained every tick) and runner_held released" \
  '[ -n "$(state_val last_heal)" ] && [ "$(state_val runner_held)" = 0 ]'

# SIGKILL skips every trap: a later invocation must start the runner from the persisted marker alone.
# --recover-runner is what the unit's ExecStopPost runs; it must touch nothing else.
rm -f "$WORK/heal.state" "$JRNL"; printf 'runner_held=1\n' > "$WORK/heal.state"
MOCK_RUNNER_STATE=inactive SCRIPT_ARGS=--recover-runner run_case 0 50
check "N12: --recover-runner starts a runner a killed heal left stopped" 'calls_has "systemctl start --no-block gitea-act-runner.service"'
check "N12: ...and does nothing else (no prune, no sweep)" '! grep -q prune "$CALLS"'
KEEP_RUNNER_STATE=1 run_beacon 0 50
check "N12: the next tick sees the runner active and releases runner_held" \
  '[ "$(state_val runner_held)" = 0 ] && [ "$(heal_field runner_held)" = 0 ] && swept'

# No persisted attempt = no cooldown and no recovery marker, so the heal must not start at all.
dangle; : > "$WORK/notadir"; HEALSTATE="$WORK/notadir/heal.state" run_beacon 0 50
check "N13: state file unwritable -> no runner stop, no heal" '! heal_ran && ! calls_has "systemctl stop"'
check "N13: ...and the sweep runs" swept

# Neighbouring failures that are NOT this one. The pull-lease flake and a plain cancelled finalize both
# name ids, and so does a finalize whose missing snapshot is not the ref being committed; BuildKit lists
# every Y involved, so only the parser can keep them out.
dangle
{
  echo 'level=error msg="failed to commit snapshot extract-844538062-ocr8 sha256:aa: NotFound: lease does not exist: not found"'
  echo 'level=error msg=/moby.buildkit.v1.Control/Solve error="rpc error: code = Canceled desc = failed to commit lxnm33ay9barlzva4hvu72s2b to 92epe5m9nwvsasko5mz3tn8yy during finalize: context canceled"'
  echo "level=error msg=/moby.buildkit.v1.Control/Solve error=\"failed to commit aaaa1111 to $Y during finalize: failed to stat active key during commit: snapshot bbbb2222 does not exist: not found\""
} > "$JRNL"
du_lists 92epe5m9nwvsasko5mz3tn8yy "$Y"
run_beacon 0 50
check "N14: other 'not found' / finalize errors do not match the signature" '! heal_ran'
check "N14: ...the beacon says clean and the sweep runs" '[ "$(heal_field dangling_record)" = 0 ] && swept'

# A production-size listing with Y FIRST (ci-runner-9 lists 865 records). `printf | grep -q` under
# pipefail could SIGPIPE the writer here and read a listed record as absent (codex round 2).
dangle
{ printf 'ID:           %s\nMutable:      false\n\n' "$Y"; for i in $(seq 1 4000); do printf 'ID:           filler%06d\nMutable:      false\nDescription:  [builder 1/9] RUN true\n\n' "$i"; done; } > "$DU"
MOCK_DU_CLEARS=1 run_beacon 0 50
check "N15: Y at the top of a large record listing is still found -> heal runs" heal_ran

# An earlier heal's recovery still pending: a new heal would read the stopped runner as an operator's
# and overwrite the marker that keeps it being restarted.
dangle; printf 'last_heal=%s\nheals_total=1\nrunner_held=1\n' "$(( $(date +%s) - 99999 ))" > "$WORK/heal.state"
MOCK_RUNNER_STATE=failed MOCK_START_FAIL=1 run_beacon 0 50
check "N16: a pending runner recovery blocks a new heal, cooldown expired or not" '! heal_ran'
check "N16: ...the start is retried and the marker survives (runner_held=1 -> alert)" \
  'calls_has "systemctl start --no-block gitea-act-runner.service" && [ "$(state_val runner_held)" = 1 ] && [ "$(heal_field runner_held)" = 1 ]'

# The peer is re-read before each destructive step: the gap was confirmed before a stop that can drain
# for 10 minutes.
dangle; MOCK_PEER_BUSY_FROM=runnerstop run_beacon 0 50
check "N17: a peer job starting during the stop -> no image prune, no docker restart" \
  '! heal_ran && ! grep -qE "docker image prune -af *$" "$CALLS"'
check "N17: ...and the runner is started again" '[ "$(cat "$WORK/runner.state")" = active ]'
check "N17: ...nothing destructive ran, so the cooldown is given back and runner_held released" \
  '[ "$(state_val last_heal)" = 0 ] && [ "$(state_val heals_total)" = 0 ] && [ "$(state_val runner_held)" = 0 ]'

dangle; MOCK_PEER_BUSY_FROM=image run_beacon 0 50
check "N18: a peer job starting during the image prune -> no docker restart" '! heal_ran && grep -qE "docker image prune -af *$" "$CALLS"'
check "N18: ...and the runner is started again" '[ "$(cat "$WORK/runner.state")" = active ]'
check "N18: ...the image prune ran, so the cooldown is kept; runner_held released" \
  '[ -n "$(state_val last_heal)" ] && [ "$(state_val runner_held)" = 0 ]'

# state_set must not report success when the final rename fails (its temp file was complete).
dangle; MOCK_MV_FAIL=1 run_beacon 0 50
check "N19: the state file cannot be replaced -> no runner stop, no heal" '! heal_ran && ! calls_has "systemctl stop"'
check "N19: ...and the sweep runs" swept

dangle; MOCK_PEER_BUSY_FROM=dockerrestart run_beacon 0 50
check "N20: a peer job starting during the docker restart -> no builder prune" \
  'heal_ran && ! grep -qE "docker builder prune -af *$" "$CALLS"'
check "N20: ...and the runner is started again" '[ "$(cat "$WORK/runner.state")" = active ]'

dangle; printf 'dangling=1\n' > "$WORK/heal.state"; MOCK_SORT_FAIL=1 run_beacon 0 50
check "N21: a failed journal parse is unknown: no heal, last dangling_record=1 kept, sweep runs" \
  '! heal_ran && [ "$(heal_field dangling_record)" = 1 ] && swept'

# A failed record must not shadow a second live record behind it (reviewer-claude on #1142). The pairs
# sort by Y, so the failed Y below comes FIRST; only skipping it reaches the healable Y2.
X2=xx9missingsnapshot000000; Y2=zz9healablerecord0000000
two_dangle() { # the journal names (X, Y) and (X2, Y2); BuildKit lists both
  dangle
  printf '%s\n' "$FINALIZE_LINE" "level=error msg=/moby.buildkit.v1.Control/Solve error=\"failed to commit $X2 to $Y2 during finalize: failed to stat active key during commit: snapshot $X2 does not exist: not found\"" > "$JRNL"
  du_lists "$Y" "$Y2"
}
two_dangle; printf 'last_heal=%s\nheals_total=1\nfailed_%s=1\n' "$(( $(date +%s) - 99999 ))" "$Y" > "$WORK/heal.state"
MOCK_DU_CLEARS=1 run_beacon 0 50
healed_record() { grep -q "heal: dangling BuildKit record $1 (snapshot" "$WORK/logger.log"; }
check "N22: a failed record listed first does not stop the heal of another live record" \
  'heal_ran && healed_record "$Y2" && ! healed_record "$Y"'
check "N22: ...the failed record keeps the alert up for this tick and stays marked" \
  '[ "$(heal_field dangling_record)" = 1 ] && [ "$(state_val "failed_$Y")" = 1 ] && [ "$(heal_field heals_total)" = 2 ]'

# The first release kept ONE failed record under the key failed_record; it is still honoured.
two_dangle; printf 'last_heal=%s\nheals_total=1\nfailed_record=%s\n' "$(( $(date +%s) - 99999 ))" "$Y" > "$WORK/heal.state"
MOCK_DU_CLEARS=1 run_beacon 0 50
check "N22b: a legacy failed_record is still skipped, and the other record is healed" \
  'healed_record "$Y2" && ! healed_record "$Y"'

# Failed marks are a SET: a second record whose heal fails must not un-fail the first. With one slot
# the two would take turns being retried, every cooldown, forever.
two_dangle; printf 'last_heal=%s\nheals_total=2\nfailed_%s=1\nfailed_%s=1\n' "$(( $(date +%s) - 99999 ))" "$Y" "$Y2" > "$WORK/heal.state"
run_beacon 0 50
check "N22c: every record a full heal left in place is skipped -> no heal, alert up, sweep runs" \
  '! heal_ran && [ "$(heal_field dangling_record)" = 1 ] && swept'
two_dangle; printf 'last_heal=%s\nheals_total=1\nfailed_%s=1\n' "$(( $(date +%s) - 99999 ))" "$Y" > "$WORK/heal.state"
run_beacon 0 50
check "N22d: a second record whose full heal fails is marked too, and the first stays marked" \
  'heal_ran && [ "$(state_val "failed_$Y")" = 1 ] && [ "$(state_val "failed_$Y2")" = 1 ]'
unset MOCK_JOURNAL MOCK_DU

echo "[Q] the gap wait is ONE wall-clock budget per run"
# The busy-gate loop counted IDLE_CONFIRM_POLLS-1 confirm sleeps on every BUSY iteration, although a
# busy read returns at once and sleeps nothing, so the 600s budget expired after ~200s of real waiting
# (codex, reviewing #1142). Both waits now go through wait_for_gap, whose deadline is set once per run:
# the busy gate gets its full budget, and a run whose heal already spent it does not wait a second time
# (the unit's TimeoutStartSec is sized for one wait). Timing bounds are generous on purpose.
t0=$(date +%s); IDLEWAIT=6 run_beacon 1 85; el=$(( $(date +%s) - t0 ))
check "Q1: busy at pressure -> the busy gate waits its whole budget on the wall clock (${el}s for 6s)" '[ "$el" -ge 5 ]'
check "Q1: ...then defers as before (pressure_defer=1)" '[ "$(beacon_field pressure_defer)" = 1 ]'

dangle; t0=$(date +%s); MOCK_JOURNAL="$JRNL" MOCK_DU="$DU" IDLEWAIT=5 run_beacon 1 85; el=$(( $(date +%s) - t0 ))
check "Q2: heal and busy gate share ONE budget: a busy run waits once, not twice (${el}s for 5s)" '[ "$el" -ge 4 ] && [ "$el" -lt 9 ]'
check "Q2: ...the record stays reported and the busy gate still defers" \
  '[ "$(heal_field dangling_record)" = 1 ] && [ "$(beacon_field pressure_defer)" = 1 ]'

echo "[M] the script's built-in defaults must equal the role defaults that actually ship"
# Every case above runs with GITEA_CLEANUP_ENV_FILE=/nonexistent, so the ${VAR:-default} fallbacks in
# the script ARE the values this suite exercises. In production the env file always exists, rendered
# from defaults/main.yml. When the two disagree the suite is green about values no runner uses and the
# deployed ones are covered by nothing — which is exactly what had happened: the script said 48h/80
# while every runner ran 24h/75, so [A]/[I1]/[J6] asserted a retention window that has never shipped.
#
# The knob -> var mapping is DERIVED FROM THE TEMPLATE rather than restated here, so a knob added later
# is checked automatically instead of needing a hand-kept list updated — a hand-kept list is precisely
# how the drift got in.
#
# NOTHING IS SKIPPED. The first draft skipped any template line carrying a Jinja filter, which quietly
# excluded BEACON (`| bool | ternary('1','0')`) from the only section that exists to catch drift — a
# false PASS in the guard against false PASSes (codex cross-review of #320). A filter this section
# cannot evaluate is now a FAILURE telling you to teach it that filter, not a silent pass.
# The env file's own path is checked separately after the loop: it is the one knob that cannot appear
# inside the file it names, so no template-derived mapping can ever see it.
DEFAULTS_YML="$HERE/../defaults/main.yml"
ENV_TMPL="$HERE/../templates/gitea-runner-cleanup.env.j2"

# Value of an ansible var from the flat defaults file. The parser understands bare scalars (with an
# optional trailing ` # comment`), single-quoted and double-quoted scalars. Only bare and double-quoted
# occur among the mapped vars today — the single-quote branch is there so adding one is not a trap, not
# because one exists. Double-quoted YAML turns `\\` into one literal backslash, which is why
# peer_job_process_re is written "Runner\\.Worker" and must compare as Runner\.Worker.
# A shape the parser does NOT model (a `|`/`>` block scalar, say) yields a value that cannot match the
# script fallback, so it surfaces as a loud mismatch rather than a false pass.
role_def_count() { grep -c -E "^$1:[[:space:]]" "$DEFAULTS_YML"; }
role_default() {
  local line v
  line="$(grep -m1 -E "^$1:[[:space:]]" "$DEFAULTS_YML")" || return 1
  v="${line#*:}"; v="${v#"${v%%[![:space:]]*}"}"
  case "$v" in
    '"'*) v="${v#\"}"; v="${v%%\"*}"; printf '%s' "$v" | sed 's/\\\\/\\/g' ;;
    "'"*) v="${v#\'}"; v="${v%%\'*}"; printf '%s' "$v" ;;
    *)    v="${v%%[[:space:]]#*}"; v="${v%"${v##*[![:space:]]}"}"; printf '%s' "$v" ;;
  esac
}
# The `${NAME:-default}` fallback the script uses for that env knob.
script_default() { sed -n "s/.*\${$1:-\([^}]*\)}.*/\1/p" "$SCRIPT" | head -1; }

# THE PARSER REFUSES TO GUESS. Every template shape below is either one [M] fully models, or a
# FAILURE naming what to teach it. That is not pedantry: the first two drafts each shipped a false
# PASS (a filtered line skipped outright; then a regex loose enough to accept `{{ a }}-{{ b }}` and
# silently compare against `b`). In a section whose entire job is catching drift, "parsed something
# plausible" is the dangerous outcome — a loud "I cannot evaluate this" costs one edit, a false PASS
# costs the next drift going unnoticed for as long as the last one did.
checked=0
while IFS= read -r ln; do
  ln="${ln#"${ln%%[![:space:]]*}"}" # leading whitespace is legal in a sourced env file; strip it so
  #                                   an indented knob cannot slip past BOTH the loop and tmpl_knobs
  case "$ln" in \#* | '') continue ;; esac
  env_name="$(printf '%s' "$ln" | sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p')"
  [ -n "$env_name" ] || continue
  checked=$((checked + 1))

  # The value must be EXACTLY one `{{ ... }}`, optionally wrapped in double quotes. Concatenation
  # (`{{ a }}-{{ b }}`), two expressions, or a literal are all rejected rather than approximated.
  raw="${ln#*=}"
  val="$raw"
  case "$val" in '"'*'"') val="${val#\"}"; val="${val%\"}" ;; esac
  inner=""
  case "$val" in
    '{{'*'}}') inner="${val#\{\{}"; inner="${inner%\}\}}" ;;
  esac
  case "$inner" in *'{{'* | *'}}'* | '') inner="" ;; esac
  if [ -z "$inner" ]; then
    bad "M: $env_name renders '$raw', which [M] does not model — it expects exactly one {{ ... }}"
    continue
  fi
  expr_norm="$(printf '%s' "$inner" | tr -s '[:space:]' ' ')"
  expr_norm="${expr_norm# }"; expr_norm="${expr_norm% }"

  # A knob the template sets but the script never reads is silently inert — worth catching on its own.
  if ! grep -qF "\${$env_name:-" "$SCRIPT"; then
    bad "M: $env_name is rendered into the env file but the script never reads it"
    continue
  fi

  # Split into the variable and, if present, the filter chain — which must be the ONE chain [M] can
  # evaluate, spelled out in full. Matching a bare `ternary(...)` anywhere in the line (the previous
  # attempt) would have accepted `foo | upper | ternary('1','0')` and evaluated it as if the filters
  # before the ternary did not exist.
  tern=""
  case "$expr_norm" in
    *'|'*)
      var_name="${expr_norm%% *}"
      tern="$(printf '%s' "$expr_norm" | sed -n "s/^[a-z0-9_]* | bool | ternary('\([^']*\)', *'\([^']*\)')$/\1 \2/p")"
      if [ -z "$tern" ]; then
        bad "M: $env_name goes through a filter chain [M] cannot evaluate ({{ $expr_norm }}) — teach it that chain rather than leaving the knob unchecked"
        continue
      fi ;;
    *) var_name="$expr_norm" ;;
  esac
  case "$var_name" in '' | *[!a-z0-9_]*) bad "M: $env_name -> '$var_name' is not a plain variable name"; continue ;; esac

  n="$(role_def_count "$var_name")"
  if [ "$n" -ne 1 ]; then
    # 0 = the template references a var with no default; >1 = a duplicate key, where YAML's last-wins
    # and this parser's first-wins disagree and the comparison would be against a value that never ships.
    bad "M: $env_name -> $var_name is defined $n times in defaults/main.yml (want exactly 1)"
    continue
  fi
  rv="$(role_default "$var_name")"

  if [ -n "$tern" ]; then
    # Ansible's `bool` filter accepts more spellings than the obvious two, and case-insensitively, so
    # compare lowercased. A value that is not clearly boolean is a FAILURE, not a silent fall to the
    # false branch — guessing here would invert the expected value and report a PASS for the wrong arm.
    case "$(printf '%s' "$rv" | tr 'A-Z' 'a-z')" in
      true | yes | 'on' | y | 1) rv="${tern%% *}" ;;
      false | no | 'off' | n | 0) rv="${tern##* }" ;;
      *) bad "M: $env_name -> $var_name is '$rv', which ansible's bool filter does not clearly resolve — [M] will not guess which ternary arm ships"; continue ;;
    esac
  fi

  sv="$(script_default "$env_name")"
  if [ "$rv" = "$sv" ]; then
    ok "M: $env_name default agrees with $var_name ($sv)"
  else
    bad "M: $env_name DRIFT — script default '$sv' but the role ships '$rv' (from $var_name)"
  fi
done < "$ENV_TMPL"

# Two independent floors, because "how many were checked" is the number this section can be silently
# wrong about. The equality catches a knob being SKIPPED (what the Jinja-filter `continue` used to do
# to BEACON); the >=15 catches the mapping matching nothing at all after a template reformat, which the
# equality alone would call a pass with both sides at zero — the same silent-all-clear shape as the
# byte caps #314 removed. Both tolerate leading whitespace, exactly as the loop above does, so the two
# counts cannot disagree about what a knob line is.
tmpl_knobs="$(grep -c -E '^[[:space:]]*[A-Z][A-Z0-9_]*=' "$ENV_TMPL")"
if [ "$checked" -eq "$tmpl_knobs" ] && [ "$checked" -ge 15 ]; then
  ok "M: every one of the $checked knobs the template renders was checked (none skipped)"
else
  bad "M: checked $checked of $tmpl_knobs knob(s) in $ENV_TMPL — one was skipped, or the mapping stopped matching"
fi

# The env file's own PATH is the one knob that cannot possibly appear in the env file, so the loop
# above is structurally blind to it — and it is the most consequential drift of all: point the role at
# a new path without updating the script's fallback and the script reads a file ansible no longer
# writes, silently reverting EVERY knob to its built-in default while every other check here still
# passes. Checked explicitly for that reason.
role_envfile="$(role_default gitea_runner_cleanup_env)"
script_envfile="$(script_default GITEA_CLEANUP_ENV_FILE)"
if [ -n "$role_envfile" ] && [ "$role_envfile" = "$script_envfile" ]; then
  ok "M: the env file's own path agrees ($script_envfile)"
else
  bad "M: env-file path DRIFT — the script reads '$script_envfile' but the role writes '$role_envfile'; every knob would silently fall back to its built-in default"
fi

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; else echo "$fails CHECK(S) FAILED"; exit 1; fi
