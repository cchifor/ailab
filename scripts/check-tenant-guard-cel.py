#!/usr/bin/env python3
"""Evaluate the agentforge-tenant-guard ValidatingAdmissionPolicy's CEL against a fixture table.

WHY THIS EXISTS. `kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml` is the ONLY
estate-side gate on what the CP bot may write into cchifor/agentforge-tenants (the tenants repo
is un-PR-gated: the CP pushes to it directly, so the reconciler's RBAC and this VAP are the whole
boundary). Every one of its rules is a CEL expression that the apiserver compiles at admission
time and NOWHERE else -- `kubectl apply --dry-run=client` parses the YAML and never evaluates the
expressions, and a `--dry-run=server` create only proves they COMPILE. Neither answers the
question that actually matters when this file is edited: *does clause N still admit the shape the
CP renders, and does it still deny the shapes it was written to deny?*

So this harness evaluates the policy the way the apiserver does -- every `spec.variables` entry in
declaration order (no hand-substituted values; that was the weakness of the throwaway harnesses
this replaces) and then every `spec.validations` expression -- against a table of admission
requests, and asserts the admit/deny verdict AND which clause produced it.

WHAT IT IS NOT. It is a fidelity approximation, not the apiserver: it uses cel-python, the object
is a fixture rather than a decoded+defaulted CRD instance, and CRD structural-schema validation
(which runs BEFORE validating admission and rejects e.g. an out-of-enum relabeling `action`) is
not modelled. It therefore proves the CEL's own logic, which is the half that lives in this repo.

USAGE:  python scripts/check-tenant-guard-cel.py        (or: just af-verify-tenant-guard)
        python scripts/check-tenant-guard-cel.py -v     list every case + its verdict
Exit 0 = every case matched its expectation. Exit 1 = a mismatch (the table names which).
"""

from __future__ import annotations

import argparse
import copy
import operator
import sys
from pathlib import Path
from typing import Any

import celpy
import yaml
from celpy import celtypes

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "kubernetes/apps/apps/agentforge/admission/tenant-guard.yaml"

# The operator-owned correlation ConfigMap the binding pins -- read from the SAME tenant-map.yaml
# the cluster runs rather than re-typed here, so a map edit cannot silently invalidate the ESO
# cases. Bound as `params` for EVERY case, so validations 12-15 evaluate for real instead of being
# stubbed out (hand-substituting them was the weakness of the throwaway harnesses this replaces).
TENANT_MAP_FILE = REPO_ROOT / "kubernetes/apps/apps/agentforge/admission/tenant-map.yaml"

CP_NS = "af-tenant-tenant-zero-platform-dev"
CP_ORG, CP_WS, CP_POOL = "tenant-zero", "platform-dev", "delivery"
# The live pool's rendered names (renderer `pool_workload_name` = af-orch-<ws>-<pool>,
# `eso_auth_sa_name` = af-eso-<ws>, `openbao_role` = af-tenant-<org>-<ws>).
CP_WORKLOAD = f"af-orch-{CP_WS}-{CP_POOL}"
CP_ESO_SA = f"af-eso-{CP_WS}"
CP_CREDS_SECRET = f"af-creds-{CP_WS}-{CP_POOL}"

# The CP's own pool labels (agentforge-platform renderer `_labels`) -- the monitor's selector and
# its metadata.labels both carry them.
CP_LABELS = {
    "app.kubernetes.io/managed-by": "agentforge-cp",
    "agentforge.io/org": CP_ORG,
    "agentforge.io/workspace": CP_WS,
    "agentforge.io/pool": CP_POOL,
}

# EXACTLY what agentforge-platform `_podmonitor()` + `_podmonitor_endpoints()` emit (renderer.py
# 1355-1397 on main). If the CP render changes, change it HERE and re-run: a policy that denies
# the live render wedges the whole agentforge-tenants layer, which is the failure this case
# guards against.
def cp_podmonitor() -> dict[str, Any]:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {
            "name": CP_WORKLOAD,
            "namespace": CP_NS,
            "labels": {**CP_LABELS, "release": "kube-prometheus-stack"},
        },
        "spec": {
            "namespaceSelector": {"matchNames": [CP_NS]},
            "selector": {"matchLabels": dict(CP_LABELS)},
            "podMetricsEndpoints": [
                {
                    "port": "metrics",
                    "interval": "30s",
                    "relabelings": [
                        {"targetLabel": "job", "replacement": "agentforge-worker"}
                    ],
                }
            ],
        },
    }


