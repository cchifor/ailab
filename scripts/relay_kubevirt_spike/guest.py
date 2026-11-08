"""Guest recipe (cloud-init) and offline measurement of an operator-downloaded containerDisk layer."""
from __future__ import annotations

import gzip
import hashlib
import pathlib
import struct
import tarfile

from .operator_bundle import DIGEST_IMAGE, BundleError

MAX_LAYER_BYTES = 2 * 1024 ** 3


def cloud_init(smoke_image: str) -> str:
    """Readiness = qemu-guest-agent + docker compose actually ran a pinned container, not VM Running."""
    if not DIGEST_IMAGE.match(smoke_image):
        raise BundleError("compose smoke image must be digest-pinned")
    text = f"""#cloud-config
package_update: true
packages: [qemu-guest-agent, docker.io, docker-compose-v2]
write_files:
  - path: /etc/docker/daemon.json
    content: '{{"data-root": "/var/lib/docker-cache/docker"}}'
  - path: /opt/relay-spike/compose.yaml
    content: |
      services:
        smoke:
          image: {smoke_image}
          restart: unless-stopped
          command: ["sh", "-c", "echo relay-spike-ok > /out/ok && while :; do sleep 3600; done"]
          volumes: ["/workspace/relay-spike:/out"]
  - path: /usr/local/sbin/relay-spike-ready
    permissions: '0755'
    content: |
      #!/bin/sh
      set -eu
      mountpoint -q /workspace && mountpoint -q /var/lib/docker-cache
      docker compose -f /opt/relay-spike/compose.yaml ps --status running --services | grep -qx smoke
      grep -qx relay-spike-ok /workspace/relay-spike/ok
bootcmd:
  # Every boot: the emptyDisk cache is blank per VMI; the workspace keeps its filesystem.
  - [sh, -c, 'for d in workspace dockercache; do test -b /dev/disk/by-id/virtio-$d || exit 1; done']
  - [sh, -c, 'blkid /dev/disk/by-id/virtio-workspace || mkfs.ext4 -L workspace /dev/disk/by-id/virtio-workspace']
  - [sh, -c, 'blkid /dev/disk/by-id/virtio-dockercache || mkfs.ext4 -L dockercache /dev/disk/by-id/virtio-dockercache']
  - [sh, -c, 'mkdir -p /workspace /var/lib/docker-cache && mount /dev/disk/by-id/virtio-workspace /workspace && mount /dev/disk/by-id/virtio-dockercache /var/lib/docker-cache && mkdir -p /workspace/relay-spike']
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [systemctl, restart, docker]
  - [sh, -c, 'rm -f /workspace/relay-spike/ok; docker compose -f /opt/relay-spike/compose.yaml up -d']
"""
    if len(text.encode("utf-8")) > 2048:
        raise BundleError("cloud-init inline userData exceeds KubeVirt's 2048-byte limit")
    return text


def measure_layer(blob: pathlib.Path, expected_digest: str) -> dict:
    """Hash-check a downloaded layer, then stream it: expanded tar bytes and qcow2 virtual size."""
    size = blob.stat().st_size
    if size > MAX_LAYER_BYTES:
        raise BundleError("layer blob exceeds the 2 GiB measurement bound")
    h = hashlib.sha256()
    with blob.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if "sha256:" + h.hexdigest() != expected_digest:
        raise BundleError("layer blob sha256 does not match the pinned layer digest")
    expanded, disks = 0, []
    with gzip.open(blob, "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tar:
        for m in tar:
            if not m.isfile():
                continue
            expanded += m.size
            if m.name.lstrip("./").startswith("disk/"):
                head = tar.extractfile(m).read(32)
                if head[:4] == b"QFI\xfb":
                    disks.append(("qcow2", struct.unpack(">Q", head[24:32])[0]))
                else:
                    disks.append(("raw", m.size))
    if len(disks) != 1:
        raise BundleError(f"expected exactly one disk under /disk in the layer, found {len(disks)}")
    return {"guest_image_expanded_bytes": expanded, "guest_disk_format": disks[0][0],
            "guest_disk_virtual_bytes": disks[0][1], "method": "layer-tar-qcow2-header"}
