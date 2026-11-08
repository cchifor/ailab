#!/usr/bin/env python3
"""Offline tests for scripts/relay-worker-preflight.py. No cluster, no kubectl: `run` is stubbed.

    python3 -m unittest scripts/tests/test_relay_worker_preflight.py -v
"""
import copy
import importlib.util
import json
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rwp", ROOT / "scripts" / "relay-worker-preflight.py")
rwp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwp)

SNAP = json.loads((ROOT / "scripts/tests/fixtures/relay-worker-preflight/snapshot-repo-derived.json").read_text())
BIND = json.loads((ROOT / "docs/relay-workers/ailab-binding.example.json").read_text())
UID = "00000000-0000-4000-8000-000000000001"
DIGEST = "sha256:" + "a" * 64


def status(report, cid):
    return next(c for c in report["checks"] if c["id"] == cid)["status"]


def with_pool(snap, mem="30Gi", cpu="12", kvm=None, ready=True, taint=True, extra_labels=None):
    """Add a dedicated relay-worker node (a hypothetical future pool; not the live estate)."""
    s = copy.deepcopy(snap)
    labels = {"kubernetes.io/arch": "amd64", "ailab.io/relay-worker-pool": "true"}
    labels.update(extra_labels or {})
    alloc = {"cpu": cpu, "memory": mem, "ephemeral-storage": "100Gi"}
    if kvm:
        alloc["devices.kubevirt.io/kvm"] = kvm
    s["queries"]["nodes"]["data"]["items"].append({
        "metadata": {"name": "relay-worker-1", "labels": labels},
        "spec": {"taints": [{"key": "dedicated", "value": "relay-worker", "effect": "NoSchedule"}] if taint else [],
                 "unschedulable": False},
        "status": {"allocatable": alloc, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                   "nodeInfo": {"architecture": "amd64"}}})
    return s


def pinned(binding, **pins):
    b = copy.deepcopy(binding)
    b["cluster"]["expected_kube_system_uid"] = UID
    b["pins"].update(pins)
    return b


class Quantities(unittest.TestCase):
    def test_malformed_list_is_not_an_empty_cluster(self):
        for name in ("nodes", "pods", "storageclasses", "runtimeclasses", "kubevirt_cr"):
            for response in ({}, {"items": None}, {"items": ""}, {"items": [None]}, []):
                with self.subTest(name=name, response=response), self.assertRaises(ValueError):
                    rwp._reduce(name, response)
            self.assertEqual(rwp._reduce(name, {"items": []}), {"items": []})

    def test_parse(self):
        self.assertEqual(rwp.quantity("500m"), 0.5)
        self.assertEqual(rwp.quantity("2Gi"), 2 * 2**30)
        self.assertEqual(rwp.quantity("1G"), 1e9)
        self.assertEqual(rwp.quantity(3), 3)
        with self.assertRaises(ValueError):
            rwp.quantity("lots")


class RepoDerivedState(unittest.TestCase):
    """The estate as the manifests describe it: no dedicated pool, no KubeVirt."""

    def setUp(self):
        self.r = rwp.evaluate(SNAP, BIND)

    def test_overall_fail_and_never_qualified(self):
        self.assertEqual(self.r["result"], "fail")
        self.assertEqual(rwp.exit_code(self.r), 1)
        self.assertEqual(self.r["qualification"], "not-established-by-preflight")
        self.assertNotIn("qualified", json.dumps(self.r["gates"]))

    def test_agent_nodes_are_not_a_dedicated_pool(self):
        self.assertEqual(status(self.r, "placement.dedicated_pool"), "fail")
        self.assertEqual(status(self.r, "headroom.developer_vm"), "fail")

    def test_gvisor_on_agent_pool_rejected(self):
        self.assertEqual(status(self.r, "runtimeclass.gvisor"), "fail")

    def test_kubevirt_absent_and_unpinned(self):
        self.assertEqual(status(self.r, "kubevirt.installed"), "fail")
        self.assertEqual(status(self.r, "kubevirt.pin"), "unknown")

    def test_capabilities_come_only_from_binding(self):
        self.assertTrue(all(v["status"] in ("pending", "unsupported") for v in self.r["capabilities"].values()))