def pm(mutate) -> dict[str, Any]:
    """The CP render with one field changed -- so every deny case differs from the ADMITTED
    baseline in exactly the property under test, and cannot pass for an unrelated reason."""
    obj = cp_podmonitor()
    mutate(obj)
    return obj


def _endpoint(obj: dict[str, Any]) -> dict[str, Any]:
    return obj["spec"]["podMetricsEndpoints"][0]


# EXACTLY what the sandbox render emits as 61-externalsecret.yaml (renderer.py 1620-1634). Carried
# here so the correlation clauses (12-15) are exercised against a real object rather than passing
# vacuously -- if a future edit breaks them, THIS is the case that says so.
def cp_externalsecret() -> dict[str, Any]:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {
            "name": CP_CREDS_SECRET,
            "namespace": CP_NS,
            "labels": dict(CP_LABELS),
        },
        "spec": {
            "refreshInterval": "1m",
            "secretStoreRef": {"name": CP_ESO_SA, "kind": "SecretStore"},
            "target": {"name": CP_CREDS_SECRET, "creationPolicy": "Owner"},
            "dataFrom": [
                {"extract": {"key": f"tenants/{CP_ORG}/{CP_WS}/orchestrator"}}
            ],
        },
    }


def es(mutate) -> dict[str, Any]:
    obj = cp_externalsecret()
    mutate(obj)
    return obj


