#!/usr/bin/env bash
# Converge the pve_ec_fan role in a Docker container, the closest thing to a Proxmox host that is not one.
# It builds the driver at the pinned commit with DKMS against the Proxmox kernel headers the hosts use.
# The container runs systemd as PID 1, with a fake /sys/class/ec_su_axb35. Cases are in
# converge/converge.sh: a first --check; apply (the profile runs once); idempotence (changed=0) and --check
# after apply; a converge with the repo unreachable; a second installed kernel; a curve change next to a
# failing build for another kernel (the curve is still applied); a fan dropped from the list (back to auto);
# recovery after a module reload under the active service; the APU power mode (applied on start, changed by a
# converge, balanced again on stop, a bad one refused); ec-fan-governor live under systemd (a sustained 88 C
# takes a fan at 40 % to 60 %, its metrics file, restarted with the profile, a config change reaches it, a
# governor outside the curve refused); refused profiles; disable after an apply that stopped
# part-way, and re-enable; a re-pin; an edited checkout; another DKMS version of the same source already
# installed (the upstream install script's); a new pin that does not build (the installed module stays); and
# the Secure Boot lockdown refusal.
# The container runs another kernel, so nothing is loaded: the srcversion reload and the live load only run
# on a host.
#
#   bash ansible/roles/pve_ec_fan/tests/test-converge-docker.sh
#
# MANUAL, not in CI: the image build needs the network (Debian, download.proxmox.com, PyPI), and the clone
# needs GitHub. --privileged is for systemd as PID 1 and for mounting a fake securityfs; it applies to the
# Docker host's VM or kernel, never to a lab host. Run it before converging a new driver pin. On Windows
# use Git Bash with Docker Desktop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
REPO_URL="$(sed -n 's/^pve_ec_fan_repo_url: //p' "$ROOT/ansible/roles/pve_ec_fan/defaults/main.yml")"
PIN="$(sed -n 's/^pve_ec_fan_commit: //p' "$ROOT/ansible/roles/pve_ec_fan/defaults/main.yml")"
IMAGE=pve-ec-fan-converge:local
NAME="pve-ec-fan-converge-$$"
WORK="$(mktemp -d)"

cleanup() {
  docker rm -f "$NAME" > /dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Docker Desktop on Windows needs C:/... paths for bind mounts.
hostpath() { if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

git clone -q "$REPO_URL" "$WORK/src"
git -C "$WORK/src" -c advice.detachedHead=false checkout -q "$PIN"
docker build -q -t "$IMAGE" "$(hostpath "$HERE/converge")"
MSYS_NO_PATHCONV=1 docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$(hostpath "$ROOT"):/repo:ro" -v "$(hostpath "$WORK/src"):/src:ro" \
  -v "$(hostpath "$HERE/converge"):/t:ro" "$IMAGE" > /dev/null
for _ in $(seq 1 30); do
  case "$(docker exec "$NAME" systemctl is-system-running 2> /dev/null || true)" in
    running | degraded) break ;;
  esac
  sleep 1
done
MSYS_NO_PATHCONV=1 docker exec "$NAME" bash /t/converge.sh