class Positive(unittest.TestCase):
    def test_dedicated_pool_with_headroom_opens_spike_gate_only(self):
        s = with_pool(SNAP)
        r = rwp.evaluate(s, pinned(BIND, kubevirt_version="v1.0.0-example", kubevirt_operator_digest=DIGEST))
        for cid in ("context.cluster_uid", "placement.dedicated_pool", "headroom.lightweight",
                    "headroom.developer_vm", "kubevirt.pin"):
            self.assertEqual(status(r, cid), "pass", cid)
        self.assertEqual(r["gates"]["kubevirt_spike_prerequisites"]["status"], "pass")
        self.assertEqual(r["gates"]["lightweight_canary_prerequisites"]["status"], "pass")
        # still not a qualification: capabilities untouched, KubeVirt not installed
        self.assertEqual(r["capabilities"]["vm.kubevirt"]["status"], "pending")
        self.assertEqual(status(r, "kubevirt.installed"), "fail")

    def test_kubevirt_presence_is_not_qualification(self):
        s = with_pool(SNAP, kvm="1k")
        s["queries"]["kubevirt_crd"] = {"status": "ok", "data": {"metadata": {"name": "virtualmachines.kubevirt.io"}}}
        s["queries"]["kubevirt_cr"] = {"status": "ok", "data": {"items": [
            {"metadata": {"name": "kubevirt", "namespace": "kubevirt"},
             "status": {"phase": "Deployed", "observedKubeVirtVersion": "v1.0.0-example"}}]}}
        r = rwp.evaluate(s, pinned(BIND, kubevirt_version="v1.0.0-example", kubevirt_operator_digest=DIGEST))
        self.assertEqual(status(r, "kubevirt.installed"), "pass")
        self.assertEqual(status(r, "kubevirt.kvm_device"), "pass")
        self.assertEqual(r["capabilities"]["vm.kubevirt"]["status"], "pending")


class Negative(unittest.TestCase):
    def test_extra_blocking_taints_keep_dedicated_nodes_unusable(self):
        for effect in ("NoSchedule", "NoExecute"):
            s = with_pool(SNAP)
            s["queries"]["nodes"]["data"]["items"][-1]["spec"]["taints"].append({
                "key": "node.kubernetes.io/disk-pressure", "effect": effect,
            })
            r = rwp.evaluate(s, pinned(BIND))
            self.assertEqual(status(r, "placement.dedicated_pool"), "fail")
            for gate in r["gates"].values():
                self.assertEqual(gate["status"], "fail")

    def test_wrong_cluster(self):
        s = copy.deepcopy(SNAP)
        s["api_server"] = "https://192.168.0.99:6443"
        self.assertEqual(status(rwp.evaluate(s, BIND), "context.api_server"), "fail")

    def test_wrong_context_name(self):
        s = copy.deepcopy(SNAP)
        s["context"] = "home-lab"
        self.assertEqual(status(rwp.evaluate(s, BIND), "context.name"), "fail")

    def test_cluster_uid_mismatch(self):
        b = pinned(BIND)
        b["cluster"]["expected_kube_system_uid"] = "different"
        self.assertEqual(status(rwp.evaluate(SNAP, b), "context.cluster_uid"), "fail")

    def test_insufficient_headroom(self):
        s = with_pool(SNAP, mem="8Gi")
        r = rwp.evaluate(s, pinned(BIND))
        self.assertEqual(status(r, "headroom.lightweight"), "pass")
        self.assertEqual(status(r, "headroom.developer_vm"), "fail")

    def test_existing_requests_consume_headroom(self):
        s = with_pool(SNAP, mem="12Gi")
        s["queries"]["pods"]["data"]["items"].append({"metadata": {"namespace": "x"}, "spec": {
            "nodeName": "relay-worker-1", "overhead": {"memory": "160Mi"},
            "containers": [{"kind": "containers", "requests": {"memory": "2Gi"}},
                           {"kind": "initContainers", "requests": {"memory": "1Gi"}}]},
            "status": {"phase": "Running"}})
        self.assertEqual(status(rwp.evaluate(s, pinned(BIND)), "headroom.developer_vm"), "fail")

    def test_untainted_or_notready_or_agent_labelled_pool_rejected(self):
        for kw in ({"taint": False}, {"ready": False}, {"extra_labels": {"ailab.io/agent-pool": "true"}}):
            r = rwp.evaluate(with_pool(SNAP, **kw), BIND)
            self.assertEqual(status(r, "placement.dedicated_pool"), "fail", kw)

    def test_version_drift(self):
        s = copy.deepcopy(SNAP)
        s["queries"]["version"]["data"]["serverVersion"]["gitVersion"] = "v1.36.0"
        s["queries"]["cilium_ds"]["data"]["images"] = ["quay.io/cilium/cilium:v1.18.14"]
        r = rwp.evaluate(s, BIND)
        self.assertEqual(status(r, "version.kubernetes"), "fail")
        self.assertEqual(status(r, "version.cilium"), "fail")

    def test_forbidden_storage_class(self):
        b = copy.deepcopy(BIND)
        b["storage"]["workspace_storage_class"] = "qnap-iscsi"
        self.assertEqual(status(rwp.evaluate(SNAP, b), "storage.workspace_class"), "fail")

    def test_qualified_without_receipt_rejected(self):
        b = copy.deepcopy(BIND)
        b["capabilities"]["vm.compose"] = {"status": "qualified"}
        r = rwp.evaluate(SNAP, b)
        self.assertEqual(r["result"], "fail")
        self.assertTrue(any("receipt_sha256" in e for e in r["binding_errors"]))

    def test_unknown_capability_status_rejected(self):
        b = copy.deepcopy(BIND)
        b["capabilities"]["pod.restricted"] = {"status": "ready"}
        self.assertEqual(rwp.evaluate(SNAP, b)["result"], "fail")

    def test_bad_snapshot_schema(self):
        s = copy.deepcopy(SNAP)
        s["schema"] = "other"
        self.assertEqual(rwp.evaluate(s, BIND)["result"], "fail")


