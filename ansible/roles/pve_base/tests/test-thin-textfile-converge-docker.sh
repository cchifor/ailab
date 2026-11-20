#!/usr/bin/env bash
# Converge the pve_base thin_textfile tasks in a Docker container with systemd as PID 1 (tasks_from, so the rest
# of pve_base is not involved). Docker Desktop's kernel has no dm-thin-pool, so the real `lvs` runs against a
# linear volume (its output format, and the unit's hardening under real LVM locking) and a stub `lvs` prints
# the thin listing in that format. Cases are in thin-textfile-converge/converge.sh: a first --check; apply;
# idempotence and --check after apply; the unit run with the real lvs, then the thin listing; disable and a
# second disable.
#
#   bash ansible/roles/pve_base/tests/test-thin-textfile-converge-docker.sh
#
# MANUAL, not in CI: the image build needs the network (Debian, PyPI). --privileged is for systemd as PID 1, the
# loop device and LVM; it applies to the Docker host's VM or kernel, never to a lab host. On Windows use
# Git Bash with Docker Desktop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
IMAGE=pve-thin-textfile-converge:local
NAME="pve-thin-textfile-converge-$$"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT

hostpath() { if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

docker build -q -t "$IMAGE" "$(hostpath "$HERE/thin-textfile-converge")"
MSYS_NO_PATHCONV=1 docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$(hostpath "$ROOT"):/repo:ro" -v "$(hostpath "$HERE/thin-textfile-converge"):/t:ro" "$IMAGE" > /dev/null
for _ in $(seq 1 30); do
  case "$(docker exec "$NAME" systemctl is-system-running 2> /dev/null || true)" in
    running | degraded) break ;;
  esac
  sleep 1
done
MSYS_NO_PATHCONV=1 docker exec "$NAME" bash /t/converge.sh
