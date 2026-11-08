#!/usr/bin/env python3
"""Offline tests for the KubeVirt spike S2 workload render, guest evidence and layer measurement.

    python3 -m unittest scripts/tests/test_relay_kubevirt_spike_workload.py -v
"""
import copy
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import pathlib
import struct
import sys
import tarfile
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from relay_kubevirt_spike import evidence as gev, guest, workload  # noqa: E402
from relay_kubevirt_spike.operator_bundle import BundleError  # noqa: E402

_spec = importlib.util.spec_from_file_location("rks", ROOT / "scripts" / "relay-kubevirt-spike.py")
rks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rks)

PINS_PATH = ROOT / "docs/relay-workers/runtime-pins.json"
BUNDLE = json.loads(PINS_PATH.read_text())["kubevirt"]["spike_bundle"]
LAUNCHER = next(c["image"] for c in BUNDLE["components"] if c["env"] == "VIRT_LAUNCHER_IMAGE")
NOW = dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc)
GI = 1024 ** 3


def evidence(**over):
    """SYNTHETIC test evidence. Not a measurement of the real image."""
    ev = gev.template(BUNDLE["guest_candidate"]["image"], LAUNCHER)
    ev.update(measured_at="2026-11-08T20:00:00Z", measured_by="test", guest_image_expanded_bytes=4 * GI,
              guest_disk_virtual_bytes=5 * GI, guest_disk_format="qcow2", launcher_uid=107,
              launcher_uid_source="oci-config-user", method="layer-tar-qcow2-header")
    ev.update(over)
    return ev


def vm_of(docs):
    return next(d for d in docs if d["kind"] == "VirtualMachine")


class EvidenceTests(unittest.TestCase):
    def test_template_and_missing_fields_refused(self):
        with self.assertRaisesRegex(BundleError, "missing"):
            gev.validate(gev.template(BUNDLE["guest_candidate"]["image"], LAUNCHER), launcher_image=LAUNCHER, now=NOW)

    def test_stale_future_unknown_and_wrong_launcher_refused(self):
        cases = [(dict(measured_at="2026-10-01T00:00:00Z"), "stale"),
                 (dict(measured_at="2026-11-10T00:00:00Z"), "stale"),
                 (dict(measured_at="2026-11-08T20:00:00"), "timezone"),
                 (dict(extra="x"), "unknown fields"),
                 (dict(launcher_image="quay.io/kubevirt/virt-launcher:v1.9.1"), "launcher_image"),
                 (dict(launcher_uid=0), "non-root"),
                 (dict(launcher_uid=True), "launcher_uid"),
                 (dict(guest_image="quay.io/containerdisks/ubuntu:24.04"), "digest"),
                 (dict(schema="other"), "schema")]
        for over, msg in cases:
            with self.subTest(over=over), self.assertRaisesRegex(BundleError, msg):
                gev.validate(evidence(**over), launcher_image=LAUNCHER, now=NOW)

    def test_root_disk_budget(self):
        with self.assertRaisesRegex(BundleError, "allowance"):
            gev.validate(evidence(guest_disk_virtual_bytes=10 * GI), launcher_image=LAUNCHER, now=NOW)
        # The base and overlay both consume ephemeral storage, not their maximum.
        with self.assertRaisesRegex(BundleError, "allowance"):
            gev.validate(evidence(guest_disk_virtual_bytes=6 * GI), launcher_image=LAUNCHER, now=NOW)
        gev.validate(evidence(guest_disk_virtual_bytes=5 * GI), launcher_image=LAUNCHER, now=NOW)
        for field in ("guest_image_expanded_bytes", "guest_disk_virtual_bytes"):
            for value in (0, -1):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(BundleError, "positive"):
                    gev.validate(evidence(**{field: value}), launcher_image=LAUNCHER, now=NOW)


