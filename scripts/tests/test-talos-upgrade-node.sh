#!/usr/bin/env bash
# Tests for scripts/talos-upgrade-node.sh (plans/2026-10-08-talos-upgrade-program-plan.md, P0.6).
#
# The wrapper exists because talosctl's default --image drops the node's system extensions (the plain
# ghcr installer for 1.11-1.13 clients, an empty-schematic metal-installer for 1.14), and losing
# iscsi-tools breaks every Trident iSCSI PV. Invariants:
# - the image is always factory nocloud-installer/<the node's OWN live schematic>:<target>, and the
#   schematic is read structurally: exactly one `schematic` extension, 64 hex;
# - the client binary is the one whose `version --client` IS the node's running version (identity,
#   not file name);
# - adjacent-minor or patch steps only, never a downgrade;
# - nocloud only, the image must exist in the factory, and every probe's exit status counts;
# - a control plane (by its live machine type) is upgraded through a REACHABLE survivor CP;
# - --dry-run never calls upgrade, and the upgrade never passes --force.
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

# One talosctl stub, installed under every versioned name. It logs every call, answers
# `version --client` with ITS OWN tag ($CTL/tag.<binary>, exit status $CTL/client_rc.<binary>), fails for any -n/-e address listed in
# $CTL/unreachable, and fails a command whose keyword has a $CTL/fail.<keyword> file AFTER printing
# valid-looking output (a failed probe must not pass on its output alone).
cat > "$ROOT/talosctl-stub" <<'STUB'
#!/usr/bin/env bash
me="$(basename "$0")"
args="$*"
echo "$me $args" >> "$CTL/calls"
if [[ "$args" == *"version --client"* ]]; then
  printf 'Client:\n\tTag:         %s\n' "$(cat "$CTL/tag.$me" 2>/dev/null)"
  exit "$(cat "$CTL/client_rc.$me" 2>/dev/null || echo 0)"
fi
node=""; ep=""
while [ $# -gt 0 ]; do case "$1" in -n) node=$2; shift ;; -e) ep=$2; shift ;; esac; shift; done
for ip in $node $ep; do
  grep -qx "$ip" "$CTL/unreachable" 2>/dev/null && { echo "rpc error: code = Unavailable ($ip)" >&2; exit 1; }
done
case "$args" in
  *" upgrade "*) echo "upgrade started"; exit 0 ;;
  *"etcd members"*) cat "$CTL/etcd_members" ;;
  *"etcd status"*) cat "$CTL/etcd_status" ;;
  *" version"*) printf 'Client:\n\tTag:         x\nServer:\n\tNODE:        %s\n\tTag:         %s\n' "$node" "$(cat "$CTL/server_version")" ;;
  *"get extensions"*) cat "$CTL/extensions.json" ;;
  *"get platformmetadata"*) printf '{"node":"%s","spec":{"hostname":"talos","platform":"%s"}}\n' "$node" "$(cat "$CTL/platform")" ;;
  *"get machinetype"*)
    if [ -f "$CTL/machinetype" ]; then t=$(cat "$CTL/machinetype")
    else case "$node" in 192.168.0.41|192.168.0.42|192.168.0.43) t=controlplane ;; *) t=worker ;; esac; fi
    printf '{\n    "metadata": {"id": "machine-type"},\n    "node": "%s",\n    "spec": "%s"\n}\n' "$node" "$t" ;;
esac
for k in version extensions platformmetadata machinetype "etcd members" "etcd status"; do
  case "$args" in *"$k"*) [ -f "$CTL/fail.${k// /-}" ] && exit 1 ;; esac
done
exit 0
STUB
chmod +x "$ROOT/talosctl-stub"
CLIENTS="1112:v1.11.2 1116:v1.11.6 11212:v1.12.12 11311:v1.13.11 1142:v1.14.2"
for c in $CLIENTS; do cp "$ROOT/talosctl-stub" "$OUT/talosctl-${c%%:*}.exe"; done

