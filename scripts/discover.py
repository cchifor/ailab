#!/usr/bin/env python3
"""Read-only discovery of the Proxmox nodes + QNAP via paramiko.
Nodes use SSH key (fallback to NODE_ROOT_PASSWORD); QNAP uses password from .env.
Writes per-host reports to docs/_generated/ and echoes them to stdout. Changes nothing.

    pip install paramiko ; python scripts/discover.py
"""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "_generated"
OUT.mkdir(parents=True, exist_ok=True)

NODE_CMD = r"""
echo "== pveversion =="; pveversion 2>/dev/null
echo "== kernel =="; uname -r
echo "== cpu/mem =="; lscpu 2>/dev/null | grep -E "Model name|^CPU\(s\)|Socket|Core|Thread"; free -h | head -2
echo "== USB4/Thunderbolt PCI =="; lspci -nn 2>/dev/null | grep -iE "usb4|thunderbolt" || echo none
echo "== thunderbolt sysfs =="; ls -1 /sys/bus/thunderbolt/devices/ 2>/dev/null || echo none
echo "== net links =="; ip -br link
echo "== net drivers/ID_PATH/MAC =="
for n in /sys/class/net/*; do
  b=$(basename "$n"); [ "$b" = lo ] && continue
  drv=$(basename "$(readlink -f "$n/device/driver" 2>/dev/null)" 2>/dev/null)
  idp=$(udevadm info "/sys/class/net/$b" 2>/dev/null | sed -n "s/.*ID_PATH=//p" | head -1)
  mac=$(cat "$n/address" 2>/dev/null); spd=$(cat "$n/speed" 2>/dev/null)
  echo "$b driver=$drv speed=${spd}M mac=$mac ID_PATH=$idp"
done
echo "== thunderbolt modules =="; lsmod | grep -i thunderbolt || echo "not loaded"
echo "== boltctl =="; boltctl list 2>/dev/null || echo "bolt not installed"
echo "== tb/usb4 dmesg =="; dmesg 2>/dev/null | grep -iE "thunderbolt|usb4" | tail -12
echo "== IOMMU =="; dmesg 2>/dev/null | grep -iE "AMD-Vi|IOMMU enabled|DMAR" | head -3
echo "== GPU =="; lspci -nn 2>/dev/null | grep -iE "VGA|Display|3D"; ls -l /dev/dri /dev/kfd 2>/dev/null || echo "no /dev/dri or /dev/kfd"
echo "== storages =="; pvesm status 2>/dev/null
echo "== cluster =="; pvecm status 2>/dev/null | sed -n '1,25p'
echo "== root disk =="; lsblk -d -o NAME,SIZE,MODEL 2>/dev/null
"""

QNAP_CMD = r"""
echo "== model/fw =="; getcfg System "Internal Model" -f /etc/config/uLinux.conf 2>/dev/null
getcfg System Version -f /etc/config/uLinux.conf 2>/dev/null
getcfg System "Build Number" -f /etc/config/uLinux.conf 2>/dev/null
echo "== uname =="; uname -a 2>/dev/null
echo "== mem =="; free -m 2>/dev/null | head -2
echo "== zpool =="; zpool list 2>/dev/null; zpool status 2>/dev/null
echo "== zfs list =="; zfs list 2>/dev/null
echo "== qcli_storage =="; qcli_storage 2>/dev/null; qcli_storage -d 2>/dev/null
echo "== nvme devices =="; ls -l /dev/nvme* 2>/dev/null
for d in /sys/class/nvme/nvme*; do [ -e "$d" ] && echo "$(basename $d): model=$(cat $d/model 2>/dev/null) fw=$(cat $d/firmware_rev 2>/dev/null)"; done
echo "== lsblk =="; lsblk -d -o NAME,SIZE,MODEL,ROTA 2>/dev/null
echo "== net =="; (ip -br addr 2>/dev/null || ifconfig -a 2>/dev/null | grep -E "Link|inet ")
echo "== thunderbolt =="; ls -l /sys/bus/thunderbolt/devices/ 2>/dev/null || echo "no tb sysfs"
echo "== nfs exports =="; cat /etc/exports 2>/dev/null || echo none
"""


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def run(client, cmd):
    _in, out, err = client.exec_command(cmd, timeout=60)
    return out.read().decode(errors="replace") + err.read().decode(errors="replace")


def main():
    import paramiko

    env = load_env(REPO / ".env")
    node_pw = env.get("NODE_ROOT_PASSWORD", "")
    nodes = [n.strip() for n in env.get("PVE_NODES", "192.168.0.2,192.168.0.3,192.168.0.4").split(",") if n.strip()]
    keyfile = pathlib.Path(os.path.expanduser("~/.ssh/id_ed25519"))
    pkey = None
    try:
        pkey = paramiko.Ed25519Key.from_private_key_file(str(keyfile))
    except Exception as e:  # noqa: BLE001
        print(f"(note: key not usable directly: {e}; using password)")

    for ip in nodes:
        print(f"\n########## NODE {ip} ##########")
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            if pkey is not None:
                c.connect(ip, username="root", pkey=pkey, timeout=12, look_for_keys=False, allow_agent=False)
            else:
                c.connect(ip, username="root", password=node_pw, timeout=12, look_for_keys=False, allow_agent=False)
        except Exception as e:  # noqa: BLE001
            print(f"   CONNECT FAILED: {e}")
            continue
        report = run(c, NODE_CMD)
        c.close()
        (OUT / f"node-{ip}.txt").write_text(report, encoding="utf-8")
        print(report)

    # ---- QNAP ----
    qhost = env.get("QNAP_SSH_HOST", "192.168.1.225")
    quser = env.get("QNAP_SSH_USER", "admin")
    qpw = env.get("QNAP_ADMIN_PASSWORD", "")
    print(f"\n########## QNAP {quser}@{qhost} ##########")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(qhost, username=quser, password=qpw, timeout=12, look_for_keys=False, allow_agent=False)
        report = run(c, QNAP_CMD)
        c.close()
    except Exception as e:  # noqa: BLE001
        report = f"   QNAP CONNECT FAILED: {e}\n   (check SSH enabled, user/pass, MFA)"
    (OUT / "qnap-state.txt").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    try:
        import paramiko  # noqa: F401
    except ImportError:
        sys.exit("paramiko not installed -> pip install paramiko")
    main()
