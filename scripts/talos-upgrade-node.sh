#!/usr/bin/env bash
# Upgrade ONE Talos node with the right installer image and the right client
# (plans/2026-10-08-talos-upgrade-program-plan.md, P0.6).
#
#   talos-upgrade-node.sh <node-ip> <target-version> [--dry-run]
#   e.g. talos-upgrade-node.sh 192.168.0.47 v1.11.6
#
# WHY. talosctl's default --image drops the node's system extensions. 1.11-1.13 clients default to the
# plain ghcr installer; the 1.14 client defaults to metal-installer with an EMPTY schematic. Losing
# iscsi-tools breaks every Trident iSCSI PV, and losing kata/gvisor breaks the env/agent sandboxes.
# Nodes do not share one schematic: agent-node-3 and the env node carry kata/gvisor. So the image is
# always built from THIS node's live schematic:
#     factory.talos.dev/nocloud-installer/<live schematic>:<target>
# and checked against the factory registry first.
#
# CLIENT. The upgrade runs the client whose `version --client` IS the node's running version (the
# docs' rule for upgrades), found by asking every _out/talosctl-*.exe what it is. File names are only
# a convention (talosctl-1112.exe = v1.11.2) and are never trusted. The running version itself is read
# with the newest client present.
#
# CONTROL PLANES. A node whose live machine type is controlplane is upgraded THROUGH another CP: with
# the talosconfig's default endpoints, the API connection that streams `--wait` could be the very node
# that reboots. The survivor is the first other address in TALOS_CP_IPS that answers `version` within
# 20 s; no reachable survivor is a refusal.
#
# PARSING. talosctl -o json is a stream of resource objects; it is parsed structurally (python3 or
# python, whichever runs), and the node must report exactly one `schematic` extension (64 hex) and
# exactly one platform. Every probe's exit status counts: a failed talosctl or curl call is a refusal
# even if it printed something that looks valid.
#
# Refuses: skipping a minor, a downgrade or no-op, a missing or ambiguous schematic, a non-nocloud
# platform, an image absent from the factory (or a factory probe that failed), no client for the
# running version, and a CP with no reachable survivor. It never passes --force, and it leaves
# draining to Talos/talosctl (the program's per-node procedure pre-drains with kubectl and gates
# before and after; this script is only the upgrade step).
#
# Exit codes: 0 = upgrade done (or --dry-run printed); 1 = precondition failed; 2 = bad arguments,
# a malformed TALOS_CP_IPS, or a refused version step; other = talosctl's own exit status.
# Env: TALOS_OUT = the ops checkout's kubernetes/infra/_out (talosctl-*.exe + talosconfig); unset, it
#      resolves to the MAIN checkout's _out from a worktree.
#      TALOS_CP_IPS = the control-plane addresses (default: the ailab CPs).
set -uo pipefail