# The factory manifest probe: prints $CTL/curl_code (default 200), exits $CTL/curl_rc (default 0).
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CTL/curl_calls"
printf '%s' "$(cat "$CTL/curl_code" 2>/dev/null || echo 200)"
exit "$(cat "$CTL/curl_rc" 2>/dev/null || echo 0)"
STUB
chmod +x "$BIN/curl"

# Healthy etcd, in talosctl's own table layout (copied from the live cluster): cp3 leads, one term,
# every member applied to the same index, no learners, no errors.
cat > "$ROOT/etcd_members" <<'TABLE'
NODE           ID                 HOSTNAME    PEER URLS                   CLIENT URLS                 LEARNER
192.168.0.42   1fd6da7fa19ceda6   talos-cp3   https://192.168.0.43:2380   https://192.168.0.43:2379   false
192.168.0.42   48262c4ce7932bdd   talos-cp2   https://192.168.0.42:2380   https://192.168.0.42:2379   false
192.168.0.42   c652f51572e4975e   talos-cp1   https://192.168.0.41:2380   https://192.168.0.41:2379   false
TABLE
cat > "$ROOT/etcd_status" <<'TABLE'
NODE           MEMBER             DB SIZE   IN USE           LEADER             RAFT INDEX   RAFT TERM   RAFT APPLIED INDEX   LEARNER   PROTOCOL   STORAGE   ERRORS
192.168.0.43   1fd6da7fa19ceda6   243 MB    90 MB (37.00%)   1fd6da7fa19ceda6   102561441    792         102561441            false     3.6.4      3.6.0
192.168.0.42   48262c4ce7932bdd   260 MB    90 MB (34.47%)   1fd6da7fa19ceda6   102561441    792         102561441            false     3.6.4      3.6.0
192.168.0.41   c652f51572e4975e   252 MB    90 MB (35.73%)   1fd6da7fa19ceda6   102561441    792         102561441            false     3.6.4      3.6.0
TABLE
SCHEM_A=53513e54bb39202f35694412577a6bc53d484744d35a126e5d42ef34785c0d83
SCHEM_K=0839748ecac818fa6db9bc8bad2cc054eed752a32cd83226e18aa382a3a384f7
new_case() {
  export CTL="$ROOT/ctl.$1"; rm -rf "$CTL"; mkdir -p "$CTL"
  echo "v1.11.2" > "$CTL/server_version"; echo "nocloud" > "$CTL/platform"
  for c in $CLIENTS; do echo "${c#*:}" > "$CTL/tag.talosctl-${c%%:*}.exe"; done
  cp "$ROOT/etcd_members" "$ROOT/etcd_status" "$CTL/"
  printf '{"spec":{"metadata":{"name":"iscsi-tools","version":"v0.2.0"}}}\n{"spec":{"metadata":{"name":"schematic","version":"%s"}}}\n' "$SCHEM_A" > "$CTL/extensions.json"
}
run() { # run <node> <target> [--dry-run]
  PATH="$BIN:$PATH" TALOS_OUT="${RUN_OUT:-$OUT}" bash "$SCRIPT" "$@" > "$CTL/out" 2>&1; echo $? > "$CTL/rc"
}
upgrade_line() { grep " upgrade " "$CTL/calls" 2>/dev/null | head -1; }

echo "== patch upgrade uses the node's own schematic and the matching client =="
new_case patch; run 192.168.0.47 v1.11.6
eq "exit 0" 0 "$(cat "$CTL/rc")"
has "runs the client matching the RUNNING version (1.11.2)" "talosctl-1112.exe" "$(upgrade_line)"
has "image is the factory nocloud installer for the live schematic" "--image factory.talos.dev/nocloud-installer/$SCHEM_A:v1.11.6" "$(upgrade_line)"
has "waits for completion" "--wait" "$(upgrade_line)"
has "targets only the given node" "-n 192.168.0.47" "$(upgrade_line)"
hasnt "never --force" "--force" "$(upgrade_line)"
hasnt "a worker gets no -e override" " -e " "$(upgrade_line)"
has "factory probe asks for this schematic and tag" "/v2/nocloud-installer/$SCHEM_A/manifests/v1.11.6" "$(cat "$CTL/curl_calls")"
has "factory probe is a HEAD" "-I" "$(cat "$CTL/curl_calls")"

