"""S2 workload artifacts for namespace relay-kv-spike. Needs validated guest evidence; fails closed."""
from __future__ import annotations

import copy
import re

from .operator_bundle import POOL_LABEL, BundleError, SPIKE_LABEL, dedicated_placement

NS, VM, PVC = "relay-kv-spike", "kv-spike-1", "kv-spike-1-workspace"
RAW_MAX = "18G"  # qemu-img size; 18 GiB virtual, inside the 20Gi PVC
FQDN = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z]{2,}$")
# Reviewed external destinations needed by the guest recipe: Ubuntu packages (qemu-guest-agent,
# docker.io, docker-compose-v2) and the pinned smoke image on quay. Nothing on the estate.
DEFAULT_EGRESS = ("archive.ubuntu.com", "security.ubuntu.com", "quay.io", "*.quay.io")
# bash + coreutils + qemu-img only: those are what v1.9.1 rpm/BUILD.bazel launcherbase ships.
PREP_SCRIPT = f"""set -eu
img=/workspace/disk.img
if [ ! -e "$img" ]; then qemu-img create -f raw "$img" {RAW_MAX}; fi
info=$(qemu-img info --output=json "$img")
printf '%s\\n' "$info"
[[ $info =~ \\"format\\":\\ \\"raw\\" ]] || {{ echo 'not raw' >&2; exit 1; }}
[[ $info =~ \\"virtual-size\\":\\ ([0-9]+) ]] || {{ echo 'no virtual-size' >&2; exit 1; }}
vs=${{BASH_REMATCH[1]}}
(( vs <= {18 * 1024 ** 3} )) || {{ echo "virtual size $vs too large" >&2; exit 1; }}
[ -r "$img" ] && [ -w "$img" ] || {{ echo "disk.img not read/write for uid $(id -u)" >&2; exit 1; }}
echo "prep-ok uid=$(id -u) virtual-size=$vs"
"""


def _meta(name, **extra):
    return {"name": name, "namespace": NS, "labels": dict(SPIKE_LABEL)} | extra


def _restricted(uid: int) -> dict:
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"}}


