#!/usr/bin/env python3
"""The dsh pod specs must not ask kubelet to chown the NFS app volume on every start.

WHY THIS EXISTS. The nfs.csi.k8s.io CSIDriver is registered with
fsGroupPolicy: File. A pod that sets fsGroup with the default change policy
(Always) therefore gets a recursive ownership walk over every file of the /app
NFS volume before its sandbox is created -- ~25 minutes per rollout on
2026-09-11, with no kubelet events while it walks, on a Recreate Deployment.
fsGroup itself must stay (it is what makes the 0440 credentials Secret volume
readable by uid 1000), so the guard is the change policy, on every pod spec that
mounts the volume with fsGroup set.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DSH = ROOT / "kubernetes" / "apps" / "apps" / "dsh"
MANIFESTS = ["deployment.yaml", "install-job.yaml"]


def _pod_specs():
    for name in MANIFESTS:
        for doc in yaml.safe_load_all((DSH / name).read_text(encoding="utf-8")):
            if not doc:
                continue
            if doc.get("kind") in ("Deployment", "Job"):
                yield name, doc["spec"]["template"]["spec"]


class FsGroupWalk(unittest.TestCase):
    def test_every_fsgroup_pod_spec_uses_on_root_mismatch(self):
        seen = 0
        for name, spec in _pod_specs():
            sc = spec.get("securityContext") or {}
            if "fsGroup" not in sc:
                continue
            seen += 1
            # fsGroup is what makes the 0440 credentials Secret volume readable by
            # uid 1000; the policy guards the walk, it must not be traded for the group.
            self.assertEqual(sc.get("fsGroup"), 1000, f"{name}: fsGroup must stay 1000")
            self.assertEqual(
                sc.get("fsGroupChangePolicy"),
                "OnRootMismatch",
                f"{name}: fsGroup without fsGroupChangePolicy: OnRootMismatch re-walks the whole "
                "NFS app volume on every pod start",
            )
        self.assertEqual(seen, 2, "expected the Deployment and the install Job to set fsGroup")


if __name__ == "__main__":
    unittest.main()