class Unknown(unittest.TestCase):
    def test_forbidden_queries_are_unknown_not_pass(self):
        s = with_pool(SNAP)
        for q in ("pods", "storageclasses", "runtimeclasses", "kube_system_ns", "version", "cilium_ds",
                  "kubevirt_crd", "kubevirt_cr"):
            s["queries"][q] = {"status": "forbidden", "detail": "Error from server (Forbidden)"}
        r = rwp.evaluate(s, pinned(BIND, kubevirt_version="v1", kubevirt_operator_digest=DIGEST))
        for cid in ("headroom.lightweight", "storage.workspace_class", "runtimeclass.gvisor",
                    "context.cluster_uid", "version.kubernetes", "version.cilium"):
            self.assertEqual(status(r, cid), "unknown", cid)
        self.assertEqual(r["result"], "unknown")
        self.assertEqual(rwp.exit_code(r), 2)
        self.assertEqual(r["gates"]["kubevirt_spike_prerequisites"]["status"], "unknown")

    def test_missing_context_everything_unknown(self):
        s = {"schema": rwp.SNAPSHOT_SCHEMA, "context": "admin@ai", "context_exists": False,
             "api_server": None, "queries": {}}
        r = rwp.evaluate(s, BIND)
        self.assertNotIn("pass", {c["status"] for c in r["checks"]})
        self.assertEqual(status(r, "placement.dedicated_pool"), "unknown")
        self.assertEqual(status(r, "kubevirt.installed"), "unknown")

    def test_kubevirt_cr_forbidden(self):
        s = copy.deepcopy(SNAP)
        s["queries"]["kubevirt_crd"] = {"status": "ok", "data": {"metadata": {"name": "virtualmachines.kubevirt.io"}}}
        s["queries"]["kubevirt_cr"] = {"status": "forbidden"}
        self.assertEqual(status(rwp.evaluate(s, BIND), "kubevirt.installed"), "unknown")


class FakeRun:
    """Records argv; answers config lookups and per-query fixtures."""

    def __init__(self, contexts=("admin@ai",), fail=None):
        self.calls, self.contexts, self.fail = [], contexts, fail or {}

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        if argv[1:3] == ["config", "get-contexts"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.contexts) + "\n", "")
        if argv[1:3] == ["config", "view"]:
            return subprocess.CompletedProcess(argv, 0, "https://192.168.0.40:6443", "")
        name = next(k for k, v in rwp.QUERIES.items() if argv[-len(v):] == v)
        if name in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail[name])
        q = SNAP["queries"][name]
        if q["status"] != "ok":
            return subprocess.CompletedProcess(argv, 1, "", 'Error from server (NotFound): x')
        data = q["data"]
        if name == "cilium_ds":
            data = {"spec": {"template": {"spec": {"containers": [
                {"image": i, "env": [{"name": "SECRET_SENTINEL", "value": "leak"}]} for i in data["images"]]}}}}
        if name == "pods":
            data = copy.deepcopy(data)
            for p in data["items"]:
                p["metadata"]["annotations"] = {"x": "SECRET_SENTINEL"}
                p["spec"]["containers"] = [{"name": "c", "env": [{"value": "SECRET_SENTINEL"}],
                                            "resources": {"requests": c["requests"]}} for c in p["spec"]["containers"]]
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")


