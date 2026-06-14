#!/usr/bin/env bash
# Read-only inventory of the Proxmox nodes + QNAP -> docs/_generated/.
# Safe: only runs read commands. Requires SSH access (see runbook 00).
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_DIR/docs/_generated"
mkdir -p "$OUT"

NODES=("192.168.0.2" "192.168.0.3" "192.168.0.4")
SSH="ssh -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new"

for ip in "${NODES[@]}"; do
  echo "==> node $ip"
  f="$OUT/node-$ip.txt"
  {
    echo "### node $ip  $(date -u +%FT%TZ)"
    # shellcheck disable=SC2016
    $SSH "root@$ip" '
      echo "== pveversion =="; pveversion 2>/dev/null;
      echo "== kernel =="; uname -r;
      echo "== USB4/Thunderbolt PCI =="; lspci -nn | grep -iE "usb4|thunderbolt" || echo none;
      echo "== net links =="; ip -br link;
      echo "== netdev drivers / ID_PATH (for .link Path=) ==";
      for n in /sys/class/net/*; do
        b=$(basename "$n"); [ "$b" = "lo" ] && continue;
        drv=$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null);
        idp=$(udevadm info "/sys/class/net/$b" 2>/dev/null | sed -n "s/.*ID_PATH=//p" | head -1);
        mac=$(cat "$n/address" 2>/dev/null);
        echo "$b driver=$drv mac=$mac ID_PATH=$idp";
      done;
      echo "== thunderbolt modules =="; lsmod | grep -i thunderbolt || echo "not loaded";
      echo "== boltctl =="; boltctl list 2>/dev/null || echo "bolt not installed";
      echo "== IOMMU =="; dmesg 2>/dev/null | grep -iE "AMD-Vi|IOMMU" | head -3;
      echo "== GPU dev nodes =="; ls -l /dev/dri /dev/kfd 2>/dev/null || echo none;
      echo "== storages =="; pvesm status 2>/dev/null || true;
    '
  } > "$f" 2>&1
  echo "   -> $f"
done

echo "==> QNAP (read-only)"
bash "$REPO_DIR/scripts/qnap-api.sh" ssh > "$OUT/qnap-state.txt" 2>&1 \
  || bash "$REPO_DIR/scripts/qnap-api.sh" inventory > "$OUT/qnap-state.txt" 2>&1 || true
echo "   -> $OUT/qnap-state.txt"

cat <<'EOF'

Next steps:
  1. Read docs/_generated/node-*.txt and set in ansible/host_vars/:
       - pve-node1/2: tb_pci_path = the USB4 netdev ID_PATH for the cable to the QNAP
       - pve-node3:   storage_nic_mac = MAC of the USB->2.5GbE adapter
  2. Read docs/_generated/qnap-state.txt for firmware + installed drives; agree pool geometry.
EOF
