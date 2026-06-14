#!/usr/bin/env bash
# Configure QNAP storage-network + NFS share for the lab via qcli (persistent, idempotent).
# Does NOT touch the existing zpool1 RAID-Z1 pool (only adds a shared folder + NFS access).
#
# Prereqs: .env filled (QNAP_SSH_USER/QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
# NOTE: the Thunderbolt *bridge* service IP (10.55.0.254) cannot be set via qcli (system
#       bridge has no interface ID) -> set it once in the QNAP UI (see runbook). Everything
#       else here is code.
set -euo pipefail
cd "$(dirname "$0")/.."

SHARE="${SHARE:-pve-nfs}"
POOLID="${POOLID:-1}"
SIZE="${SIZE:-5497558138880}"     # 5 TiB thin quota
ETH1_IP="${ETH1_IP:-10.55.1.254}" # QNAP 10GbE <-> node3
SVC_IP="${SVC_IP:-10.55.0.254}"   # NFS service IP on the Thunderbolt bridge (tbtbr0)

python scripts/qnap-ssh.py "
  set -e
  qcli -l user={{QNAP_USER}} pw={{QNAP_PW}} saveauthsid=yes >/dev/null 2>&1

  echo '== 10GbE (eth1) static IP for node3 link =='
  qcli_network -m interfaceID=eth1 IPType=STATIC IP=${ETH1_IP} netmask=255.255.255.0 dns_type=manual 2>&1 | head -2

  echo '== enable NFS (v3 + v4) =='
  qcli_networkservice -n nfsServerEnabled=Enabled nfsServerEnabledV4=Enabled 2>&1 | head -1

  echo '== shared folder ${SHARE} (pool ${POOLID}, thin, lz4, no dedup) =='
  if qcli_sharedfolder -C sharename=${SHARE} 2>/dev/null | grep -qi exist; then
    echo '   share already exists'
  else
    qcli_sharedfolder -s sharename=${SHARE} poolID=${POOLID} comment=ProxmoxNFS guest=deny compress=1 dedup=0 type=1 size=${SIZE} 2>&1 | head -2
    sleep 6
  fi

  echo '== NFS host access (both storage subnets, rw, no_root_squash) =='
  qcli_sharedfolder -N sharename=${SHARE} Access=Enabled 2>&1 | head -1
  qcli_sharedfolder -T sharename=${SHARE} HostIP=10.55.0.0/24 Permission=rw Squash=no_root_squash secure=1 sync=1 wdelay=0 2>&1 | head -1
  qcli_sharedfolder -T sharename=${SHARE} HostIP=10.55.1.0/24 Permission=rw Squash=no_root_squash secure=1 sync=1 wdelay=0 2>&1 | head -1

  echo '== result =='
  qcli_sharedfolder -n sharename=${SHARE} 2>&1 | head -12
  echo '-- exportfs --'; exportfs -v 2>/dev/null | grep -i ${SHARE} || true
"
echo
echo "== persist Thunderbolt-bridge service IP ${SVC_IP} via cron reconciler =="
# qcli cannot set the system Thunderbolt bridge IP, so we use an idempotent reconciler on the
# persistent DOM (/etc/config) + a cron entry. Self-heals the IP within 1 min of any reboot.
python scripts/qnap-ssh.py <<PYEOF
cat > /tmp/tb-storage-ip.sh <<'SH'
#!/bin/sh
# ailab QNAP storage reconciler (cron, every minute).
# 1) ensure the Thunderbolt-bridge NFS service IP is present (idempotent)
/usr/bin/ip addr show dev tbtbr0 2>/dev/null | grep -q "${SVC_IP}/24" || /usr/bin/ip addr add ${SVC_IP}/24 dev tbtbr0 2>/dev/null
# 2) once per boot, after the share is mounted, re-export NFS: a QNAP boot-race drops the
#    per-subnet rw rule from the kernel export table -> TB-subnet clients would be read-only.
FLAG=/tmp/.ailab-nfs-reexported
if [ ! -f "\$FLAG" ] && [ -d /share/${SHARE} ] && [ "\$(cut -d. -f1 /proc/uptime 2>/dev/null)" -gt 90 ] 2>/dev/null; then
  /etc/init.d/nfs restart >/dev/null 2>&1
  touch "\$FLAG"
fi
SH
echo {{QNAP_PW}} | sudo -S -p "" cp /tmp/tb-storage-ip.sh /etc/config/tb-storage-ip.sh
echo {{QNAP_PW}} | sudo -S -p "" chmod 755 /etc/config/tb-storage-ip.sh
echo {{QNAP_PW}} | sudo -S -p "" sh -c 'grep -q tb-storage-ip /etc/config/crontab || echo "* * * * * /etc/config/tb-storage-ip.sh >/dev/null 2>&1" >> /etc/config/crontab'
echo {{QNAP_PW}} | sudo -S -p "" crontab /etc/config/crontab
echo {{QNAP_PW}} | sudo -S -p "" /etc/config/tb-storage-ip.sh
ip -br addr show tbtbr0
PYEOF
echo
echo "Done. QNAP storage configured (network + share + service-IP persistence) — all as code."
