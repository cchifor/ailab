#!/bin/sh
# Runs ON THE QNAP (root cron, every minute) as /etc/config/tb-storage-ip.sh.
# Installed by scripts/qnap-tb-storage-ip-install.sh. Do not hand-edit the deployed copy.
#
# 1. Keeps the NFS service IP 10.55.0.254/24 on the Thunderbolt bridge (tbtbr0). Since 2026-11-27
#    QNAP's own config gives the bridge this address at boot (runbook qnap-storage-setup.md §2), so
#    this line only acts if that ever fails. Drop it once a NAS boot has proven the static config.
# 2. Once per boot, after NFS is exporting the pve-nfs share: if the kernel export table has fewer
#    read-write entries for that share to the Thunderbolt subnet (10.55.0.0/24) than to the 10GbE
#    subnet (10.55.1.0/24), on two runs in a row, restart NFS to re-export. Measured 2026-06-14
#    (bf7559ed): after a NAS reboot the Thunderbolt subnet's rw rule was missing, so ai-node1/2 were
#    read-only while ai-node3 was fine. On that boot the bridge got its address only from this cron
#    job, after NFS had started. Until 2026-11-27 the restart ran on every boot. Now it runs only when
#    the rule is missing, and logs it (syslog tag tb-storage-ip), because an unneeded restart lands
#    under control planes that have just booted from this share. Two runs, because one sample can be
#    taken while the exports are still being added.
# The TB_* variables are test seams (scripts/tests/test_qnap_tb_storage_ip.py); cron sets none of them.
PATH="${TB_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH
SVC_IP=10.55.0.254
SHARE=pve-nfs
STATE="${TB_STATE:-/tmp}"
SHARE_ROOT="${TB_SHARE_ROOT:-/share}"
NFS_INIT="${TB_NFS_INIT:-/etc/init.d/nfs}"

ip addr show dev tbtbr0 2>/dev/null | grep -q " $SVC_IP/24" || ip addr add "$SVC_IP/24" dev tbtbr0 2>/dev/null

FLAG="$STATE/.ailab-nfs-reexported"
SEEN="$STATE/.ailab-nfs-tb-rw-missing"
[ -f "$FLAG" ] && exit 0
[ -d "$SHARE_ROOT/$SHARE" ] || exit 0

# rw entries of the share's exports (QNAP exports it as /share/<pool>/pve-nfs and /share/NFSv=4/pve-nfs)
# for one client subnet. `exportfs -v` prints a long path alone on a line and each client indented below.
rw_entries() {  # $1 = client subnet, e.g. 10.55.0.0/24
  exportfs -v 2>/dev/null | awk -v net="$1(" -v share="/$SHARE" '
    /^[^ \t]/ { path = $1; if (NF == 1) next }
    substr(path, length(path) - length(share) + 1) == share && index($0, net) && $0 ~ /[(,]rw[,)]/ { n++ }
    END { print n + 0 }'
}
tb=$(rw_entries 10.55.0.0/24)
eth=$(rw_entries 10.55.1.0/24)
[ "$eth" -gt 0 ] || exit 0   # the share is not exported yet: judge on a later run
if [ "$tb" -ge "$eth" ]; then
  rm -f "$SEEN"
  touch "$FLAG"
  exit 0
fi
if [ ! -f "$SEEN" ]; then
  : > "$SEEN"                # first sighting: look again on the next run
  exit 0
fi
logger -t tb-storage-ip "Thunderbolt subnet rw export of $SHARE missing ($tb of $eth entries) on two runs; restarting NFS"
"$NFS_INIT" restart >/dev/null 2>&1
rm -f "$SEEN"
touch "$FLAG"