def render(ev: dict, launcher_image: str, cloud_init: str, egress: tuple[str, ...] = DEFAULT_EGRESS) -> list[dict]:
    for host in egress:
        if not FQDN.match(host) or host.endswith((".local", ".lan", "chifor.me")):
            raise BundleError(f"egress destination {host!r} is not a reviewed public FQDN")
    place = dedicated_placement()
    uid = ev["launcher_uid"]
    res = {"cpu": "4", "memory": "8Gi", "ephemeral-storage": "30Gi"}
    prep_res = {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "64Mi"}
    docs = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS, "labels": dict(SPIKE_LABEL) | {
            # KubeVirt masquerade needs NET_ADMIN in virt-launcher, forbidden by baseline.
            # This exception is scoped to the disposable spike namespace and dedicated pool.
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/audit": "restricted"}}},
        {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": _meta("kv-spike-quota"), "spec": {"hard": {
            "requests.cpu": "6", "requests.memory": "12Gi", "limits.memory": "12Gi",
            "requests.storage": "20Gi", "persistentvolumeclaims": "1",
            "requests.ephemeral-storage": "32Gi", "limits.ephemeral-storage": "32Gi"}}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": _meta("default-deny"),
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        {"apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy", "metadata": _meta("kv-spike-launcher-egress"),
         "spec": {"endpointSelector": {"matchLabels": {"kubevirt.io": "virt-launcher"}},
                  # Deny wins over allow: an FQDN that resolves into the estate LAN/tailnet stays blocked.
                  # (Cluster 10.x pod/service ranges are NOT denied here: kube-dns lives there.)
                  "egressDeny": [{"toCIDR": ["192.168.0.0/23", "172.16.0.0/12", "100.64.0.0/10"]}],
                  "egress": [
                      {"toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system",
                                                        "k8s-app": "kube-dns"}}],
                       "toPorts": [{"ports": [{"port": "53", "protocol": "ANY"}],
                                    "rules": {"dns": [{"matchPattern": "*"}]}}]},
                      {"toFQDNs": [{"matchPattern": h} if h.startswith("*") else {"matchName": h} for h in egress],
                       "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}, {"port": "80", "protocol": "TCP"}]}]}]}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": _meta(PVC),
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local-path", "volumeMode": "Filesystem",
                  "resources": {"requests": {"storage": "20Gi"}}}},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": _meta("kv-spike-1-prep"),
         "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 600, "template": {
             "metadata": {"labels": dict(SPIKE_LABEL)},
             "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
                      "securityContext": {"fsGroup": uid, "seccompProfile": {"type": "RuntimeDefault"}},
                      **copy.deepcopy(place),
                      "containers": [{"name": "qemu-img", "image": launcher_image,
                                      "command": ["/usr/bin/bash", "-c", PREP_SCRIPT],
                                      "securityContext": _restricted(uid),
                                      "resources": {"requests": prep_res, "limits": prep_res},
                                      "volumeMounts": [{"name": "ws", "mountPath": "/workspace"}]}],
                      "volumes": [{"name": "ws", "persistentVolumeClaim": {"claimName": PVC}}]}}}},
        {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine", "metadata": _meta(VM),
         "spec": {"runStrategy": "Halted", "template": {"metadata": {"labels": dict(SPIKE_LABEL)}, "spec": {
             **copy.deepcopy(place), "evictionStrategy": "None", "terminationGracePeriodSeconds": 60,
             "domain": {"cpu": {"cores": 4, "sockets": 1, "threads": 1}, "memory": {"guest": "8Gi"},
                        "resources": {"requests": dict(res), "limits": dict(res)},
                        "devices": {"autoattachGraphicsDevice": False, "autoattachSerialConsole": True,
                                    "disks": [{"name": n, "serial": n, "disk": {"bus": "virtio"}}
                                              for n in ("rootdisk", "workspace", "dockercache", "cloudinit")],
                                    "interfaces": [{"name": "default", "masquerade": {}}]}},
             "networks": [{"name": "default", "pod": {}}],
             "readinessProbe": {"exec": {"command": ["/usr/local/sbin/relay-spike-ready"]},
                                "initialDelaySeconds": 60, "periodSeconds": 30, "timeoutSeconds": 20,
                                "failureThreshold": 60},
             "volumes": [{"name": "rootdisk", "containerDisk": {"image": ev["guest_image"]}},
                         {"name": "workspace", "persistentVolumeClaim": {"claimName": PVC}},
                         {"name": "dockercache", "emptyDisk": {"capacity": "20Gi"}},
                         {"name": "cloudinit", "cloudInitNoCloud": {"userData": cloud_init}}]}}}},
    ]
    check_vm(docs[-1])
    return docs


ALLOWED_VOLUMES = {"containerDisk", "persistentVolumeClaim", "emptyDisk", "cloudInitNoCloud"}


def check_vm(vm: dict) -> None:
    spec = vm["spec"]["template"]["spec"]
    dom = spec["domain"]
    for side in ("requests", "limits"):
        r = dom.get("resources", {}).get(side, {})
        if r.get("memory") != "8Gi" or r.get("ephemeral-storage") != "30Gi" or r.get("cpu") != "4":
            raise BundleError(f"VM resources.{side} must be cpu 4 / memory 8Gi / ephemeral-storage 30Gi")
    if dom.get("memory", {}).get("guest") != "8Gi":
        raise BundleError("VM memory.guest must be 8Gi")
    for v in spec.get("volumes", []):
        kinds = set(v) - {"name"}
        if not kinds <= ALLOWED_VOLUMES:
            raise BundleError(f"forbidden VM volume type {sorted(kinds - ALLOWED_VOLUMES)}")
    devs = dom.get("devices", {})
    for forbidden in ("hostDevices", "gpus", "filesystems"):
        if devs.get(forbidden):
            raise BundleError(f"forbidden VM device {forbidden}")
    if [i for i in devs.get("interfaces", []) if set(i) != {"name", "masquerade"}] or \
            [n for n in spec.get("networks", []) if set(n) != {"name", "pod"}]:
        raise BundleError("only pod network with masquerade is allowed")
    if spec.get("evictionStrategy") != "None":
        raise BundleError("evictionStrategy must be None (no live migration)")
    if spec.get("nodeSelector", {}).get(POOL_LABEL) != "true" or spec.get("tolerations") != dedicated_placement()["tolerations"]:
        raise BundleError("VM placement escapes the dedicated pool")
    if "exec" not in spec.get("readinessProbe", {}):
        raise BundleError("VM needs an in-guest exec readiness probe")
