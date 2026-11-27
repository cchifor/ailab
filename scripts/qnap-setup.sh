#!/usr/bin/env bash
# Configure QNAP storage-network + NFS share for the lab via qcli (persistent, idempotent).
# Does NOT touch the existing zpool1 RAID-Z1 pool (only adds a shared folder + NFS access).
#
# Prereqs: .env filled (QNAP_SSH_USER/QNAP_ADMIN_PASSWORD), SSH enabled on the QNAP.
# NOTE: the Thunderbolt *bridge* service IP (10.55.0.254) cannot be set via qcli (the system
#       bridge has no interface ID). Set it in the QNAP UI as a static address (QNAP FAQ: Network
#       & Virtual Switch > Advanced > Virtual Switch > Thunderbolt Bridge > Configure > IPv4), so it
#       exists before iSCSI and NFS start at boot: runbook qnap-storage-setup.md §2. The cron
#       reconciler installed at the end (scripts/qnap-tb-storage-ip-install.sh) is the fallback.
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
echo "== storage reconciler (service IP fallback + conditional NFS re-export) =="
bash scripts/qnap-tb-storage-ip-install.sh
echo
echo "Done. QNAP storage configured (network + share + reconciler). Set the Thunderbolt Bridge's"
echo "static IP ${SVC_IP}/24 in the QNAP UI if this is a fresh NAS (runbook qnap-storage-setup.md §2)."
