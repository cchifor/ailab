#!/usr/bin/env python3
"""Offline tests for the KubeVirt spike operator/CR renderer. No network, no cluster.

    python3 -m unittest scripts/tests/test_relay_kubevirt_spike_operator.py -v
"""
import copy
import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from relay_kubevirt_spike import operator_bundle as ob  # noqa: E402

_spec = importlib.util.spec_from_file_location("rks", ROOT / "scripts" / "relay-kubevirt-spike.py")
rks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rks)

PINS = json.loads((ROOT / "docs/relay-workers/runtime-pins.json").read_text())
PUBLIC = ROOT / ".data/public-pins"


def fixture_operator():
    """Reduced copy of the v1.9.1 operator manifest: same kinds/names, operator Deployment shape."""
    docs = [{"apiVersion": "v1", "kind": k, "metadata": {"name": n, "namespace": "kubevirt"}}
            for k, n in sorted(ob.EXPECTED) if k != "Deployment"]
    docs.append({"apiVersion": "apps/v1", "kind": "Deployment",
                 "metadata": {"name": "virt-operator", "namespace": "kubevirt"},
                 "spec": {"replicas": 2, "template": {"spec": {
                     "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                         "nodeSelectorTerms": [{"matchExpressions": [
                             {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists"}]}]}},
                         "podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}},
                     "tolerations": [{"key": "CriticalAddonsOnly", "operator": "Exists"}],
                     "nodeSelector": {"kubernetes.io/os": "linux"},
                     "containers": [{"name": "virt-operator", "image": "quay.io/kubevirt/virt-operator:v1.9.1",
                                     "env": [{"name": "VIRT_OPERATOR_IMAGE", "value": "quay.io/kubevirt/virt-operator:v1.9.1"},
                                             {"name": "WATCH_NAMESPACE", "value": ""},
                                             {"name": "KUBEVIRT_VERSION", "value": "v1.9.1"}]}]}}}})
    # Reduced structural schema for the fixture; the pinned-source test below
    # exercises the actual upstream CRD with its verified download hash.
    def schema(value):
        if isinstance(value, dict):
            return {"type": "object", "properties": {k: schema(v) for k, v in value.items()}}
        if isinstance(value, list):
            return {"type": "array", "items": schema(value[0]) if value else {"type": "string"}}
        return {"type": "integer" if type(value) is int else "string"}
    spec = ob.render_cr(yaml.safe_dump(CR))["spec"]
    crd = next(d for d in docs if d["kind"] == "CustomResourceDefinition")
    crd["spec"] = {"versions": [{"name": "v1", "served": True,
        "schema": {"openAPIV3Schema": {"type": "object", "properties": {"spec": schema(spec)}}}}]}
    return docs


CR = {"apiVersion": "kubevirt.io/v1", "kind": "KubeVirt", "metadata": {"name": "kubevirt", "namespace": "kubevirt"},
      "spec": {"configuration": {"developerConfiguration": {"featureGates": []}}}}


_LIVE = []


class Fixture:
    def __init__(self, operator=None, cr=None, mutate_pins=None):
        self.tmp = tempfile.TemporaryDirectory()
        _LIVE.append(self.tmp)
        d = pathlib.Path(self.tmp.name)
        texts = {"kubevirt-operator-v1.9.1.yaml": yaml.safe_dump_all(operator or fixture_operator()),
                 "kubevirt-cr-v1.9.1.yaml": yaml.safe_dump(cr or CR)}
        pins = copy.deepcopy(PINS)
        b = pins["kubevirt"]["spike_bundle"]
        b["sources"] = [{"file": n, "sha256": hashlib.sha256(t.encode()).hexdigest()} for n, t in texts.items()]
        for n, t in texts.items():
            (d / n).write_text(t)
        if mutate_pins:
            mutate_pins(pins)
        self.dir, self.pins = d, d / "pins.json"
        self.pins.write_text(json.dumps(pins))

    def render(self):
        return rks.render_operator_bundle(self.pins, self.dir)


def placement_ok(pod):
    sel = pod["nodeSelector"]
    exprs = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    return (sel.get(ob.POOL_LABEL) == "true" and len(exprs) == 1 and pod["tolerations"] == [ob.TOLERATION]
            and {e["key"] for e in exprs[0]["matchExpressions"] if e["operator"] == "DoesNotExist"}
            == set(ob.FORBIDDEN_NODE_LABELS))


class OperatorBundleTests(unittest.TestCase):
    def tearDown(self):
        while _LIVE:
            _LIVE.pop().cleanup()

    def test_placement_escape_refused(self):
        files = Fixture().render()
        docs = list(yaml.safe_load_all(files["10-operator.yaml"]))
        cr = yaml.safe_load(files["20-kubevirt-cr.yaml"])
        escapes = [
            lambda pod: pod["tolerations"].append({"key": "node-role.kubernetes.io/control-plane", "operator": "Exists"}),
            lambda pod: pod["nodeSelector"].pop(ob.POOL_LABEL),
            lambda pod: pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"].append(
                {"matchExpressions": [{"key": "ailab.io/agent-pool", "operator": "Exists"}]}),
        ]
        for escape in escapes:
            d2 = copy.deepcopy(docs)
            escape(next(d for d in d2 if d["kind"] == "Deployment")["spec"]["template"]["spec"])
            with self.assertRaisesRegex(ob.BundleError, "escapes"):
                ob.check_rendered(d2, cr)
            for part in ("infra", "workloads"):
                c2 = copy.deepcopy(cr)
                escape(c2["spec"][part]["nodePlacement"])
                with self.assertRaisesRegex(ob.BundleError, "escapes"):
                    ob.check_rendered(docs, c2)

    def test_fixture_render_pins_and_places(self):
        files = Fixture().render()
        docs = list(yaml.safe_load_all(files["10-operator.yaml"]))
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        c = pod["containers"][0]
        env = {e["name"]: e.get("value") for e in c["env"]}
        self.assertEqual(c["image"], PINS["kubevirt"]["operator_image"])
        self.assertEqual(env["VIRT_OPERATOR_IMAGE"], c["image"])
        for name in ob.REQUIRED_ENVS:
            self.assertRegex(env[name], ob.DIGEST_IMAGE)
        self.assertTrue(placement_ok(pod))
        self.assertNotIn("CriticalAddonsOnly", json.dumps(pod["tolerations"]))
        cr = yaml.safe_load(files["20-kubevirt-cr.yaml"])
        for part in ("infra", "workloads"):
            self.assertTrue(placement_ok(cr["spec"][part]["nodePlacement"]))
        self.assertEqual(cr["spec"]["infra"]["replicas"], 1)
        self.assertIn("Template", cr["spec"]["configuration"]["developerConfiguration"]["disabledFeatureGates"])
        self.assertEqual(Fixture().render(), files)  # deterministic

    def test_unknown_cr_paths_and_missing_schema_refused(self):
        files = Fixture().render()
        docs = list(yaml.safe_load_all(files["10-operator.yaml"]))
        cr = yaml.safe_load(files["20-kubevirt-cr.yaml"])
        cr["spec"]["configuration"]["developerConfiguration"]["inventedGate"] = []
        with self.assertRaisesRegex(ob.BundleError, "inventedGate.*unknown field"):
            ob.check_rendered(docs, cr)
        cr.pop("spec")
        cr["spec"] = yaml.safe_load(files["20-kubevirt-cr.yaml"])["spec"]
        crd = next(d for d in docs if d["kind"] == "CustomResourceDefinition")
        props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        del props["configuration"]["properties"]["developerConfiguration"]["properties"]["disabledFeatureGates"]
        with self.assertRaisesRegex(ob.BundleError, "disabledFeatureGates.*unknown field"):
            ob.check_rendered(docs, cr)
        crd.pop("spec")
        with self.assertRaisesRegex(ob.BundleError, "no served spec schema"):
            ob.check_rendered(docs, cr)

    def test_hash_substitution_refused(self):
        f = Fixture()
        p = f.dir / "kubevirt-operator-v1.9.1.yaml"
        p.write_text(p.read_text().replace("v1.9.1", "v1.9.2"))
        with self.assertRaisesRegex(ob.BundleError, "sha256 mismatch"):
            f.render()

    def test_unknown_resource_refused(self):
        docs = fixture_operator() + [{"kind": "DaemonSet", "metadata": {"name": "extra"}}]
        with self.assertRaisesRegex(ob.BundleError, "unsupported"):
            Fixture(operator=docs).render()

    def test_unpinned_or_missing_component_refused(self):
        def tag_only(p):
            p["kubevirt"]["spike_bundle"]["components"][4]["image"] = "quay.io/kubevirt/virt-launcher:v1.9.1"
        with self.assertRaisesRegex(ob.BundleError, "not digest-pinned"):
            Fixture(mutate_pins=tag_only).render()

        def drop(p):
            del p["kubevirt"]["spike_bundle"]["components"][-1]
        with self.assertRaisesRegex(ob.BundleError, "incomplete"):
            Fixture(mutate_pins=drop).render()

    def test_cr_enabling_gates_or_emulation_refused(self):
        for dev in ({"featureGates": ["HostDisk"]}, {"featureGates": [], "useEmulation": True}):
            cr = copy.deepcopy(CR)
            cr["spec"]["configuration"]["developerConfiguration"] = dev
            with self.assertRaisesRegex(ob.BundleError, "gates or emulation"):
                Fixture(cr=cr).render()

    def test_changed_operator_env_refused(self):
        docs = fixture_operator()
        docs[-1]["spec"]["template"]["spec"]["containers"][0]["env"].pop()
        with self.assertRaisesRegex(ob.BundleError, "env changed"):
            Fixture(operator=docs).render()

    @unittest.skipUnless((PUBLIC / "kubevirt-operator-v1.9.1.yaml").is_file(), "upstream downloads absent")
    def test_exact_upstream_source_renders(self):
        files = rks.render_operator_bundle(ROOT / "docs/relay-workers/runtime-pins.json", PUBLIC)
        self.assertEqual(len(list(yaml.safe_load_all(files["10-operator.yaml"]))), len(ob.EXPECTED))
        self.assertNotIn(":v1.9.1\n", files["10-operator.yaml"].split("kind: Deployment")[-1])