echo "== a CP is upgraded THROUGH a reachable survivor CP, never through itself =="
new_case cp; run 192.168.0.41 v1.11.6
has "cp1 upgraded via another CP endpoint" "-e 192.168.0.42" "$(upgrade_line)"
hasnt "never via its own address" "-e 192.168.0.41" "$(upgrade_line)"
has "and still only node cp1" "-n 192.168.0.41" "$(upgrade_line)"
new_case cp2; run 192.168.0.42 v1.11.6
has "cp2 upgraded via cp1" "-e 192.168.0.41" "$(upgrade_line)"
new_case cpdown; echo 192.168.0.42 > "$CTL/unreachable"; run 192.168.0.41 v1.11.6
eq "cp1 with cp2 down -> exit 1 (rebooting cp1 would lose etcd quorum)" 1 "$(cat "$CTL/rc")"
eq "no upgrade call" "" "$(upgrade_line)"
has "names the absent control plane" "192.168.0.42" "$(grep -i 'did not answer' "$CTL/out")"
has "a healthy CP upgrade reports the etcd leader" "leader 1fd6da7fa19ceda6" "$(cat "$ROOT/ctl.cp/out")"
new_case nosurv; printf '192.168.0.42\n192.168.0.43\n' > "$CTL/unreachable"; run 192.168.0.41 v1.11.6
eq "no reachable survivor -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case onlyself; TALOS_CP_IPS=192.168.0.41 run 192.168.0.41 v1.11.6
eq "TALOS_CP_IPS = only the target -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case unlisted; echo controlplane > "$CTL/machinetype"; run 192.168.0.47 v1.11.6
eq "a live control plane missing from TALOS_CP_IPS -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case badlist; TALOS_CP_IPS="192.168.0.41 not-an-ip" run 192.168.0.41 v1.11.6
eq "a malformed TALOS_CP_IPS -> exit 2" 2 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"

echo "== a CP is upgraded only into a fully healthy 3/3 etcd it does not lead =="
new_case leader; run 192.168.0.43 v1.11.6
eq "the target (cp3) is the etcd leader -> exit 1 (forfeit first)" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
has "says so" "leader" "$(cat "$CTL/out")"
new_case etcderr; sed -i '/^192.168.0.43 /s/ *$/   etcdserver: request timed out/' "$CTL/etcd_status"; run 192.168.0.41 v1.11.6
eq "a member reporting ERRORS -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case learner; sed -i '/talos-cp3/s/false$/true/' "$CTL/etcd_members"; run 192.168.0.41 v1.11.6
eq "a learner member -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case lag; sed -i '/^192.168.0.42 /s/102561441            false/102551441            false/' "$CTL/etcd_status"; run 192.168.0.41 v1.11.6
eq "a member 10000 entries behind -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case term; sed -i '/^192.168.0.42 /s/ 792 / 793 /' "$CTL/etcd_status"; run 192.168.0.41 v1.11.6
eq "members disagreeing on the raft term -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case twoleaders; sed -i '/^192.168.0.42 /s/   1fd6da7fa19ceda6   102561441/   48262c4ce7932bdd   102561441/' "$CTL/etcd_status"; run 192.168.0.41 v1.11.6
eq "members disagreeing on the leader -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case twomembers; sed -i '/talos-cp3/d' "$CTL/etcd_members"; run 192.168.0.41 v1.11.6
eq "etcd membership != TALOS_CP_IPS -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case statusmissing; sed -i '/^192.168.0.43 /d' "$CTL/etcd_status"; run 192.168.0.41 v1.11.6
eq "a member missing from etcd status -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
for k in etcd-members etcd-status; do
  new_case "fail$k"; touch "$CTL/fail.$k"; run 192.168.0.41 v1.11.6
  eq "talosctl ${k/-/ } failing (with valid output) -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