# -- the case table ---------------------------------------------------------------------------
# (name, object, expected) where expected is None to ADMIT or the 1-based validation index that
# must DENY. Pinning the index (not just "denied") is what stops a case passing because some
# OTHER clause happened to reject the fixture.
CASES: list[tuple[str, dict[str, Any], int | None]] = [
    # ---- the live CP render must stay admitted -------------------------------------------
    ("cp-render-as-shipped", cp_podmonitor(), None),
    # ---- (1) GVK allowlist ----------------------------------------------------------------
    (
        "servicemonitor-gvk-rejected",
        pm(lambda o: o.update(kind="ServiceMonitor")),
        1,
    ),
    (
        "prometheusrule-gvk-rejected",
        pm(lambda o: o.update(kind="PrometheusRule")),
        1,
    ),
    # ---- (17) cross-tenant / cluster-wide reach -------------------------------------------
    (
        "matchnames-another-tenant",
        pm(lambda o: o["spec"].update(namespaceSelector={"matchNames": ["af-tenant-victim-ws"]})),
        17,
    ),
    (
        "namespaceselector-any-true",
        pm(
            lambda o: o["spec"].update(
                namespaceSelector={"any": True, "matchNames": [CP_NS]}
            )
        ),
        17,
    ),
    # ---- (17) inert monitor ---------------------------------------------------------------
    (
        "missing-release-label",
        pm(lambda o: o["metadata"]["labels"].pop("release")),
        17,
    ),
    ("no-endpoints", pm(lambda o: o["spec"].update(podMetricsEndpoints=[])), 17),
    # ---- (17) secret exfil / who Prometheus dials -----------------------------------------
    (
        "endpoint-bearer-token-secret",
        pm(
            lambda o: _endpoint(o).update(
                bearerTokenSecret={"name": "orchestrator-creds", "key": "token"}
            )
        ),
        17,
    ),
    (
        "endpoint-proxy-url",
        pm(lambda o: _endpoint(o).update(proxyUrl="http://attacker.example:8080")),
        17,
    ),
    (
        "endpoint-tls-config",
        pm(
            lambda o: _endpoint(o).update(
                tlsConfig={"ca": {"secret": {"name": "orchestrator-creds", "key": "ca"}}}
            )
        ),
        17,
    ),
    # ---- (17) label forgery ---------------------------------------------------------------
    # metricRelabelings rewrites the identity labels AFTER the scrape...
    (
        "endpoint-metric-relabelings",
        pm(
            lambda o: _endpoint(o).update(
                metricRelabelings=[
                    {"targetLabel": "namespace", "replacement": "af-tenant-victim-ws"}
                ]
            )
        ),
        17,
    ),
    # ...and `relabelings` does the same thing BEFORE it, with strictly more reach. The
    # operator appends an endpoint's user relabelings AFTER its own namespace/pod/container/job
    # relabelings, so a `replace` onto one of those targets wins. `unless on (namespace, pod)`
    # in agentforge-coverage-rules.yaml is the join this forges.
    (
        "relabel-forges-namespace",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"targetLabel": "namespace", "replacement": "af-tenant-victim-ws"}
            )
        ),
        17,
    ),
    (
        "relabel-forges-pod",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"targetLabel": "pod", "replacement": "victim-orchestrator-0"}
            )
        ),
        17,
    ),
    (
        "relabel-forges-container",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"targetLabel": "container", "replacement": "orchestrator"}
            )
        ),
        17,
    ),
    # __address__ decides WHO PROMETHEUS DIALS -- the same property proxyUrl is banned for. The
    # tenant NetworkPolicy admits TCP 9464 from ns `monitoring` in EVERY tenant namespace, so a
    # redirected address reaches another tenant's exporter from Prometheus's network position.
    (
        "relabel-redirects-address",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {
                    "sourceLabels": ["__address__"],
                    "regex": "(.*)",
                    "targetLabel": "__address__",
                    "replacement": "10.42.7.9:9464",
                }
            )
        ),
        17,
    ),
    (
        "relabel-rewrites-metrics-path",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"targetLabel": "__metrics_path__", "replacement": "/debug/pprof"}
            )
        ),
        17,
    ),
    # labelmap/labeldrop/labelkeep carry NO targetLabel, so a targetLabel-only pin misses them:
    # labelmap can MINT `namespace` out of a tenant-controlled pod label.
    (
        "relabel-labelmap",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {
                    "action": "labelmap",
                    "regex": "__meta_kubernetes_pod_label_(.+)",
                    "replacement": "$1",
                }
            )
        ),
        17,
    ),
    # The CRD enum accepts both spellings of every action, so both must be denied.
    (
        "relabel-labeldrop-camelcase",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"action": "LabelDrop", "regex": "namespace|pod"}
            )
        ),
        17,
    ),
    (
        "relabel-labelkeep",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"action": "labelkeep", "regex": "job"}
            )
        ),
        17,
    ),
    # honorLabels reaches the same forgery through the exposition body instead of the config.
    ("endpoint-honor-labels", pm(lambda o: _endpoint(o).update(honorLabels=True)), 17),
    # A relabeling onto a NON-reserved label stays admitted: the CP legitimately writes
    # job=agentforge-worker, and pinning relabelings by equality would wedge the tenant layer on
    # the next renderer change.
    (
        "relabel-nonreserved-label-admitted",
        pm(
            lambda o: _endpoint(o)["relabelings"].append(
                {"targetLabel": "forge_pool_tier", "replacement": "p1"}
            )
        ),
        None,
    ),
    # ---- (5)/(16) namespace pinning still applies to this GVK ------------------------------
    (
        "podmonitor-into-monitoring-ns",
        pm(
            lambda o: (
                o["metadata"].update(namespace="monitoring"),
                o["spec"].update(namespaceSelector={"matchNames": ["monitoring"]}),
            )
        ),
        5,
    ),
    (
        "podmonitor-into-reserved-tenant-zero",
        pm(
            lambda o: (
                o["metadata"].update(namespace="af-tenant-tenant-zero-playground"),
                o["spec"].update(
                    namespaceSelector={"matchNames": ["af-tenant-tenant-zero-playground"]}
                ),
            )
        ),
        16,
    ),
    # ---- (12)-(15) ESO correlation: proof the map lookup is really being evaluated ---------
    ("cp-externalsecret-as-shipped", cp_externalsecret(), None),
    (
        "externalsecret-reads-another-tenants-subtree",
        es(
            lambda o: o["spec"].update(
                dataFrom=[{"extract": {"key": "tenants/victim-org/victim-ws/orchestrator"}}]
            )
        ),
        15,
    ),
    (
        "externalsecret-in-unmapped-namespace",
        es(
            lambda o: o["metadata"].update(namespace="af-tenant-unmapped-ws")
        ),
        12,
    ),
]


# -- evaluation -------------------------------------------------------------------------------

# cel-python 0.5.0 has no `_==_`/`_!=_` overload for (message, null) -- `params != null` and
# `namespaceObject != null`, both of which the policy's own variables use as fail-closed guards,
# raise "found no matching overload" instead of returning a bool. The apiserver's CEL does have
# it. Re-supply exactly that overload and delegate every other comparison to the stock operator,
# so nothing else about equality changes.
def _cel_eq(a: Any, b: Any) -> celtypes.BoolType:
    if a is None or b is None:
        return celtypes.BoolType(a is None and b is None)
    return celtypes.BoolType(bool(operator.eq(a, b)))


