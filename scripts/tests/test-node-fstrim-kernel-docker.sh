#!/usr/bin/env bash
# node-fstrim.sh against REAL filesystems on a real kernel, in the CronJob's own pinned image: an XFS volume
# (Talos /var) and an ext4 one, each on a loop device whose backing file is sparse, seen through a READ-ONLY
# bind mount (the pod's hostPath is readOnly) and trimmed by a process holding CAP_SYS_ADMIN only (the
# pod's capabilities). It checks that the freed space really comes back out of the backing files (the
# "thin pool"), that the chunking reaches the end of each filesystem (statfs understates it), and that a
# second XFS run still exits 0.
#
#   bash scripts/tests/test-node-fstrim-kernel-docker.sh
#
# MANUAL, not in CI: --privileged (loop devices, mounts) applies to the Docker host's VM or kernel, never to
# a lab host; xfsprogs and e2fsprogs come from the network, only to make the test filesystems (the slim image
# has neither mkfs). Git Bash + Docker Desktop on Windows.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="$(sed -nE 's/^ *image: (mirror\.gcr\.io\/library\/debian:[^ ]+)$/\1/p' "$ROOT/kubernetes/apps/node-fstrim/base/cronjob.yaml")"
[ -n "$IMAGE" ] || { echo "cannot read the image from base/cronjob.yaml"; exit 1; }
hostpath() { if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
echo "image: $IMAGE"

MSYS_NO_PATHCONV=1 docker run --rm --privileged -v "$(hostpath "$ROOT"):/repo:ro" "$IMAGE" bash -c '
set -u
fail=0
ok() { echo "PASS: $*"; }
bad() { echo "FAIL: $*"; fail=1; }
apt-get -qq update > /dev/null 2>&1 && apt-get -qq install -y xfsprogs e2fsprogs > /dev/null 2>&1 || { echo "cannot install xfsprogs, e2fsprogs"; exit 1; }
mkdir -p /b /host/var /host/ws /pod/var /pod/ws && mount -t tmpfs -o size=4g tmpfs /b
alloc() { echo $(( $(stat -c %b "$1") * 512 / 1048576 )); }
truncate -s 2500M /b/var.img && mkfs.xfs -q -f /b/var.img
truncate -s 1500M /b/ws.img && mkfs.ext4 -q -F /b/ws.img
LV=$(losetup -f --show /b/var.img); LW=$(losetup -f --show /b/ws.img)
mount "$LV" /host/var && mount "$LW" /host/ws
for m in var ws; do
  dd if=/dev/urandom of=/host/$m/blob bs=1M count=600 status=none; sync; rm /host/$m/blob; sync
  mount --bind /host/$m /pod/$m && mount -o remount,bind,ro /pod/$m
done
sleep 6
b_var=$(alloc /b/var.img); b_ws=$(alloc /b/ws.img)
# The pod: root with CAP_SYS_ADMIN only, the settings of base/cronjob.yaml but 1 GiB chunks.
setpriv --bounding-set -all,+sys_admin --inh-caps -all,+sys_admin env CHUNK_GB=1 RUN_MAX_S=300 \
  LOCK_FILE=/tmp/node-fstrim.lock STOP_FILE=/tmp/node-fstrim.stop bash /repo/kubernetes/apps/node-fstrim/node-fstrim.sh /pod/var /pod/ws > /tmp/out 2>&1
rc=$?
cat /tmp/out | sed "s/^/    /"
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc"
a_var=$(alloc /b/var.img); a_ws=$(alloc /b/ws.img)
[ $((b_var - a_var)) -ge 550 ] && ok "XFS: the freed 600 MiB came back ($b_var -> $a_var MiB allocated)" || bad "XFS: $b_var -> $a_var MiB"
[ $((b_ws - a_ws)) -ge 550 ] && ok "ext4: the freed 600 MiB came back ($b_ws -> $a_ws MiB allocated)" || bad "ext4: $b_ws -> $a_ws MiB"
grep -q "/pod/var chunk=3/3" /tmp/out && ok "XFS covered in 3 chunks (statfs < 2.5 GiB, the last one to the end)" || bad "XFS chunks"
grep -q "/pod/ws chunk=2/2" /tmp/out && ok "ext4 covered in 2 chunks" || bad "ext4 chunks"
grep -q "complete - exit 0 (done)" /tmp/out && ok "done" || bad "not done"
findmnt -no OPTIONS /pod/var | grep -q "^ro," && ok "it trimmed through the read-only bind" || bad "the bind was not read-only"
# Without CAP_SYS_ADMIN FITRIM is refused: the capability is the one the pod needs, and enough.
setpriv --bounding-set -all --inh-caps -all env CHUNK_GB=1 LOCK_FILE=/tmp/l2 bash /repo/kubernetes/apps/node-fstrim/node-fstrim.sh /pod/ws > /tmp/out2 2>&1
rc2=$?
[ "$rc2" -eq 5 ] && grep -q "Operation not permitted" /tmp/out2 && ok "without CAP_SYS_ADMIN: refused (exit 5, EPERM)" || bad "without CAP_SYS_ADMIN: exit $rc2"
# XFS keeps no record of what it trimmed: a second run walks the free space again and still succeeds.
setpriv --bounding-set -all,+sys_admin --inh-caps -all,+sys_admin env CHUNK_GB=1 LOCK_FILE=/tmp/l3 bash /repo/kubernetes/apps/node-fstrim/node-fstrim.sh /pod/var > /tmp/out3 2>&1
[ $? -eq 0 ] && ok "a second XFS run: exit 0" || bad "a second XFS run failed"
# STATE_DIR on the real XFS: a budget that fits one 1 GiB chunk per run (31 s against a 30 s chunk cost), so the
# pass spans three runs, each starting at the cursor the last one left, the third to the real end.
mkdir -p /tmp/state
for n in 1 2 3; do
  setpriv --bounding-set -all,+sys_admin --inh-caps -all,+sys_admin env CHUNK_GB=1 RUN_MAX_S=31 STATE_DIR=/tmp/state \
    LOCK_FILE=/tmp/l4 bash /repo/kubernetes/apps/node-fstrim/node-fstrim.sh /pod/var > /tmp/run$n 2>&1
  echo "rc=$? $(grep -o "chunk=[0-9]/3" /tmp/run$n | tr "\n" " ")" >> /tmp/runs
done
sed "s/^/    /" /tmp/runs
[ "$(cat /tmp/runs | tr "\n" "|")" = "rc=0 chunk=1/3 |rc=0 chunk=2/3 |rc=0 chunk=3/3 |" ] \
  && ok "STATE_DIR: one chunk per run, continued at the cursor, the pass completes in the third" || bad "STATE_DIR runs: $(cat /tmp/runs)"
[ -s /tmp/state/_pod_var.complete ] && [ ! -e /tmp/state/_pod_var.cursor ] && ok "STATE_DIR: the complete pass is recorded, the cursor removed" \
  || bad "STATE_DIR: $(ls /tmp/state)"
umount /pod/var /pod/ws /host/var /host/ws; losetup -d "$LV" "$LW"
[ "$fail" -eq 0 ] && echo "node-fstrim kernel test: all cases passed" || echo "node-fstrim kernel test: FAILURES"
exit "$fail"
'