done
new_case workeretcd; run 192.168.0.47 v1.11.6
eq "a worker upgrade -> exit 0" 0 "$(cat "$CTL/rc")"
eq "and never asks etcd" "" "$(grep ' etcd ' "$CTL/calls")"

echo "== a kata/gvisor node keeps ITS schematic; JSON is read structurally =="
new_case kata; printf '{"spec":{"metadata":{"name":"schematic","version":"%s"}}}\n' "$SCHEM_K" > "$CTL/extensions.json"; run 192.168.0.49 v1.11.6
has "agent-node-3 image carries the kata schematic" "nocloud-installer/$SCHEM_K:v1.11.6" "$(upgrade_line)"
new_case pretty; printf '{\n\t"node": "192.168.0.49",\n\t"spec": {\n\t\t"metadata": {\n\t\t\t"version": "%s",\n\t\t\t"author": "x",\n\t\t\t"name": "schematic"\n\t\t}\n\t}\n}\n{\n\t"spec": {"metadata": {"name": "kata-containers", "version": "3.20.0"}}\n}\n' "$SCHEM_K" > "$CTL/extensions.json"; run 192.168.0.49 v1.11.6
eq "pretty-printed, reordered, tab-indented JSON -> exit 0" 0 "$(cat "$CTL/rc")"
has "and the schematic is still found" "nocloud-installer/$SCHEM_K:v1.11.6" "$(upgrade_line)"
new_case twoschem; printf '{"spec":{"metadata":{"name":"schematic","version":"%s"}}}\n{"spec":{"metadata":{"name":"schematic","version":"%s"}}}\n' "$SCHEM_A" "$SCHEM_K" > "$CTL/extensions.json"; run 192.168.0.47 v1.11.6
eq "two schematic extensions -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case decoy; printf '{"spec":{"metadata":{"name":"iscsi-tools","version":"v0.2.0","description":"\\"name\\": \\"schematic\\", \\"version\\": \\"%s\\""}}}\n' "$SCHEM_A" > "$CTL/extensions.json"; run 192.168.0.47 v1.11.6
eq "a schematic-looking string inside another field -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"

echo "== adjacent minor allowed, client follows the running version =="
new_case minor; echo "v1.11.6" > "$CTL/server_version"; run 192.168.0.41 v1.12.12
eq "exit 0" 0 "$(cat "$CTL/rc")"
has "1.11.6 node is driven by talosctl-1116" "talosctl-1116.exe" "$(upgrade_line)"

echo "== the client is chosen by what it IS, not by its file name =="
new_case liar; echo "v1.11.6" > "$CTL/tag.talosctl-1112.exe"; run 192.168.0.47 v1.11.6
eq "no binary reports v1.11.2 -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
new_case renamed; cp "$ROOT/talosctl-stub" "$OUT/talosctl-custom.exe"; echo "v1.11.2" > "$CTL/tag.talosctl-custom.exe"; echo "v9.9.9" > "$CTL/tag.talosctl-1112.exe"; run 192.168.0.47 v1.11.6
eq "a differently named v1.11.2 client -> exit 0" 0 "$(cat "$CTL/rc")"
has "and it is the one that upgrades" "talosctl-custom.exe" "$(upgrade_line)"
rm -f "$OUT/talosctl-custom.exe"
new_case clientrc; echo 7 > "$CTL/client_rc.talosctl-1112.exe"; run 192.168.0.47 v1.11.6
eq "the only v1.11.2 client fails its identity probe (valid tag, exit 7) -> exit 1" 1 "$(cat "$CTL/rc")"
eq "no upgrade call" "" "$(upgrade_line)"
new_case clientrc2; echo 7 > "$CTL/client_rc.talosctl-1112.exe"; cp "$ROOT/talosctl-stub" "$OUT/talosctl-spare.exe"; echo "v1.11.2" > "$CTL/tag.talosctl-spare.exe"; run 192.168.0.47 v1.11.6
eq "a healthy second v1.11.2 client -> exit 0" 0 "$(cat "$CTL/rc")"
has "and the failing one never upgrades" "talosctl-spare.exe" "$(upgrade_line)"
rm -f "$OUT/talosctl-spare.exe"
new_case newest; run 192.168.0.47 v1.11.6
has "the running-version read uses the NEWEST client (v1.14.2, not 11311 by name)" "talosctl-1142.exe" "$(grep ' version$' "$CTL/calls" | head -1)"

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
new_case noimg; echo 404 > "$CTL/curl_code"; run 192.168.0.41 v1.11.6
eq "image not in factory -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
has "says the image is not found" "not found" "$(cat "$CTL/out")"
new_case noclient; echo "v1.10.9" > "$CTL/server_version"; run 192.168.0.41 v1.11.6
eq "no talosctl for the running version -> exit 1" 1 "$(cat "$CTL/rc")"

