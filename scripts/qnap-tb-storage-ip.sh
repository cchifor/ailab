#!/bin/sh
# Runs ON THE QNAP (root cron, every minute) as /etc/config/tb-storage-ip.sh.
# Installed by scripts/qnap-tb-storage-ip-install.sh. Do not hand-edit the deployed copy.
#
# 1. Keeps the NFS service IP 10.55.0.254/24 on the Thunderbolt bridge (tbtbr0). Since 2026-11-27
#    QNAP's own config gives the bridge this address at boot (runbook qnap-storage-setup.md §2), so
#    this line only acts if that ever fails. Drop it once a NAS boot has proven the static config.
# 2. Once per boot, after NFS is exporting: if the kernel export table has fewer read-write entries
#    for the Thunderbolt subnet (10.55.0.0/24) than for the 10GbE subnet (10.55.1.0/24), restart NFS
#    to re-export. Measured 2026-06-14 (bf7559ed): after a NAS reboot the Thunderbolt subnet's rw rule
#    was missing, so ai-node1/2 were read-only while ai-node3 was fine. On that boot the bridge got
#    its address only from this cron job, after NFS had started. Until 2026-11-27 the restart ran on
#    every boot. Now it runs only when the rule is missing, and logs it (syslog tag tb-storage-ip),
#    because an unneeded restart lands under control planes that have just booted from this share.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
SVC_IP=10.55.0.254

ip addr show dev tbtbr0 2>/dev/null | grep -q " $SVC_IP/24" || ip addr add "$SVC_IP/24" dev tbtbr0 2>/dev/null

FLAG=/tmp/.ailab-nfs-reexported
[ -f "$FLAG" ] && exit 0
[ -d /share/pve-nfs ] || exit 0
tb=$(exportfs -v 2>/dev/null | grep -c '10\.55\.0\.0/24(.*rw')
eth=$(exportfs -v 2>/dev/null | grep -c '10\.55\.1\.0/24(.*rw')
[ "$eth" -gt 0 ] || exit 0   # NFS is not exporting yet: judge on a later run
if [ "$tb" -lt "$eth" ]; then
  logger -t tb-storage-ip "Thunderbolt subnet rw export missing ($tb of $eth entries); restarting NFS"
  /etc/init.d/nfs restart >/dev/null 2>&1
fi
touch "$FLAG"
