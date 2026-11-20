#!/usr/bin/env bash
# Converge the dev_worker fstrim tasks (tasks_from) in an Ubuntu 24.04 container with systemd as PID 1. Cases
# are in fstrim-converge/converge.sh: a first --check (nothing written, the stock timer left on); apply (only
# the root entry's discard dropped from fstab, backed up; the remount skipped on a root without discard; the
# unit, timer and settings; the stock weekly timer masked; no trim started); idempotence and --check after
# apply; the unit run end to end against a real ext4 /workspace on a loop device (the freed space comes back
# out of the sparse backing file; the beacon says done); refused settings; disable (the stock weekly timer back
# on, fstab left nodiscard) and a second disable.
#
#   bash ansible/roles/dev_worker/tests/test-fstrim-converge-docker.sh
#
# MANUAL, not in CI: the image build needs the network (Ubuntu, PyPI). --privileged is for systemd as PID 1 and
# the loop device; it applies to the Docker host's VM or kernel, never to a lab host or a dev worker. On Windows
# use Git Bash with Docker Desktop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
IMAGE=dev-worker-fstrim-converge:local
NAME="dev-worker-fstrim-converge-$$"

cleanup() { docker rm -f "$NAME" > /dev/null 2>&1 || true; }
trap cleanup EXIT

hostpath() { if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

docker build -q -t "$IMAGE" "$(hostpath "$HERE/fstrim-converge")"
MSYS_NO_PATHCONV=1 docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$(hostpath "$ROOT"):/repo:ro" -v "$(hostpath "$HERE/fstrim-converge"):/t:ro" "$IMAGE" > /dev/null
for _ in $(seq 1 30); do
  case "$(docker exec "$NAME" systemctl is-system-running 2> /dev/null || true)" in
    running | degraded) break ;;
  esac
  sleep 1
done
MSYS_NO_PATHCONV=1 docker exec "$NAME" bash /t/converge.sh