NODE="${1:-}"; TARGET="${2:-}"; DRY="${3:-}"
usage() { echo "usage: $0 <node-ip> <vX.Y.Z> [--dry-run]"; exit 2; }
is_ipv4() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local o; for o in "${BASH_REMATCH[@]:1}"; do [ "$((10#$o))" -le 255 ] || return 1; done
}
is_ipv4 "$NODE" || usage
[[ "$TARGET" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || usage
t_maj=${BASH_REMATCH[1]}; t_min=${BASH_REMATCH[2]}; t_pat=${BASH_REMATCH[3]}
[ -z "$DRY" ] || [ "$DRY" = "--dry-run" ] || usage
CP_IPS="${TALOS_CP_IPS:-192.168.0.41 192.168.0.42 192.168.0.43}"
for cp in $CP_IPS; do is_ipv4 "$cp" || { echo "TALOS_CP_IPS entry '$cp' is not an IPv4 address"; exit 2; }; done

if [ -z "${TALOS_OUT:-}" ]; then
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || { echo "set TALOS_OUT"; exit 1; }
  TALOS_OUT="$(cd "$common/.." && pwd -P)/kubernetes/infra/_out"
fi
TC="$TALOS_OUT/talosconfig"
[ -f "$TC" ] || { echo "no talosconfig at $TC"; exit 1; }
log() { echo "$(date -u +%FT%TZ) talos-upgrade-node[$NODE]: $*"; }
command -v timeout >/dev/null 2>&1 || { log "no 'timeout' command - refusing"; exit 1; }

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json' >/dev/null 2>&1; then PY=$c; break; fi
done
[ -n "$PY" ] || { log "no working python3/python (needed to parse talosctl JSON) - refusing"; exit 1; }
# jget <schematic|platform|machinetype>: reads a talosctl -o json stream on stdin, prints the one
# value or exits non-zero (malformed JSON, none, or more than one).
jget() {
  "$PY" -c '
import json, re, sys
mode = sys.argv[1]
s = sys.stdin.buffer.read().decode("utf-8")
dec, i, objs = json.JSONDecoder(), 0, []
while True:
    while i < len(s) and s[i].isspace():
        i += 1
    if i >= len(s):
        break
    o, i = dec.raw_decode(s, i)
    objs.append(o)
def spec(o):
    return o.get("spec") if isinstance(o, dict) else None
if mode == "schematic":
    v = [spec(o)["metadata"].get("version") for o in objs
         if isinstance(spec(o), dict) and isinstance(spec(o).get("metadata"), dict)
         and spec(o)["metadata"].get("name") == "schematic"]
    ok = len(v) == 1 and isinstance(v[0], str) and re.fullmatch(r"[0-9a-f]{64}", v[0])
elif mode == "platform":
    v = [spec(o).get("platform") for o in objs if isinstance(spec(o), dict)]
    ok = len(v) == 1 and isinstance(v[0], str)
else:
    v = [spec(o) for o in objs]
    ok = len(v) == 1 and isinstance(v[0], str)
if not ok:
    sys.exit(1)
print(v[0])
' "$1"
}

ERRF="$(mktemp)"; trap 'rm -f "$ERRF"' EXIT
# probe <description> <cmd...>: stdout -> $OUT; a non-zero exit is a refusal, whatever it printed.
probe() {
  local desc=$1; shift
  OUT="$("$@" 2>"$ERRF")"; local rc=$?
  [ "$rc" -eq 0 ] || { log "$desc failed (rc=$rc): $(tr '\n' ' ' < "$ERRF" | cut -c1-300)- refusing"; exit 1; }
}
tc() { "$1" --talosconfig "$TC" -n "$NODE" "${@:2}"; }

# Every client, by what it reports itself to be.
declare -A TAG=()
probe_bin=""; newest=""
for b in "$TALOS_OUT"/talosctl-*.exe; do
  [ -f "$b" ] || continue
  t="$("$b" version --client 2>/dev/null | awk '$1=="Tag:"{print $2; exit}')"
  [[ "$t" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || continue
  TAG["$b"]=$t
  key=$(printf '%06d%06d%06d' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}")
  [[ "$key" > "$newest" ]] && { newest=$key; probe_bin=$b; }
done
[ -n "$probe_bin" ] || { log "no working talosctl-*.exe in $TALOS_OUT"; exit 1; }

probe "reading the running Talos version" tc "$probe_bin" version
cur="$(awk '/^Server:/{s=1} s && $1=="Tag:"{print $2; exit}' <<<"$OUT")"
[[ "$cur" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { log "cannot read the running Talos version (got '$cur')"; exit 1; }
c_maj=${BASH_REMATCH[1]}; c_min=${BASH_REMATCH[2]}; c_pat=${BASH_REMATCH[3]}

# Only a patch step within the minor, or exactly the next minor. Never back, never a no-op.
step_ok=0
if [ "$t_maj" -eq "$c_maj" ]; then
  if [ "$t_min" -eq "$c_min" ] && [ "$t_pat" -gt "$c_pat" ]; then step_ok=1; fi
  if [ "$t_min" -eq $((c_min + 1)) ]; then step_ok=1; fi
fi
[ "$step_ok" -eq 1 ] || { log "refusing $cur -> $TARGET: only a patch step or the next minor (adjacent-minor upgrade path)"; exit 2; }

client=""
for b in "${!TAG[@]}"; do [ "${TAG[$b]}" = "$cur" ] && { client=$b; break; }; done
[ -n "$client" ] || { log "no talosctl-*.exe in $TALOS_OUT reports $cur (the running version)"; exit 1; }

probe "reading the platform" tc "$client" get platformmetadata -o json
platform="$(jget platform <<<"$OUT")" || { log "cannot read exactly one platform - refusing"; exit 1; }
[ "$platform" = "nocloud" ] || { log "platform is '$platform', not nocloud - refusing (the image would be wrong)"; exit 1; }

probe "reading the extensions" tc "$client" get extensions -o json
schematic="$(jget schematic <<<"$OUT")" || { log "no single valid schematic extension on the node - refusing (an upgrade would drop its extensions)"; exit 1; }

probe "reading the machine type" tc "$client" get machinetype -o json
mtype="$(jget machinetype <<<"$OUT")" || { log "cannot read the machine type - refusing"; exit 1; }

image="factory.talos.dev/nocloud-installer/$schematic:$TARGET"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -I \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
  "https://factory.talos.dev/v2/nocloud-installer/$schematic/manifests/$TARGET" 2>"$ERRF")"; crc=$?
[ "$crc" -eq 0 ] || { log "factory probe failed (curl rc=$crc: $(tr '\n' ' ' < "$ERRF" | cut -c1-200)) - refusing"; exit 1; }
[ "$code" != "404" ] || { log "image $image not found in the factory (HTTP 404) - refusing"; exit 1; }
[ "$code" = "200" ] || { log "factory probe for $image returned HTTP $code - refusing"; exit 1; }

in_cp_list=0
for cp in $CP_IPS; do [ "$cp" = "$NODE" ] && in_cp_list=1; done
endpoint=()
case "$mtype" in
  controlplane)
    [ "$in_cp_list" -eq 1 ] || { log "node is a control plane but not in TALOS_CP_IPS ($CP_IPS) - refusing"; exit 1; }
    for cand in $CP_IPS; do
      [ "$cand" = "$NODE" ] && continue
      if timeout 20 "$client" --talosconfig "$TC" -e "$cand" -n "$cand" version >/dev/null 2>&1; then
        endpoint=(-e "$cand"); break
      fi
      log "survivor candidate $cand did not answer"
    done
    [ "${#endpoint[@]}" -gt 0 ] || { log "no reachable control-plane endpoint other than the target - refusing"; exit 1; }
    ;;
  worker)
    [ "$in_cp_list" -eq 0 ] || { log "node is a worker but listed in TALOS_CP_IPS ($CP_IPS) - refusing"; exit 1; }
    ;;
  *) log "unknown machine type '$mtype' - refusing"; exit 1 ;;
esac

cmd=("$client" --talosconfig "$TC" "${endpoint[@]}" -n "$NODE" upgrade --image "$image" --wait)
log "running $cur ($mtype), schematic $schematic, target $TARGET"
log "command: $(printf '%q ' "${cmd[@]}")"
[ "$DRY" = "--dry-run" ] && { log "dry run - the command above is planned, not run"; exit 0; }
"${cmd[@]}"; rc=$?
[ "$rc" -eq 0 ] && log "upgrade returned 0; now run the per-node gates (version, extensions, etcd, workloads)" \
               || log "talosctl upgrade exited $rc - diagnose before any retry; never --force"
exit "$rc"
