#!/usr/bin/env bash
# Stage the Talos nocloud disk image on each Proxmox node's local:import datastore.
# bpg's download_file can't decompress xz (the format the Talos factory serves), so we
# download + `xz -d` on each node here; the VM disk imports from local:import/<file>.
# Enables the 'import' content type on the 'local' storage (idempotent).
# Keep TALOS_VER + SCHEMATIC in sync with kubernetes/infra (`tofu output schematic_id`).
#
# Stages ONE image per invocation. Defaults = the CP/control-plane image. To stage a DIFFERENT image
# (e.g. the AgentForge v2 agent pool's Kata/gVisor image — kubernetes/infra/agent-nodes/image.tf)
# override SCHEMATIC + FILE, which coexist on local:import because FILE is distinct:
#   SCHEMATIC=$(tofu -chdir=kubernetes/infra/agent-nodes output -raw schematic_id) \
#   FILE=$(tofu -chdir=kubernetes/infra/agent-nodes output -raw agent_image_file) \
#     scripts/stage-talos-image.sh
set -euo pipefail
cd "$(dirname "$0")/.."

TALOS_VER="${TALOS_VER:-v1.11.2}"
SCHEMATIC="${SCHEMATIC:-53513e54bb39202f35694412577a6bc53d484744d35a126e5d42ef34785c0d83}"
URL="https://factory.talos.dev/image/${SCHEMATIC}/${TALOS_VER}/nocloud-amd64.raw.xz"
FILE="${FILE:-talos-${TALOS_VER}-nocloud-amd64.raw}" # import content needs .raw (not .img); override for the agent image
NODES="${NODES:-192.168.0.2 192.168.0.3 192.168.0.4}"

for ip in $NODES; do
  echo "== staging Talos image on $ip =="
  python scripts/node-ssh.py "$ip" "
    set -e
    command -v xz >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq xz-utils; }
    # enable 'import' content on the local storage (idempotent)
    cur=\$(pvesm config local 2>/dev/null | sed -n 's/^[[:space:]]*content[[:space:]]*//p')
    echo \",\$cur,\" | grep -q ',import,' || pvesm set local --content \"\${cur},import\"
    mkdir -p /var/lib/vz/import
    DST=/var/lib/vz/import/${FILE}
    if [ -f \"\$DST\" ]; then echo 'present in import'; ls -la \"\$DST\"; exit 0; fi
    if [ -f /var/lib/vz/template/iso/${FILE} ]; then
      mv -f /var/lib/vz/template/iso/${FILE} \"\$DST\"; echo 'moved from iso'; ls -la \"\$DST\"; exit 0
    fi
    cd /var/lib/vz/import
    rm -f talos-*.tmp_* talos.raw.xz 2>/dev/null || true
    echo 'downloading...'; curl -fL --retry 3 '$URL' -o talos.raw.xz
    echo 'decompressing...'; xz -d -f talos.raw.xz
    mv -f talos.raw \"\$DST\"; ls -la \"\$DST\"
  "
done
echo "Done. Talos image staged as local:import/${FILE} on all nodes."
