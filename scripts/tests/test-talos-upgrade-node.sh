#!/usr/bin/env bash
# Tests for scripts/talos-upgrade-node.sh (plans/2026-10-08-talos-upgrade-program-plan.md, P0.6).
#
# The wrapper exists because talosctl's default --image drops the node's system extensions (the plain
# ghcr installer for 1.11-1.13 clients, an empty-schematic metal-installer for 1.14), and losing
# iscsi-tools breaks every Trident iSCSI PV. Invariants:
# - the image is always factory nocloud-installer/<the node's OWN live schematic>:<target>;
# - the client binary matches the node's running version;
# - adjacent-minor or patch steps only, never a downgrade;
# - nocloud only, the image must exist in the factory;
# - --dry-run never calls upgrade.
#
# Run: bash scripts/tests/test-talos-upgrade-node.sh   (Linux, WSL or Git Bash)
set -u

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/talos-upgrade-node.sh"
[ -f "$SCRIPT" ] || { echo "cannot find $SCRIPT"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }
eq()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "expected '$2', got '$3'"; }
has() { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "expected to contain '$2', got: $3" ;; esac; }
hasnt() { case "$3" in *"$2"*) bad "$1" "must NOT contain '$2', got: $3" ;; *) ok "$1" ;; esac; }

ROOT=$(mktemp -d); trap 'rm -rf "$ROOT"' EXIT
OUT="$ROOT/_out"; BIN="$ROOT/bin"; mkdir -p "$OUT" "$BIN"
touch "$OUT/talosconfig"

# One talosctl stub, installed under every versioned name. It reports which binary ran, and answers
# version/get from $CTL files.
cat > "$ROOT/talosctl-stub" <<'STUB'
#!/usr/bin/env bash
me="$(basename "$0")"
args="$*"
echo "$me $args" >> "$CTL/calls"
case "$args" in
  *" version"*) printf 'Server:\n\tNODE:        %s\n\tTag:         %s\n' x "$(cat "$CTL/server_version")" ;;
  *"get extensions"*) cat "$CTL/extensions.json" ;;
  *"get platformmetadata"*) printf '{"spec":{"platform":"%s"}}\n' "$(cat "$CTL/platform")" ;;
  *" upgrade "*) echo "upgrade started" ;;
esac
STUB
chmod +x "$ROOT/talosctl-stub"
for v in 1112 1116 11212 11311 1142; do cp "$ROOT/talosctl-stub" "$OUT/talosctl-$v.exe"; done

cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
# The factory manifest probe: 200 unless $CTL/image_missing exists.
echo "$*" >> "$CTL/curl_calls"
[ -f "$CTL/image_missing" ] && { printf '404'; exit 0; }
printf '200'
STUB
chmod +x "$BIN/curl"

SCHEM_A=53513e54bb39202f35694412577a6bc53d484744d35a126e5d42ef34785c0d83
SCHEM_K=0839748ecac818fa6db9bc8bad2cc054eed752a32cd83226e18aa382a3a384f7
new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL"
  echo "v1.11.2" > "$CTL/server_version"; echo "nocloud" > "$CTL/platform"
  printf '{"spec":{"metadata":{"name":"iscsi-tools","version":"v0.2.0"}}}\n{"spec":{"metadata":{"name":"schematic","version":"%s"}}}\n' "$SCHEM_A" > "$CTL/extensions.json"
}
run() { # run <node> <target> [--dry-run]
  PATH="$BIN:$PATH" TALOS_OUT="$OUT" bash "$SCRIPT" "$@" > "$CTL/out" 2>&1; echo $? > "$CTL/rc"
}
upgrade_line() { grep " upgrade " "$CTL/calls" 2>/dev/null | head -1; }

echo "== patch upgrade uses the node's own schematic and the matching client =="
new_case patch; run 192.168.0.47 v1.11.6
eq "exit 0" 0 "$(cat "$CTL/rc")"
has "runs the client matching the RUNNING version (1.11.2)" "talosctl-1112.exe" "$(upgrade_line)"
has "image is the factory nocloud installer for the live schematic" "--image factory.talos.dev/nocloud-installer/$SCHEM_A:v1.11.6" "$(upgrade_line)"
has "waits for completion" "--wait" "$(upgrade_line)"
has "targets only the given node" "-n 192.168.0.47" "$(upgrade_line)"

hasnt "a worker gets no -e override" " -e " "$(upgrade_line)"

echo "== a CP is upgraded THROUGH a survivor CP, never through itself =="
new_case cp; run 192.168.0.41 v1.11.6
has "cp1 upgraded via another CP endpoint" "-e 192.168.0.42" "$(upgrade_line)"
hasnt "never via its own address" "-e 192.168.0.41" "$(upgrade_line)"
has "and still only node cp1" "-n 192.168.0.41" "$(upgrade_line)"
new_case cp2; run 192.168.0.42 v1.11.6
has "cp2 upgraded via cp1" "-e 192.168.0.41" "$(upgrade_line)"

echo "== a kata/gvisor node keeps ITS schematic =="
new_case kata; printf '{"spec":{"metadata":{"name":"schematic","version":"%s"}}}\n' "$SCHEM_K" > "$CTL/extensions.json"; run 192.168.0.49 v1.11.6
has "agent-node-3 image carries the kata schematic" "nocloud-installer/$SCHEM_K:v1.11.6" "$(upgrade_line)"

echo "== adjacent minor allowed, client follows the running version =="
new_case minor; echo "v1.11.6" > "$CTL/server_version"; run 192.168.0.41 v1.12.12
eq "exit 0" 0 "$(cat "$CTL/rc")"
has "1.11.6 node is driven by talosctl-1116" "talosctl-1116.exe" "$(upgrade_line)"

echo "== refusals: skip a minor, downgrade, same version =="
new_case skip; run 192.168.0.41 v1.13.11
eq "skipping 1.12 -> exit 2" 2 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case down; echo "v1.12.12" > "$CTL/server_version"; run 192.168.0.41 v1.11.6
eq "downgrade -> exit 2" 2 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case same; run 192.168.0.41 v1.11.2
eq "same version -> exit 2" 2 "$(cat "$CTL/rc")"

echo "== refusals: no schematic, wrong platform, missing image, no matching client =="
new_case noschem; printf '{"spec":{"metadata":{"name":"iscsi-tools","version":"v0.2.0"}}}\n' > "$CTL/extensions.json"; run 192.168.0.41 v1.11.6
eq "no schematic -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case metal; echo "metal" > "$CTL/platform"; run 192.168.0.41 v1.11.6
eq "non-nocloud -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case noimg; touch "$CTL/image_missing"; run 192.168.0.41 v1.11.6
eq "image not in factory -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case noclient; echo "v1.10.9" > "$CTL/server_version"; run 192.168.0.41 v1.11.6
eq "no talosctl for the running version -> exit 1" 1 "$(cat "$CTL/rc")"

echo "== --dry-run prints the exact command and never upgrades =="
new_case dry; run 192.168.0.41 v1.11.6 --dry-run
eq "exit 0" 0 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
has "prints the command" "upgrade" "$(cat "$CTL/out")"
has "prints the image" "nocloud-installer/$SCHEM_A:v1.11.6" "$(cat "$CTL/out")"

echo "== bad arguments =="
new_case args; run 192.168.0.41
eq "missing target -> exit 2" 2 "$(cat "$CTL/rc")"
new_case args2; run not-an-ip v1.11.6
eq "bad node -> exit 2" 2 "$(cat "$CTL/rc")"

echo
echo "talos-upgrade-node: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
