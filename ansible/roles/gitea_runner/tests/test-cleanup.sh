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
#                       which is what a systemctl/DBus failure looks like to the script
cat >"$BIN/systemctl" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *is-active*)
    st="\${MOCK_PEER_STATE:-active}"
    [ "\$st" = unreadable ] && exit 3
    echo "\$st"; [ "\$st" = active ] && exit 0 || exit 3 ;;
  *MainPID*)
    [ "\${MOCK_SYSTEMCTL_FAIL:-0}" = 1 ] && exit 1
    echo "\${MOCK_MAINPID:-4242}" ;;
  *) : ;;
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
    # rc 2 models a pgrep ERROR (bad regex, /proc failure) — distinct from rc 1 "no match".
    [ "\${MOCK_PEER_PROBE_RC:-}" = 2 ] && exit 2
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
  : > "$CALLS"; rm -f "$WORK/pgrep.count" "$WORK/df.count" "$WORK"/phase.*
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
    bash "$SCRIPT" >/dev/null 2>&1 || true
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