class _Resp:
    def __init__(self, body, header):
        self.body, self.headers = body, {"Docker-Content-Digest": header}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return self.body[:n]


class PinCheckTests(unittest.TestCase):
    def test_moved_tag_fails_and_errors_are_unknown(self):
        from relay_kubevirt_spike import pins_check
        bundle = copy.deepcopy(PINS["kubevirt"]["spike_bundle"])
        body = b'{"manifests": []}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        rows = pins_check.check(bundle, opener=lambda req, timeout: _Resp(body, digest))
        self.assertTrue(rows and all(r["status"] == "fail" for r in rows))
        bundle["components"][0]["image"] = "quay.io/kubevirt/virt-operator@" + digest
        rows = pins_check.check(bundle, opener=lambda req, timeout: _Resp(body, digest))
        self.assertEqual(rows[0]["status"], "pass")
        rows = pins_check.check(bundle, opener=lambda req, timeout: _Resp(body, "sha256:" + "0" * 64))
        self.assertEqual(rows[0]["status"], "unknown")  # header/body disagreement is never a pass

        def boom(req, timeout):
            raise OSError("offline")
        self.assertTrue(all(r["status"] == "unknown" for r in pins_check.check(bundle, opener=boom)))
        seen = []

        def capture(req, timeout):
            seen.append(req)
            return _Resp(body, digest)
        pins_check.check(bundle, opener=capture)
        self.assertTrue(all(r.get_header("User-agent") == pins_check.USER_AGENT for r in seen))
        self.assertTrue(all(r.full_url.startswith("https://quay.io/v2/") for r in seen))
        self.assertFalse(any(r.has_header("Authorization") for r in seen))


if __name__ == "__main__":
    unittest.main()
