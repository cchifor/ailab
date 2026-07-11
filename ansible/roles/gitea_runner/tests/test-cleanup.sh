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
  "system df") : ;;   # empty -> beacon sizes fall back to 0
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

# pgrep -P <pid> -> a child iff MOCK_BUSY=1 (script treats "daemon has children" as busy)
cat >"$BIN/pgrep" <<EOF
#!/usr/bin/env bash
if [ "\${MOCK_BUSY:-0}" = 1 ]; then echo 4243; fi
exit 0
EOF

for stub in logger; do printf '#!/usr/bin/env bash\nexit 0\n' >"$BIN/$stub"; done
chmod +x "$BIN"/*

run_case() { # <busy> <pct>
  : > "$CALLS"
  MOCK_BUSY="$1" MOCK_PCT="$2" PATH="$BIN:$PATH" \
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

echo "[B] BUSY + PRESSURE (85%) -> window-safe reclaim RUNS despite busy  (THE FIX)"
run_case 1 85
assert_has "image prune -af --filter until=6h"  "B: pressure image prune @6h while busy"
assert_has "builder prune -f --filter until=6h" "B: pressure builder prune @6h while busy"

echo "[C] BUSY + CRITICAL (95%) -> critical 1h-window reclaim runs"
run_case 1 95
assert_has "image prune -af --filter until=1h"  "C: critical image prune @1h while busy"

echo "[D] non-default buildx builders are pruned too (docker buildx prune --builder)"
run_case 0 85
assert_has "buildx prune -f --filter until=6h --builder builder-leaked" "D: per-builder buildx prune"

echo "[E] BUSY + low disk (50%) -> routine sweep SKIPPED (busy optimization preserved)"
run_case 1 50
assert_none "E: no prune while busy + below pressure"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; else echo "$fails CHECK(S) FAILED"; exit 1; fi