class WorkloadTests(unittest.TestCase):
    def render(self, ev=None):
        return rks.render_workload_bundle(PINS_PATH, ev or evidence(), NOW)

    def test_positive_deterministic_render(self):
        files = self.render()
        self.assertEqual(files, self.render())
        docs = list(yaml.safe_load_all(files["30-workload.yaml"]))
        # Masquerade's generated launcher requires NET_ADMIN, which baseline PSA rejects.
        self.assertEqual(docs[0]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "privileged")
        self.assertEqual(docs[0]["metadata"]["labels"]["pod-security.kubernetes.io/audit"], "restricted")
        kinds = [d["kind"] for d in docs]
        self.assertEqual(kinds, ["Namespace", "ResourceQuota", "NetworkPolicy", "CiliumNetworkPolicy",
                                 "PersistentVolumeClaim", "Job", "VirtualMachine"])
        self.assertTrue(all(d["metadata"].get("namespace", "relay-kv-spike") == "relay-kv-spike" for d in docs))
        pvc = docs[4]["spec"]
        self.assertEqual((pvc["volumeMode"], pvc["storageClassName"], pvc["resources"]["requests"]["storage"]),
                         ("Filesystem", "local-path", "20Gi"))
        job = docs[5]["spec"]["template"]["spec"]
        self.assertEqual(job["containers"][0]["image"], LAUNCHER)
        self.assertFalse(job["containers"][0]["securityContext"]["allowPrivilegeEscalation"])
        self.assertNotIn("hostPath", json.dumps(job))
        vm = vm_of(docs)["spec"]["template"]["spec"]
        self.assertEqual(vm["domain"]["resources"]["limits"], {"cpu": "4", "memory": "8Gi", "ephemeral-storage": "30Gi"})
        self.assertEqual(vm["volumes"][2]["emptyDisk"]["capacity"], "20Gi")
        self.assertIn("docker compose", vm["volumes"][3]["cloudInitNoCloud"]["userData"])
        cnp = docs[3]["spec"]
        self.assertIn("192.168.0.0/23", cnp["egressDeny"][0]["toCIDR"])
        self.assertEqual(json.loads(files["workload-metadata.json"])["status"], "rendered-not-applied-not-qualified")

    def test_guest_restart_preserves_readiness_workload(self):
        config = yaml.safe_load(guest.cloud_init(BUNDLE["compose_smoke_image"]["image"]))
        compose = yaml.safe_load(next(f["content"] for f in config["write_files"] if f["path"].endswith("compose.yaml")))
        # Docker must restart the container on an in-guest reboot: cloud-init runcmd is once-per-instance.
        self.assertEqual(compose["services"]["smoke"]["restart"], "unless-stopped")
        self.assertIn("while :", compose["services"]["smoke"]["command"][-1])

    def test_forbidden_vm_features_refused(self):
        base = vm_of(workload.render(evidence(), LAUNCHER, "#cloud-config\n"))
        mutations = [
            ("hostDisk", lambda s: s["volumes"].append({"name": "h", "hostDisk": {"path": "/x"}})),
            ("dataVolume", lambda s: s["volumes"].append({"name": "d", "dataVolume": {"name": "x"}})),
            ("hostDevices", lambda s: s["domain"]["devices"].update(hostDevices=[{"name": "x"}])),
            ("gpus", lambda s: s["domain"]["devices"].update(gpus=[{"name": "x"}])),
            ("filesystems", lambda s: s["domain"]["devices"].update(filesystems=[{"name": "x"}])),
            ("bridge", lambda s: s["domain"]["devices"]["interfaces"].__setitem__(0, {"name": "default", "bridge": {}})),
            ("multus", lambda s: s["networks"].append({"name": "m", "multus": {"networkName": "x"}})),
            ("migration", lambda s: s.update(evictionStrategy="LiveMigrate")),
            ("placement", lambda s: s["nodeSelector"].pop("ailab.io/relay-worker-pool")),
            ("missing limit", lambda s: s["domain"]["resources"]["limits"].pop("ephemeral-storage")),
            ("missing request", lambda s: s["domain"]["resources"]["requests"].pop("memory")),
            ("guest memory", lambda s: s["domain"]["memory"].update(guest="16Gi")),
            ("readiness", lambda s: s.pop("readinessProbe")),
        ]
        for name, mutate in mutations:
            vm = copy.deepcopy(base)
            mutate(vm["spec"]["template"]["spec"])
            with self.subTest(name), self.assertRaises(BundleError):
                workload.check_vm(vm)

    def test_estate_egress_destination_refused(self):
        for host in ("git.chifor.me", "192.168.0.40", "nas.local"):
            with self.subTest(host), self.assertRaises(BundleError):
                workload.render(evidence(), LAUNCHER, "#cloud-config\n", egress=(host,))

    def test_unpinned_smoke_image_and_guest_mismatch_refused(self):
        with self.assertRaisesRegex(BundleError, "digest"):
            guest.cloud_init("quay.io/prometheus/busybox:glibc")
        with self.assertRaisesRegex(BundleError, "guest candidate"):
            self.render(evidence(guest_image="quay.io/containerdisks/fedora@sha256:" + "b" * 64))


def _layer(disk: bytes, name="disk/disk.img"):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(disk)
        tar.addfile(info, io.BytesIO(disk))
    return gzip.compress(raw.getvalue(), mtime=0)


class MeasureTests(unittest.TestCase):
    def test_qcow2_header_and_hash(self):
        disk = b"QFI\xfb" + b"\0" * 20 + struct.pack(">Q", 5 * GI) + b"\0" * 64
        blob = _layer(disk)
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "layer"
            p.write_bytes(blob)
            got = guest.measure_layer(p, "sha256:" + hashlib.sha256(blob).hexdigest())
            self.assertEqual((got["guest_disk_format"], got["guest_disk_virtual_bytes"]), ("qcow2", 5 * GI))
            self.assertEqual(got["guest_image_expanded_bytes"], len(disk))
            with self.assertRaisesRegex(BundleError, "does not match"):
                guest.measure_layer(p, "sha256:" + "0" * 64)
            p.write_bytes(_layer(disk, name="other/file"))
            with self.assertRaisesRegex(BundleError, "exactly one disk"):
                guest.measure_layer(p, "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