class Collector(unittest.TestCase):
    def test_requires_context(self):
        with self.assertRaises(ValueError):
            rwp.collect("", run=FakeRun())

    def test_absent_context_runs_no_cluster_query(self):
        fr = FakeRun(contexts=("home-lab",))
        snap = rwp.collect("admin@ai", run=fr)
        self.assertFalse(snap["context_exists"])
        self.assertEqual(snap["queries"], {})
        self.assertTrue(all(a[1] == "config" for a in fr.calls))

    def test_every_call_pins_context_and_is_read_only(self):
        fr = FakeRun()
        snap = rwp.collect("admin@ai", run=fr)
        for argv in fr.calls:
            if argv[1] == "config":
                self.assertNotIn("--raw", argv)
                continue
            self.assertEqual(argv[1:3], ["--context", "admin@ai"])
            self.assertIn(argv[3], ("get", "version"))
            self.assertFalse({a.lower() for a in argv} & {"secret", "secrets", "exec", "port-forward", "proxy"})
        self.assertNotIn("SECRET_SENTINEL", json.dumps(snap))
        self.assertEqual(snap["queries"]["kubevirt_crd"]["status"], "notfound")

    def test_collected_snapshot_evaluates_like_fixture(self):
        snap = rwp.collect("admin@ai", run=FakeRun())
        a = {c["id"]: c["status"] for c in rwp.evaluate(snap, BIND)["checks"]}
        b = {c["id"]: c["status"] for c in rwp.evaluate(SNAP, BIND)["checks"]}
        self.assertEqual(a, b)

    def test_forbidden_classified(self):
        snap = rwp.collect("admin@ai", run=FakeRun(fail={"pods": 'Error from server (Forbidden): pods is forbidden'}))
        self.assertEqual(snap["queries"]["pods"]["status"], "forbidden")
        self.assertEqual(status(rwp.evaluate(with_pool(snap), pinned(BIND)), "headroom.lightweight"), "unknown")

    def test_query_allowlist_has_no_forbidden_verbs(self):
        for argv in rwp.QUERIES.values():
            rwp._check_argv(argv)
        for bad in (["get", "secrets"], ["exec", "x"], ["get", "pods", "--raw"], ["port-forward", "x"]):
            with self.assertRaises(RuntimeError):
                rwp._check_argv(bad)


class BindingExample(unittest.TestCase):
    def test_example_is_valid_and_conservative(self):
        self.assertEqual(rwp.validate_binding(BIND), [])
        self.assertNotIn("qualified", {v["status"] for v in BIND["capabilities"].values()})
        self.assertNotIn("agentforge", json.dumps(BIND).lower())
        self.assertNotIn("colocated_gvisor_qualified", BIND["placement"])
        self.assertIsNone(BIND["pins"]["kubevirt_operator_digest"])


def add_pod(snap, node, ns="x", phase="Running", containers=(), pod_requests=None, overhead=None):
    s = copy.deepcopy(snap)
    spec = {"nodeName": node, "overhead": overhead or {},
            "containers": [dict({"restartPolicy": None}, **c) for c in containers]}
    if pod_requests is not None:
        spec["resources"] = {"requests": pod_requests}
    s["queries"]["pods"]["data"]["items"].append(
        {"metadata": {"namespace": ns}, "spec": spec, "status": {"phase": phase}})
    return s


