#!/bin/sh
# blk-iocost on one block device (pve_base, tag iocost; see roles/pve_base/defaults/main.yml).
#   ailab-iocost enable  <dev> [qos]   kernel auto cost model; QoS = the given latency targets (ctrl=user), or the
#                                      kernel's auto QoS when none are given; controller on
#   ailab-iocost disable <dev>         controller off (every cgroup back to unweighted sharing)
#   ailab-iocost status  <dev>         print this device's io.cost.qos and io.cost.model lines
# AILAB_SYSFS / AILAB_CGROUP override /sys and /sys/fs/cgroup (tests only).
set -eu
SYSFS=${AILAB_SYSFS:-/sys}
CGROUP=${AILAB_CGROUP:-/sys/fs/cgroup}
action=${1:-}
dev=${2:-}
qos=${3:-}
case "$dev" in
  ''|*/*|.*) echo "ailab-iocost: bad device name '$dev'" >&2; exit 2 ;;
esac
devfile="$SYSFS/block/$dev/dev"
[ -r "$devfile" ] || { echo "ailab-iocost: no such block device: $dev" >&2; exit 2; }
mm=$(cat "$devfile")
case "$mm" in
  *[!0-9:]*|'') echo "ailab-iocost: unexpected MAJ:MIN '$mm' for $dev" >&2; exit 2 ;;
esac
# QoS parameters are key=value words (rpct= rlat= wpct= wlat= min= max=): lowercase, digits, '=', '.', spaces.
case "$qos" in
  *[!a-z0-9=.' ']*) echo "ailab-iocost: bad QoS parameters '$qos'" >&2; exit 2 ;;
esac
case "$action" in
  enable)
    # Model first (auto = the kernel's built-in linear model for this device class), then QoS on.
    printf '%s ctrl=auto\n' "$mm" > "$CGROUP/io.cost.model"
    if [ -n "$qos" ]; then
      printf '%s enable=1 ctrl=user %s\n' "$mm" "$qos" > "$CGROUP/io.cost.qos"
    else
      printf '%s enable=1 ctrl=auto\n' "$mm" > "$CGROUP/io.cost.qos"
    fi
    ;;
  disable)
    printf '%s enable=0\n' "$mm" > "$CGROUP/io.cost.qos"
    ;;
  status)
    grep "^$mm " "$CGROUP/io.cost.qos" || true
    grep "^$mm " "$CGROUP/io.cost.model" || true
    ;;
  *)
    echo "usage: ailab-iocost enable <dev> [qos] | disable <dev> | status <dev>   (dev e.g. nvme0n1)" >&2
    exit 2
    ;;
esac
