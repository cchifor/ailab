#!/usr/bin/env bash
# Converge the pve_base nfs_disk_gate tasks in a Docker container with systemd as PID 1 (tasks_from, so the rest of
# pve_base is not involved), then boot a stand-in pve-guests.service through the drop-in. Proxmox is not installed:
# the stand-in unit has the stock unit's shape (oneshot, an ExecStartPre, then startall), and a tmpfs mounted at
# /mnt/pve/qnap-nfs plays the NFS share. Cases are in nfs-disk-gate-converge/converge.sh: a first --check; apply;
# idempotence and --check after apply; the share ready, late and never there; disable and a second disable.
#
#   bash ansible/roles/pve_base/tests/test-nfs-disk-gate-converge-docker.sh
#
# MANUAL, not in CI: the image build needs the network (Debian, PyPI). --privileged is for systemd as PID 1 and the
# tmpfs mounts; it applies to the Docker host's VM or kernel, never to a lab host. On Windows use Git Bash with
# Docker Desktop. Takes about 2 min after the image is built (the "never there" case waits out a 30 s budget).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
IMAGE=pve-nfs-disk-gate-converge:local
NAME="pve-nfs-disk-gate-converge-$$"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT

hostpath() { if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

docker build -q -t "$IMAGE" "$(hostpath "$HERE/nfs-disk-gate-converge")"
MSYS_NO_PATHCONV=1 docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$(hostpath "$ROOT"):/repo:ro" -v "$(hostpath "$HERE/nfs-disk-gate-converge"):/t:ro" "$IMAGE" > /dev/null
for _ in $(seq 1 30); do
  case "$(docker exec "$NAME" systemctl is-system-running 2> /dev/null || true)" in
    running | degraded) break ;;
  esac
  sleep 1
done
MSYS_NO_PATHCONV=1 docker exec "$NAME" bash /t/converge.sh