class ReviewRegressions(unittest.TestCase):
    # (1) stderr / context resolution
    def test_raw_stderr_never_persisted(self):
        leak = "error: exec plugin: token=SECRET_SENTINEL_abc rejected: Unauthorized"
        snap = rwp.collect("admin@ai", run=FakeRun(fail={"nodes": leak, "pods": "boom SECRET_SENTINEL"}))
        self.assertNotIn("SECRET_SENTINEL", json.dumps(snap))
        self.assertEqual(snap["queries"]["nodes"], {"status": "unauthenticated"})
        self.assertEqual(snap["queries"]["pods"], {"status": "error"})

    def test_context_resolution_exceptions_are_unknown(self):
        for exc in (FileNotFoundError("kubectl"), subprocess.TimeoutExpired("kubectl", 1)):
            def boom(argv, timeout=60, exc=exc):
                raise exc
            snap = rwp.collect("admin@ai", run=boom)
            self.assertFalse(snap["context_exists"])
            self.assertEqual(snap["queries"], {})
            self.assertNotIn("pass", {c["status"] for c in rwp.evaluate(snap, BIND)["checks"]})

    def test_non_url_server_output_dropped(self):
        class R(FakeRun):
            def __call__(self, argv, timeout=60):
                if argv[1:3] == ["config", "view"]:
                    return subprocess.CompletedProcess(argv, 0, "SECRET_SENTINEL garbage", "")
                return super().__call__(argv, timeout)
        snap = rwp.collect("admin@ai", run=R())
        self.assertIsNone(snap["api_server"])
        self.assertNotIn("SECRET_SENTINEL", json.dumps(snap))
        self.assertEqual(status(rwp.evaluate(snap, BIND), "context.api_server"), "unknown")

    # (2) resource accounting
    def test_sidecar_init_counts_for_whole_pod_life(self):
        pod = {"spec": {"overhead": {}, "containers": [
            {"kind": "initContainers", "restartPolicy": "Always", "requests": {"memory": "1Gi"}},
            {"kind": "initContainers", "restartPolicy": None, "requests": {"memory": "3Gi"}},
            {"kind": "containers", "requests": {"memory": "2Gi"}}]}}
        # init peak = 1 + 3 = 4Gi; steady = 2 + 1 = 3Gi; old max(app, init) logic gave 3Gi
        self.assertEqual(rwp.pod_request(pod, "memory"), 4 * 2**30)
        pod["spec"]["containers"][1]["requests"]["memory"] = "1Mi"
        self.assertEqual(rwp.pod_request(pod, "memory"), 3 * 2**30)

    def test_pod_level_requests_win(self):
        pod = {"spec": {"overhead": {"memory": "1Gi"}, "resources": {"requests": {"memory": "6Gi"}},
                        "containers": [{"kind": "containers", "requests": {"memory": "1Gi"}}]}}
        self.assertEqual(rwp.pod_request(pod, "memory"), 7 * 2**30)

    def test_pod_level_request_consumes_headroom(self):
        s = add_pod(with_pool(SNAP, mem="16Gi"), "relay-worker-1", pod_requests={"memory": "8Gi"},
                    containers=[{"kind": "containers", "requests": {"memory": "100Mi"}}])
        self.assertEqual(status(rwp.evaluate(s, pinned(BIND)), "headroom.developer_vm"), "fail")

    def test_collector_keeps_sidecar_and_pod_level_fields(self):
        raw = {"items": [{"metadata": {"namespace": "x"}, "spec": {"nodeName": "n",
               "resources": {"requests": {"cpu": "1"}, "limits": {"cpu": "2"}},
               "initContainers": [{"name": "s", "restartPolicy": "Always", "resources": {"requests": {"cpu": "1"}}}],
               "containers": [{"name": "c", "resources": {}}]}, "status": {"phase": "Running"}}]}
        red = rwp._reduce("pods", raw)["items"][0]["spec"]
        self.assertEqual(red["resources"], {"requests": {"cpu": "1"}})
        self.assertEqual(red["containers"][0]["restartPolicy"], "Always")

    def test_malformed_or_negative_pod_quantity_is_unknown(self):
        for bad in ("-1Gi", "nan", "1e400", "lots"):
            s = add_pod(with_pool(SNAP), "relay-worker-1",
                        containers=[{"kind": "containers", "requests": {"memory": bad}}])
            self.assertEqual(status(rwp.evaluate(s, pinned(BIND)), "headroom.developer_vm"), "unknown", bad)

    def test_quantity_rejects_nonfinite_and_negative(self):
        for bad in ("-1", "inf", "nan", "1e400", "-500m"):
            with self.assertRaises(ValueError, msg=bad):
                rwp.quantity(bad)

    def test_bad_headroom_rejected(self):
        for want in ({}, {"memory": "0"}, {"memory": "-1Gi"}, {"memory": "x"}, {"gpu": "1"}, "8Gi"):
            b = copy.deepcopy(BIND)
            b["headroom"]["developer_vm"] = want
            self.assertEqual(rwp.evaluate(SNAP, b)["result"], "fail", want)

    def test_wrong_binding_types_do_not_crash(self):
        for mutate in (lambda b: b.__setitem__("placement", []),
                       lambda b: b["placement"].__setitem__("min_nodes", "1"),
                       lambda b: b["placement"].__setitem__("node_selector", {}),
                       lambda b: b["placement"].__setitem__("protected_namespaces", []),
                       lambda b: b["cluster"].__setitem__("expected_context", 5),
                       lambda b: b["capabilities"].__setitem__("x", "pending"),
                       lambda b: b.__setitem__("runtime_classes", ["gvisor"])):
            b = copy.deepcopy(BIND)
            mutate(b)
            r = rwp.evaluate(SNAP, b)
            self.assertEqual(r["result"], "fail")
            self.assertTrue(r["binding_errors"])
        self.assertEqual(rwp.validate_binding([]), ["binding must be a JSON object"])

    # (3) protected workloads
    def test_protected_workload_on_dedicated_pool_fails(self):
        s = add_pod(with_pool(SNAP), "relay-worker-1", ns="relay",
                    containers=[{"kind": "containers", "requests": {}}])
        r = rwp.evaluate(s, pinned(BIND, kubevirt_version="v1", kubevirt_operator_digest=DIGEST))
        self.assertEqual(status(r, "placement.protected_workloads_absent"), "fail")
        self.assertEqual(r["gates"]["lightweight_canary_prerequisites"]["status"], "fail")
        self.assertEqual(r["gates"]["kubevirt_spike_prerequisites"]["status"], "fail")

    def test_finished_protected_pod_ignored_and_other_node_ignored(self):
        s = add_pod(with_pool(SNAP), "relay-worker-1", ns="llm-router", phase="Succeeded")
        r = rwp.evaluate(s, pinned(BIND))  # the fixture also has a relay pod on agent-node-1
        self.assertEqual(status(r, "placement.protected_workloads_absent"), "pass")

    def test_protected_workloads_unknown_when_pods_unreadable(self):
        s = with_pool(SNAP)
        s["queries"]["pods"] = {"status": "forbidden"}
        r = rwp.evaluate(s, pinned(BIND))
        self.assertEqual(status(r, "placement.protected_workloads_absent"), "unknown")

    # (4) no co-location flag
    def test_colocation_flag_rejected(self):
        b = copy.deepcopy(BIND)
        b["placement"]["colocated_gvisor_qualified"] = True
        r = rwp.evaluate(SNAP, b)
        self.assertTrue(any("dedicated-pool only" in e for e in r["binding_errors"]))

    # (7) digest shape, lightweight gate needs cilium
    def test_operator_digest_shape_enforced(self):
        for bad in ("latest", "sha256:abc", "sha256:" + "A" * 64, "x" * 64):
            r = rwp.evaluate(SNAP, pinned(BIND, kubevirt_version="v1", kubevirt_operator_digest=bad))
            self.assertTrue(any("kubevirt_operator_digest" in e for e in r["binding_errors"]), bad)

    def test_lightweight_gate_requires_cilium(self):
        s = with_pool(SNAP)
        s["queries"]["cilium_ds"] = {"status": "forbidden"}
        r = rwp.evaluate(s, pinned(BIND))
        self.assertEqual(r["gates"]["lightweight_canary_prerequisites"]["status"], "unknown")
        s["queries"]["cilium_ds"] = {"status": "ok", "data": {"images": ["quay.io/cilium/cilium:v1.18.14"]}}
        r = rwp.evaluate(s, pinned(BIND))
        self.assertEqual(r["gates"]["lightweight_canary_prerequisites"]["status"], "fail")


class Cli(unittest.TestCase):
    def test_collect_refuses_ephemeral_out(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rwp.main(["collect", "--context", "admin@ai", "--out", "/tmp/x.json"])
        self.assertEqual(rc, 2)
        self.assertIn("refusing ephemeral", err.getvalue())

    def test_evaluate_fixture_exit_fail(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = rwp.main(["evaluate", "--snapshot",
                           str(ROOT / "scripts/tests/fixtures/relay-worker-preflight/snapshot-repo-derived.json"),
                           "--binding", str(ROOT / "docs/relay-workers/ailab-binding.example.json")])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