echo "== a failed probe never passes on its output =="
new_case curlrc; echo 28 > "$CTL/curl_rc"; run 192.168.0.47 v1.11.6
eq "curl transport error after printing 200 -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
hasnt "and it is not reported as a missing image" "not found" "$(cat "$CTL/out")"
new_case curl503; echo 503 > "$CTL/curl_code"; run 192.168.0.47 v1.11.6
eq "factory HTTP 503 -> exit 1" 1 "$(cat "$CTL/rc")"; has "reported as HTTP 503" "503" "$(cat "$CTL/out")"
hasnt "not as a missing image" "not found" "$(cat "$CTL/out")"
for k in version extensions platformmetadata machinetype; do
  new_case "fail$k"; touch "$CTL/fail.$k"; run 192.168.0.47 v1.11.6
  eq "talosctl $k failing (with valid output) -> exit 1" 1 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
done

echo "== --dry-run prints the exact command and never upgrades =="
new_case dry; run 192.168.0.41 v1.11.6 --dry-run
eq "exit 0" 0 "$(cat "$CTL/rc")"; eq "no upgrade call" "" "$(upgrade_line)"
has "prints the command" "upgrade" "$(cat "$CTL/out")"
has "prints the image" "nocloud-installer/$SCHEM_A:v1.11.6" "$(cat "$CTL/out")"
SPACED="$ROOT/out dir"; mkdir -p "$SPACED"; cp "$OUT"/talosctl-*.exe "$OUT/talosconfig" "$SPACED/"
new_case spaced; RUN_OUT="$SPACED" run 192.168.0.47 v1.11.6 --dry-run
eq "a TALOS_OUT with a space works" 0 "$(cat "$CTL/rc")"
has "and the printed command keeps the argument boundary" 'out\ dir/talosctl-1112.exe' "$(cat "$CTL/out")"

echo "== without TALOS_OUT, a worktree resolves the MAIN checkout's _out =="
new_case wt
( cd "$ROOT" && git init -q main && cd main && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init \
  && git worktree add -q "$ROOT/wt" -b wt ) >/dev/null 2>&1
mkdir -p "$ROOT/main/kubernetes/infra/_out"; cp "$OUT"/talosctl-*.exe "$OUT/talosconfig" "$ROOT/main/kubernetes/infra/_out/"
( cd "$ROOT/wt" && PATH="$BIN:$PATH" TALOS_OUT= bash "$SCRIPT" 192.168.0.47 v1.11.6 > "$CTL/out" 2>&1; echo $? > "$CTL/rc" )
eq "exit 0 from the worktree" 0 "$(cat "$CTL/rc")"
has "uses the main checkout's _out" "main/kubernetes/infra/_out/talosconfig" "$(upgrade_line)"

echo "== bad arguments =="
new_case args; run 192.168.0.41
eq "missing target -> exit 2" 2 "$(cat "$CTL/rc")"
new_case args2; run not-an-ip v1.11.6
eq "bad node -> exit 2" 2 "$(cat "$CTL/rc")"
new_case args3; run 192.168.0.300 v1.11.6
eq "octet over 255 -> exit 2" 2 "$(cat "$CTL/rc")"

echo
echo "talos-upgrade-node: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