def _cel_ne(a: Any, b: Any) -> celtypes.BoolType:
    if a is None or b is None:
        return celtypes.BoolType(not (a is None and b is None))
    return celtypes.BoolType(bool(operator.ne(a, b)))


# cel-python 0.5.0 also ships none of the CEL *strings extension*, which Kubernetes DOES enable and
# which the ESO correlation variables use (`entryParts`, `allowedCreds` both call `.split()`).
# Supply the one member this policy actually calls, with the extension's own semantics.
def _cel_split(text: Any, sep: Any) -> celtypes.ListType:
    return celtypes.ListType([celtypes.StringType(p) for p in str(text).split(str(sep))])


EXTRA_FUNCTIONS = {"_==_": _cel_eq, "_!=_": _cel_ne, "split": _cel_split}


def load_policy(path: Path = POLICY) -> dict[str, Any]:
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ValidatingAdmissionPolicy":
            return doc
    raise SystemExit(f"{path}: no ValidatingAdmissionPolicy document")


def load_tenant_map() -> dict[str, Any]:
    for doc in yaml.safe_load_all(TENANT_MAP_FILE.read_text(encoding="utf-8")):
        if doc and doc.get("kind") == "ConfigMap":
            return {"data": doc.get("data") or {}}
    raise SystemExit(f"{TENANT_MAP_FILE}: no ConfigMap document")


def evaluate(
    policy: dict[str, Any], tenant_map: dict[str, Any], obj: dict[str, Any]
) -> int | None:
    """Return the 1-based index of the first validation that DENIES `obj`, or None to admit.

    Mirrors the apiserver: `spec.variables` are evaluated in declaration order (each one able to
    read the ones before it), then the validations in order.
    """
    env = celpy.Environment()
    group, _, _ = obj["apiVersion"].rpartition("/")
    if "/" not in obj["apiVersion"]:  # core group ("v1")
        group = ""
    ns = obj.get("metadata", {}).get("namespace", "")
    base = {
        "object": celpy.json_to_cel(obj),
        # CREATE only, so oldObject is null; no validation in this policy reads it.
        "oldObject": celpy.json_to_cel(None),
        "params": celpy.json_to_cel(tenant_map),
        "request": celpy.json_to_cel(
            {
                "kind": {"group": group, "version": "v1", "kind": obj["kind"]},
                "operation": "CREATE",
                "namespace": ns,
                "userInfo": {
                    "username": "system:serviceaccount:flux-system:"
                    "agentforge-tenants-reconciler"
                },
            }
        ),
        # A tenant Namespace object as the CP renders it: PSA baseline, and NOT sandbox-labelled.
        "namespaceObject": celpy.json_to_cel(
            {
                "metadata": {
                    "name": ns,
                    "labels": {"pod-security.kubernetes.io/enforce": "baseline"},
                }
            }
        ),
    }

    resolved: dict[str, Any] = {}
    for var in policy["spec"].get("variables", []):
        act = dict(base)
        act["variables"] = celtypes.MapType(
            {celtypes.StringType(k): v for k, v in resolved.items()}
        )
        prog = env.program(env.compile(var["expression"]), functions=EXTRA_FUNCTIONS)
        resolved[var["name"]] = prog.evaluate(act)

    act = dict(base)
    act["variables"] = celtypes.MapType(
        {celtypes.StringType(k): v for k, v in resolved.items()}
    )
    for idx, val in enumerate(policy["spec"]["validations"], 1):
        prog = env.program(env.compile(val["expression"]), functions=EXTRA_FUNCTIONS)
        if not prog.evaluate(act):
            return idx
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    # Point at a candidate copy (e.g. `git show <rev>:<path>`) to see how a proposed -- or a
    # previous -- version of the policy scores against the same table, without editing the file.
    ap.add_argument("--policy", type=Path, default=POLICY, metavar="PATH")
    args = ap.parse_args()

    policy = load_policy(args.policy)
    tenant_map = load_tenant_map()
    failures: list[str] = []
    for name, obj, expected in CASES:
        got = evaluate(policy, tenant_map, copy.deepcopy(obj))
        ok = got == expected
        if args.verbose or not ok:
            verdict = "ADMIT" if got is None else f"DENY by validation ({got})"
            want = "ADMIT" if expected is None else f"DENY by validation ({expected})"
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {verdict}" + ("" if ok else f"  [want {want}]"))
        if not ok:
            failures.append(name)

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} tenant-guard CEL cases passed")
    if failures:
        print("FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
