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
# and checked against the factory registry first. The client binary matches the version the node is
# RUNNING (the docs' rule for upgrades): _out/talosctl-<digits>.exe, e.g. v1.11.2 -> talosctl-1112.exe.
#
# Refuses: skipping a minor, a downgrade or no-op, a missing schematic, a non-nocloud platform, an image
# absent from the factory, and no client for the running version. It never passes --force, and it
# leaves draining to Talos/talosctl (the program's per-node procedure pre-drains with kubectl and gates
# before and after; this script is only the upgrade step).
#
# Exit codes: 0 = upgrade done (or --dry-run printed); 1 = precondition failed; 2 = bad arguments or
# refused version step; other = talosctl's own exit status.
# Env: TALOS_OUT = the ops checkout's kubernetes/infra/_out (talosctl-*.exe + talosconfig).
set -uo pipefail

NODE="${1:-}"; TARGET="${2:-}"; DRY="${3:-}"
usage() { echo "usage: $0 <node-ip> <vX.Y.Z> [--dry-run]"; exit 2; }
[[ "$NODE" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || usage
[[ "$TARGET" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || usage
t_maj=${BASH_REMATCH[1]}; t_min=${BASH_REMATCH[2]}; t_pat=${BASH_REMATCH[3]}
[ -z "$DRY" ] || [ "$DRY" = "--dry-run" ] || usage

if [ -z "${TALOS_OUT:-}" ]; then
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || { echo "set TALOS_OUT"; exit 1; }
  TALOS_OUT="$(cd "$common/.." && pwd -P)/kubernetes/infra/_out"
fi
TC="$TALOS_OUT/talosconfig"
[ -f "$TC" ] || { echo "no talosconfig at $TC"; exit 1; }
log() { echo "$(date -u +%FT%TZ) talos-upgrade-node[$NODE]: $*"; }

# Any client can read the server version; prefer the newest one present.
probe="$(ls "$TALOS_OUT"/talosctl-*.exe 2>/dev/null | sort -V | tail -n 1)"
[ -n "$probe" ] || { log "no talosctl-*.exe in $TALOS_OUT"; exit 1; }
tc() { "$1" --talosconfig "$TC" -n "$NODE" "${@:2}"; }

cur="$(tc "$probe" version 2>/dev/null | awk '/^Server:/{s=1} s && $1=="Tag:"{print $2; exit}')"
[[ "$cur" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { log "cannot read the running Talos version (got '$cur')"; exit 1; }
c_maj=${BASH_REMATCH[1]}; c_min=${BASH_REMATCH[2]}; c_pat=${BASH_REMATCH[3]}

# Only a patch step within the minor, or exactly the next minor. Never back, never a no-op.
step_ok=0
if [ "$t_maj" -eq "$c_maj" ]; then
  if [ "$t_min" -eq "$c_min" ] && [ "$t_pat" -gt "$c_pat" ]; then step_ok=1; fi
  if [ "$t_min" -eq $((c_min + 1)) ]; then step_ok=1; fi
fi
[ "$step_ok" -eq 1 ] || { log "refusing $cur -> $TARGET: only a patch step or the next minor (adjacent-minor upgrade path)"; exit 2; }

client="$TALOS_OUT/talosctl-${c_maj}${c_min}${c_pat}.exe"
[ -x "$client" ] || [ -f "$client" ] || { log "no client matching the running version $cur ($client)"; exit 1; }

platform="$(tc "$probe" get platformmetadata -o json 2>/dev/null | sed -nE 's/.*"platform" *: *"([^"]+)".*/\1/p' | head -n 1)"
[ "$platform" = "nocloud" ] || { log "platform is '$platform', not nocloud - refusing (the image would be wrong)"; exit 1; }

schematic="$(tc "$probe" get extensions -o json 2>/dev/null | tr -d '\n' | grep -oE '"name" *: *"schematic" *, *"version" *: *"[0-9a-f]{64}"' | grep -oE '[0-9a-f]{64}' | head -n 1)"
[ -n "$schematic" ] || { log "no schematic extension on the node - refusing (an upgrade would drop its extensions)"; exit 1; }

image="factory.talos.dev/nocloud-installer/$schematic:$TARGET"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -I \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json' \
  "https://factory.talos.dev/v2/nocloud-installer/$schematic/manifests/$TARGET")"
[ "$code" = "200" ] || { log "image $image not found in the factory (HTTP $code) - refusing"; exit 1; }

# A CP is upgraded THROUGH another CP. With the talosconfig's default endpoints, the API connection
# that streams `--wait` could be the very node that reboots. Workers use the default endpoints.
CP_IPS="${TALOS_CP_IPS:-192.168.0.41 192.168.0.42 192.168.0.43}"
endpoint=()
for cp in $CP_IPS; do
  if [ "$cp" = "$NODE" ]; then
    for other in $CP_IPS; do [ "$other" != "$NODE" ] && { endpoint=(-e "$other"); break; }; done
  fi
done

cmd=("$client" --talosconfig "$TC" "${endpoint[@]}" -n "$NODE" upgrade --image "$image" --wait)
log "running $cur, schematic $schematic, target $TARGET"
log "command: ${cmd[*]}"
[ "$DRY" = "--dry-run" ] && { log "dry run - not upgrading"; exit 0; }
"${cmd[@]}"; rc=$?
[ "$rc" -eq 0 ] && log "upgrade returned 0; now run the per-node gates (version, extensions, etcd, workloads)" \
               || log "talosctl upgrade exited $rc - diagnose before any retry; never --force"
exit "$rc"
