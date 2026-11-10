#!/usr/bin/env python3
"""Read-only preflight for Relay-managed Kubernetes workers on an EXPLICIT Kubernetes context.

Two steps, so the evaluation is testable offline and the evidence is inspectable:

    # 1. collect: read-only `kubectl get`/`version` on --context ONLY, reduced to the fields used
    python3 scripts/relay-worker-preflight.py collect --context admin@ai --out <evidence-path>.json
    # 2. evaluate: pure function of (snapshot, binding); no cluster access
    python3 scripts/relay-worker-preflight.py evaluate --snapshot <evidence-path>.json \
        --binding docs/relay-workers/ailab-binding.example.json [--json]

What it is NOT: a qualification. Object presence (a CRD, a RuntimeClass, a node label, a KubeVirt
CR in phase Deployed) proves that a prerequisite is visible, never that a capability works. The
report therefore never derives `qualified` from what it sees; capability status is copied from the
binding. A binding that claims `qualified` without a receipt digest is rejected, and so is a
qualified VM capability whose `qualified_pins` differ from the binding's KubeVirt pins (a pin change
needs a new qualification). Qualification is the live evidence in
docs/runbooks/relay-worker-kubevirt-feasibility.md, recorded outside git, bound by digest.

Fail closed: no --context = refusal; an unknown context, a mismatched API server, a forbidden or
failed query = `unknown` (exit 2) — never `pass`. Exit 0 only when every preflight check passes;
1 when any check fails; 2 when none fails but at least one is unknown.

Scope: this first collector is DEDICATED-POOL ONLY. Nodes carrying a forbidden label (the
Relay/router agent pool, control plane) never qualify, and a RuntimeClass that pins pods there fails.
Co-located placement needs separate measured evidence and a later change, not a flag.

Never: Secrets, exec/attach/port-forward/proxy, writes, `config view --raw`, credential discovery.
The allowlist below is asserted by scripts/tests/test_relay_worker_preflight.py. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import pathlib
import re
import subprocess
import sys

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
SNAPSHOT_SCHEMA = "ailab.relay-worker-preflight-snapshot/v1"
REPORT_SCHEMA = "ailab.relay-worker-preflight-report/v1"
BINDING_SCHEMA = "ailab.relay-worker-binding/v1alpha1"
CAPABILITY_STATUSES = {"pending", "unsupported", "qualified"}
# A qualified capability that ran on KubeVirt is valid only for the pins its evidence was taken with.
_KUBEVIRT_PINS = ("kubevirt_version", "kubevirt_operator_digest")
QUALIFICATION_PINS = {"vm.kubevirt": _KUBEVIRT_PINS, "vm.compose": _KUBEVIRT_PINS}

# name -> kubectl argv (appended after `kubectl --context <ctx>`). Only `get` and `version`.
QUERIES: dict[str, list[str]] = {
    "version": ["version", "-o", "json"],
    "kube_system_ns": ["get", "namespace", "kube-system", "-o", "json"],
    "nodes": ["get", "nodes", "-o", "json"],
    "pods": ["get", "pods", "--all-namespaces", "-o", "json"],
    "storageclasses": ["get", "storageclasses.storage.k8s.io", "-o", "json"],
    "runtimeclasses": ["get", "runtimeclasses.node.k8s.io", "-o", "json"],
    "cilium_ds": ["get", "daemonset", "cilium", "-n", "kube-system", "-o", "json"],
    "kubevirt_crd": ["get", "customresourcedefinitions.apiextensions.k8s.io",
                     "virtualmachines.kubevirt.io", "-o", "json"],
    "kubevirt_cr": ["get", "kubevirts.kubevirt.io", "--all-namespaces", "-o", "json"],
}
FORBIDDEN_TOKENS = {"secret", "secrets", "exec", "attach", "port-forward", "portforward", "proxy",
                    "cp", "--raw", "apply", "create", "delete", "patch", "edit", "label", "annotate",
                    "cordon", "drain", "scale", "replace", "debug", "run", "auth", "config"}


# ---------------------------------------------------------------------------- quantities
_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DEC = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
        "P": 1e15, "E": 1e18}


def quantity(value) -> float:
    """Kubernetes resource.Quantity -> float (cores or bytes). Raises ValueError if malformed."""
    s = str(value).strip()
    m = re.fullmatch(r"(\+?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?", s)
    if not m:
        raise ValueError(f"bad quantity {value!r}")
    num, suf = float(m.group(1)), m.group(2) or ""
    out = num * (_BIN[suf] if suf in _BIN else _DEC[suf])
    if not math.isfinite(out) or out < 0:
        raise ValueError(f"non-finite or negative quantity {value!r}")
    return out


# ---------------------------------------------------------------------------- collection
def _check_argv(argv: list[str]) -> None:
    lowered = [a.lower() for a in argv]
    if lowered[0] not in ("get", "version"):
        raise RuntimeError(f"non-read verb refused: {argv[0]}")
    for a in lowered:
        if a in FORBIDDEN_TOKENS or a.startswith("secret"):
            raise RuntimeError(f"forbidden kubectl token refused: {a}")


def _classify_error(stderr: str) -> str:
    """Bounded classification only. Raw stderr is NEVER persisted or printed: exec auth plugins
    and provider errors can echo credentials."""
    s = (stderr or "").lower()
    if "forbidden" in s:
        return "forbidden"
    if "unauthorized" in s or "credentials" in s or "token" in s:
        return "unauthenticated"
    if "notfound" in s or "not found" in s or "the server doesn't have a resource type" in s:
        return "notfound"
    return "error"


def _reduce(name: str, data: dict) -> dict:
    """Keep only the fields evaluate() reads. Pod env/args/annotations never reach disk."""
    if not isinstance(data, dict):
        raise ValueError("expected Kubernetes object")
    if name in {"nodes", "pods", "storageclasses", "runtimeclasses", "kubevirt_cr"}:
        if not isinstance(data.get("items"), list):
            raise ValueError("expected Kubernetes list items")
        if not all(isinstance(item, dict) for item in data["items"]):
            raise ValueError("expected Kubernetes list objects")
    if name == "version":
        return {"serverVersion": {"gitVersion": (data.get("serverVersion") or {}).get("gitVersion")}}
    if name == "kube_system_ns":
        return {"metadata": {"uid": data.get("metadata", {}).get("uid")}}
    if name == "nodes":
        items = []
        for n in data.get("items", []):
            st = n.get("status", {})
            items.append({
                "metadata": {"name": n["metadata"]["name"], "labels": n["metadata"].get("labels", {})},
                "spec": {"taints": n.get("spec", {}).get("taints", []),
                         "unschedulable": n.get("spec", {}).get("unschedulable", False)},
                "status": {"allocatable": st.get("allocatable", {}),
                           "conditions": [{"type": c.get("type"), "status": c.get("status")}
                                          for c in st.get("conditions", [])],
                           "nodeInfo": {"architecture": st.get("nodeInfo", {}).get("architecture")}},
            })
        return {"items": items}
    if name == "pods":
        items = []
        for p in data.get("items", []):
            spec = p.get("spec", {})
            reqs = []
            for key in ("initContainers", "containers"):
                for c in spec.get(key, []) or []:
                    reqs.append({"kind": key, "restartPolicy": c.get("restartPolicy"),
                                 "requests": ((c.get("resources") or {}).get("requests") or {})})
            items.append({"metadata": {"namespace": p["metadata"].get("namespace")},
                          "spec": {"nodeName": spec.get("nodeName"), "containers": reqs,
                                   "overhead": spec.get("overhead") or {},
                                   "resources": {"requests":
                                                 ((spec.get("resources") or {}).get("requests") or {})}},
                          "status": {"phase": p.get("status", {}).get("phase")}})
        return {"items": items}
    if name == "storageclasses":
        return {"items": [{"metadata": {"name": s["metadata"]["name"]},
                           "provisioner": s.get("provisioner"),
                           "volumeBindingMode": s.get("volumeBindingMode"),
                           "reclaimPolicy": s.get("reclaimPolicy")} for s in data.get("items", [])]}
    if name == "runtimeclasses":
        return {"items": [{"metadata": {"name": r["metadata"]["name"]}, "handler": r.get("handler"),
                           "scheduling": r.get("scheduling") or {}} for r in data.get("items", [])]}
    if name == "cilium_ds":
        cs = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        return {"images": [c.get("image") for c in cs]}
    if name == "kubevirt_crd":
        return {"metadata": {"name": data.get("metadata", {}).get("name")}}
    if name == "kubevirt_cr":
        return {"items": [{"metadata": {"name": k["metadata"]["name"],
                                        "namespace": k["metadata"].get("namespace")},
                           "status": {"phase": k.get("status", {}).get("phase"),
                                      "observedKubeVirtVersion":
                                          k.get("status", {}).get("observedKubeVirtVersion")}}
                          for k in data.get("items", [])]}
    raise KeyError(name)


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def resolve_context(context: str, kubeconfig: str | None, run=_run) -> dict:
    """Exact-context lookup. Returns {exists, server}; never reads credentials."""
    base = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else [])
    try:
        names = run(base + ["config", "get-contexts", "-o", "name"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exists": False, "server": None, "error": type(exc).__name__}
    if names.returncode != 0 or context not in names.stdout.split():
        return {"exists": False, "server": None,
                "error": None if names.returncode == 0 else "kubeconfig-unreadable"}
    # jsonpath on one non-secret field; --minify narrows to this context. Not --raw.
    try:
        server = run(base + ["config", "view", "--minify", "--context", context,
                             "-o", "jsonpath={.clusters[0].cluster.server}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exists": True, "server": None, "error": type(exc).__name__}
    url = server.stdout.strip() if server.returncode == 0 else ""
    # Only an https URL is kept; anything else (an error text) is dropped, not persisted.
    ok = re.fullmatch(r"https://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._\-/]*)?", url)
    return {"exists": True, "server": url if ok else None, "error": None}


def collect(context: str, kubeconfig: str | None = None, run=_run) -> dict:
    if not context:
        raise ValueError("--context is required; the default context is never used")
    ctx = resolve_context(context, kubeconfig, run)
    snap = {"schema": SNAPSHOT_SCHEMA, "context": context, "context_exists": ctx["exists"],
            "api_server": ctx["server"], "context_error": ctx.get("error"),
            "collected_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "queries": {}}
    if not ctx["exists"]:
        return snap  # every query stays absent -> unknown; nothing runs against another context
    base = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + ["--context", context]
    for name, argv in QUERIES.items():
        _check_argv(argv)
        try:
            proc = run(base + argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            snap["queries"][name] = {"status": "error", "detail": type(exc).__name__}  # class name only
            continue
        if proc.returncode != 0:
            snap["queries"][name] = {"status": _classify_error(proc.stderr)}
            continue
        try:
            snap["queries"][name] = {"status": "ok", "data": _reduce(name, json.loads(proc.stdout))}
        except (ValueError, KeyError, TypeError) as exc:
            snap["queries"][name] = {"status": "error", "detail": f"unparseable: {type(exc).__name__}"}
    return snap


# ---------------------------------------------------------------------------- evaluation
def _check(cid: str, status: str, detail: str, source: str = "cluster", **extra) -> dict:
    return {"id": cid, "status": status, "detail": detail, "source": source, **extra}


def _q(snap: dict, name: str):
    """(status, data). Missing query = 'missing'."""
    q = snap.get("queries", {}).get(name)
    if not q:
        return "missing", None
    return q.get("status", "error"), q.get("data")


def _unavailable(cid: str, snap: dict, name: str) -> dict:
    st, _ = _q(snap, name)
    return _check(cid, UNKNOWN, f"query {name} {st}; cannot decide (fail closed)")


SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_STR_OR_NONE = (str, type(None))


def validate_binding(b) -> list[str]:
    """Type-checks the binding; returns errors instead of crashing evaluate()."""
    if not isinstance(b, dict):
        return ["binding must be a JSON object"]
    errs = []
    if b.get("schema") != BINDING_SCHEMA:
        errs.append(f"binding schema must be {BINDING_SCHEMA}")
    for key in ("cluster", "pins", "placement", "headroom", "storage", "capabilities"):
        if not isinstance(b.get(key), dict):
            errs.append(f"binding.{key} must be an object")
    if errs:
        return errs
    c, pins, pl, st = b["cluster"], b["pins"], b["placement"], b["storage"]
    for k in ("expected_context", "expected_api_server"):
        if not isinstance(c.get(k), str) or not c[k]:
            errs.append(f"cluster.{k} must be a non-empty string")
    if not isinstance(c.get("expected_kube_system_uid"), _STR_OR_NONE):
        errs.append("cluster.expected_kube_system_uid must be a string or null")
    for k in ("kubernetes_version", "cilium_version", "kubevirt_version", "kubevirt_operator_digest"):
        if not isinstance(pins.get(k), _STR_OR_NONE):
            errs.append(f"pins.{k} must be a string or null")
    dig = pins.get("kubevirt_operator_digest")
    if isinstance(dig, str) and not SHA256_DIGEST.fullmatch(dig):
        errs.append("pins.kubevirt_operator_digest must be sha256:<64 lowercase hex> or null")
    if "colocated_gvisor_qualified" in pl:
        errs.append("placement.colocated_gvisor_qualified is not supported: this collector is "
                    "dedicated-pool only; co-location needs measured evidence, not a flag")
    sel = pl.get("node_selector")
    if not isinstance(sel, dict) or not sel or not all(isinstance(v, str) for v in sel.values()):
        errs.append("placement.node_selector must be a non-empty map naming a dedicated pool")
    taint = pl.get("required_taint")
    if not (isinstance(taint, dict) and all(isinstance(taint.get(k), str) for k in ("key", "value", "effect"))):
        errs.append("placement.required_taint must have string key/value/effect")
    for k in ("forbidden_node_labels", "protected_namespaces"):
        v = pl.get(k)
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            errs.append(f"placement.{k} must be a non-empty list of strings")
    mn = pl.get("min_nodes", 1)
    if not isinstance(mn, int) or isinstance(mn, bool) or mn < 1:
        errs.append("placement.min_nodes must be an integer >= 1")
    if not b["headroom"]:
        errs.append("headroom must name at least one tier")
    for tier, want in b["headroom"].items():
        if not isinstance(want, dict) or not any(k in want for k in _RES):
            errs.append(f"headroom.{tier} must request at least one of {sorted(_RES)}")
            continue
        for k, v in want.items():
            if k.startswith("_"):
                continue
            if k not in _RES:
                errs.append(f"headroom.{tier}.{k} is not a known resource")
                continue
            try:
                if quantity(v) <= 0:
                    errs.append(f"headroom.{tier}.{k} must be > 0")
            except ValueError:
                errs.append(f"headroom.{tier}.{k}: invalid quantity {v!r}")
    for k in ("workspace_storage_class",):
        if not isinstance(st.get(k), str) or not st[k]:
            errs.append(f"storage.{k} must be a non-empty string")
    if not isinstance(st.get("forbidden_storage_classes", []), list):
        errs.append("storage.forbidden_storage_classes must be a list")
    rcs = b.get("runtime_classes", {})
    if not isinstance(rcs, dict) or not all(isinstance(v, str) for v in rcs.values()):
        errs.append("runtime_classes must map alias -> RuntimeClass name")
    for cap, v in b["capabilities"].items():
        if not isinstance(v, dict):
            errs.append(f"capability {cap} must be an object")
            continue
        sv = v.get("status")
        if sv not in CAPABILITY_STATUSES:
            errs.append(f"capability {cap}: status {sv!r} not in {sorted(CAPABILITY_STATUSES)}")
        if sv == "qualified" and not re.fullmatch(r"[0-9a-f]{64}", str(v.get("receipt_sha256", ""))):
            errs.append(f"capability {cap}: 'qualified' requires receipt_sha256 of live evidence")
        if sv == "qualified" and cap in QUALIFICATION_PINS:
            qp = v.get("qualified_pins")
            if not isinstance(qp, dict):
                errs.append(f"capability {cap}: 'qualified' requires qualified_pins "
                            f"{list(QUALIFICATION_PINS[cap])}, the pins the evidence was taken with")
                continue
            for k in QUALIFICATION_PINS[cap]:
                if not isinstance(pins.get(k), str) or qp.get(k) != pins.get(k):
                    errs.append(f"capability {cap}: qualified_pins.{k} {qp.get(k)!r} != pins.{k} "
                                f"{pins.get(k)!r}; a pin change needs a new qualification")
    return errs


def check_context(snap: dict, b: dict) -> list[dict]:
    c = b["cluster"]
    out = []
    if snap.get("context") != c.get("expected_context"):
        out.append(_check("context.name", FAIL,
                          f"snapshot context {snap.get('context')!r} != binding {c.get('expected_context')!r}"))
    elif not snap.get("context_exists"):
        out.append(_check("context.name", UNKNOWN, "context not present in the supplied kubeconfig"))
    else:
        out.append(_check("context.name", PASS, f"context {snap['context']} exists"))
    exp = c.get("expected_api_server")
    if not snap.get("api_server"):
        out.append(_check("context.api_server", UNKNOWN, "API server not resolved"))
    elif snap["api_server"] != exp:
        out.append(_check("context.api_server", FAIL, f"{snap['api_server']} != {exp} (wrong cluster)"))
    else:
        out.append(_check("context.api_server", PASS, snap["api_server"]))
    st, data = _q(snap, "kube_system_ns")
    uid = (data or {}).get("metadata", {}).get("uid") if st == "ok" else None
    pin = c.get("expected_kube_system_uid")
    if uid is None:
        out.append(_unavailable("context.cluster_uid", snap, "kube_system_ns"))
    elif not pin:
        out.append(_check("context.cluster_uid", UNKNOWN,
                          "binding has no expected_kube_system_uid; operator must pin it", observed=uid))
    elif uid != pin:
        out.append(_check("context.cluster_uid", FAIL, f"kube-system uid {uid} != pinned {pin}"))
    else:
        out.append(_check("context.cluster_uid", PASS, uid))
    return out


def check_versions(snap: dict, b: dict) -> list[dict]:
    pins, out = b["pins"], []
    st, data = _q(snap, "version")
    if st != "ok":
        out.append(_unavailable("version.kubernetes", snap, "version"))
    else:
        got = (data.get("serverVersion") or {}).get("gitVersion") or ""
        exp = pins.get("kubernetes_version")
        ok = bool(exp) and (got == exp or got.startswith(exp + "+"))
        out.append(_check("version.kubernetes", PASS if ok else FAIL, f"server {got}, pinned {exp}"))
    st, data = _q(snap, "cilium_ds")
    if st != "ok":
        out.append(_unavailable("version.cilium", snap, "cilium_ds"))
    else:
        exp = pins.get("cilium_version")
        # Strip the digest before parsing the final path component: a digest-only
        # reference has no version tag, and registry ports are not image tags.
        refs = [i.split("@", 1)[0].rsplit("/", 1)[-1] for i in data.get("images", []) if i]
        tags = [i.rsplit(":", 1)[1] for i in refs if ":" in i]
        ok = bool(exp) and any(t.lstrip("v") == exp.lstrip("v") for t in tags)
        state = PASS if ok else FAIL if tags else UNKNOWN
        out.append(_check("version.cilium", state,
                          f"agent tags {tags}, pinned {exp}" if tags else
                          "agent images have no version tag; digest alone does not establish the pinned version"))
    return out


def _node_ready(n: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in n["status"].get("conditions", []))


def dedicated_nodes(nodes: list[dict], pl: dict) -> tuple[list[dict], list[str]]:
    sel, taint = pl.get("node_selector") or {}, pl.get("required_taint")
    chosen, rejected = [], []
    for n in nodes:
        labels, name = n["metadata"].get("labels", {}), n["metadata"]["name"]
        if not all(labels.get(k) == v for k, v in sel.items()):
            continue
        why = []
        bad = [l for l in pl.get("forbidden_node_labels", []) if l in labels]
        if bad:
            why.append(f"shares forbidden labels {bad}")
        if taint and not any(t.get("key") == taint["key"] and t.get("value") == taint.get("value")
                             and t.get("effect") == taint.get("effect")
                             for t in n["spec"].get("taints", [])):
            why.append("missing dedicated taint")
        # The spike tolerates only the explicitly approved dedicated taint.
        # Ready alone does not make a disk-pressure/quarantined node schedulable.
        for t in n["spec"].get("taints", []):
            if t.get("effect") not in ("NoSchedule", "NoExecute"):
                continue
            if not taint or any(t.get(k) != taint.get(k) for k in ("key", "value", "effect")):
                why.append(f"untolerated blocking taint {t.get('key')}:{t.get('effect')}")
        if n["spec"].get("unschedulable"):
            why.append("cordoned")
        if not _node_ready(n):
            why.append("not Ready")
        arch = n["status"].get("nodeInfo", {}).get("architecture")
        if pl.get("architecture") and arch != pl["architecture"]:
            why.append(f"arch {arch}")
        if why:
            rejected.append(f"{name}: {', '.join(why)}")
        else:
            chosen.append(n)
    return chosen, rejected


def check_placement(snap: dict, b: dict) -> tuple[list[dict], list[dict]]:
    pl = b["placement"]
    st, data = _q(snap, "nodes")
    if st != "ok":
        return [_unavailable("placement.dedicated_pool", snap, "nodes")], []
    chosen, rejected = dedicated_nodes(data.get("items", []), pl)
    need = int(pl.get("min_nodes", 1))
    names = [n["metadata"]["name"] for n in chosen]
    if len(chosen) >= need:
        return [_check("placement.dedicated_pool", PASS, f"{len(chosen)} dedicated node(s)",
                       nodes=names, rejected=rejected)], chosen
    return [_check("placement.dedicated_pool", FAIL,
                   f"{len(chosen)}/{need} usable nodes match {pl.get('node_selector')}; Relay/router "
                   "agent nodes are not implicitly approved (plan phase 4)",
                   nodes=names, rejected=rejected)], chosen


_RES = {"cpu": "cpu", "memory": "memory", "ephemeral_storage": "ephemeral-storage"}


def pod_request(pod: dict, res: str) -> float:
    """Effective scheduler request for one resource (KEP-753 sidecars, KEP-2837 pod-level).

    Pod-level `spec.resources.requests` wins when set. Otherwise:
      init peak = max over ordinary init containers i of (req_i + sidecars declared before i);
      steady    = sum(app containers) + sum(all restartable init sidecars);
      request   = max(init peak, steady).
    Overhead is added in both cases. Raises ValueError on a malformed quantity (caller -> unknown).
    """
    spec = pod["spec"]
    overhead = quantity(spec.get("overhead", {}).get(res, 0))
    pod_level = ((spec.get("resources") or {}).get("requests") or {})
    if res in pod_level:
        return quantity(pod_level[res]) + overhead
    sidecars, init_peak, app = 0.0, 0.0, 0.0
    for c in spec.get("containers", []):
        r = quantity(c.get("requests", {}).get(res, 0))
        if c.get("kind") == "initContainers":
            if c.get("restartPolicy") == "Always":
                sidecars += r
                init_peak = max(init_peak, sidecars)
            else:
                init_peak = max(init_peak, sidecars + r)
        else:
            app += r
    return max(init_peak, app + sidecars) + overhead


def node_free(node: dict, pods: list[dict]) -> dict:
    """allocatable - effective requests of non-terminal pods. None = cannot compute (unknown)."""
    name = node["metadata"]["name"]
    alloc = node["status"].get("allocatable", {})
    free = {}
    for key, res in _RES.items():
        try:
            used = sum(pod_request(p, res) for p in pods
                       if p["spec"].get("nodeName") == name
                       and p["status"].get("phase") not in ("Succeeded", "Failed"))
            free[key] = quantity(alloc[res]) - used if res in alloc else None
        except (ValueError, KeyError, TypeError, AttributeError):
            free[key] = None
    return free


def check_protected_workloads(snap: dict, b: dict, chosen: list[dict], pool_status: str) -> list[dict]:
    """Labels/taints do not prove Relay/router absence: look at what actually runs there."""
    cid = "placement.protected_workloads_absent"
    if pool_status != PASS:
        return [_check(cid, UNKNOWN if pool_status == UNKNOWN else FAIL, "no qualifying dedicated pool")]
    st, data = _q(snap, "pods")
    if st != "ok":
        return [_unavailable(cid, snap, "pods")]
    names = {n["metadata"]["name"] for n in chosen}
    prot = set(b["placement"]["protected_namespaces"])
    hits = sorted({(p["spec"].get("nodeName"), p["metadata"].get("namespace"))
                   for p in data.get("items", [])
                   if p["spec"].get("nodeName") in names and p["metadata"].get("namespace") in prot
                   and p["status"].get("phase") not in ("Succeeded", "Failed")})
    if hits:
        return [_check(cid, FAIL, f"protected workloads on the dedicated pool: {hits}")]
    return [_check(cid, PASS, f"no {sorted(prot)} pods on {sorted(names)} at collection time")]


def check_headroom(snap: dict, b: dict, chosen: list[dict], pool_status: str) -> list[dict]:
    out = []
    for tier, want in b["headroom"].items():
        cid = f"headroom.{tier}"
        if pool_status != PASS:
            out.append(_check(cid, UNKNOWN if pool_status == UNKNOWN else FAIL,
                              "no qualifying dedicated pool to measure"))
            continue
        st, data = _q(snap, "pods")
        if st != "ok":
            out.append(_unavailable(cid, snap, "pods"))
            continue
        fits, per_node, missing = [], {}, False
        for n in chosen:
            free = node_free(n, data.get("items", []))
            per_node[n["metadata"]["name"]] = {k: v for k, v in free.items()}
            keys = [k for k in _RES if k in want]
            if any(free[k] is None for k in keys):
                missing = True
                continue
            if all(free[k] >= quantity(want[k]) for k in keys):
                fits.append(n["metadata"]["name"])
        if fits:
            out.append(_check(cid, PASS, f"request-level fit on {fits} (scheduler view, not a "
                              "load measurement)", free=per_node))
        elif missing:
            out.append(_check(cid, UNKNOWN, "allocatable or a pod request missing/unparseable",
                              free=per_node))
        else:
            out.append(_check(cid, FAIL, f"no dedicated node fits {want}", free=per_node))
    return out


def check_storage(snap: dict, b: dict) -> list[dict]:
    s = b["storage"]
    st, data = _q(snap, "storageclasses")
    if st != "ok":
        return [_unavailable("storage.workspace_class", snap, "storageclasses")]
    by = {x["metadata"]["name"]: x for x in data.get("items", [])}
    want = s.get("workspace_storage_class")
    if want in s.get("forbidden_storage_classes", []):
        return [_check("storage.workspace_class", FAIL, f"{want} is forbidden (ADR 0037 clone churn)")]
    sc = by.get(want)
    if not sc:
        return [_check("storage.workspace_class", FAIL, f"StorageClass {want} absent")]
    bad = []
    if s.get("workspace_provisioner") and sc.get("provisioner") != s["workspace_provisioner"]:
        bad.append(f"provisioner {sc.get('provisioner')}")
    if s.get("workspace_binding_mode") and sc.get("volumeBindingMode") != s["workspace_binding_mode"]:
        bad.append(f"bindingMode {sc.get('volumeBindingMode')}")
    if bad:
        return [_check("storage.workspace_class", FAIL, f"{want}: {', '.join(bad)}")]
    return [_check("storage.workspace_class", PASS,
                   f"{want} present ({sc.get('provisioner')}); behaviour/ceilings still unqualified")]


def check_runtimeclasses(snap: dict, b: dict, chosen: list[dict]) -> list[dict]:
    out = []
    st, data = _q(snap, "runtimeclasses")
    for alias, rc_name in (b.get("runtime_classes") or {}).items():
        cid = f"runtimeclass.{alias}"
        if st != "ok":
            out.append(_unavailable(cid, snap, "runtimeclasses"))
            continue
        rc = next((r for r in data.get("items", []) if r["metadata"]["name"] == rc_name), None)
        if not rc:
            out.append(_check(cid, FAIL, f"RuntimeClass {rc_name} absent"))
            continue
        sel = (rc.get("scheduling") or {}).get("nodeSelector") or {}
        forbidden = set(b["placement"].get("forbidden_node_labels", []))
        hits = sorted(forbidden & set(sel))
        if hits:
            out.append(_check(cid, FAIL, f"RuntimeClass {rc_name} pins pods onto {hits} (Relay/router "
                              "pool); this collector supports dedicated placement only"))
            continue
        lands = [n["metadata"]["name"] for n in chosen
                 if all(n["metadata"].get("labels", {}).get(k) == v for k, v in sel.items())]
        if not lands:
            out.append(_check(cid, FAIL, f"RuntimeClass {rc_name} selects no dedicated node ({sel})"))
        else:
            out.append(_check(cid, PASS, f"{rc_name} can schedule on {lands}; handler not exercised"))
    return out


def check_kubevirt(snap: dict, b: dict, chosen: list[dict]) -> list[dict]:
    pins, out = b["pins"], []
    if not pins.get("kubevirt_version") or not SHA256_DIGEST.fullmatch(
            str(pins.get("kubevirt_operator_digest") or "")):
        out.append(_check("kubevirt.pin", UNKNOWN, "no KubeVirt version + operator digest selected "
                          "(operator prerequisite K1 in the feasibility runbook)", source="binding"))
    else:
        out.append(_check("kubevirt.pin", PASS, pins["kubevirt_version"], source="binding"))
    st, _ = _q(snap, "kubevirt_crd")
    if st == "notfound":
        out.append(_check("kubevirt.installed", FAIL, "virtualmachines.kubevirt.io CRD absent"))
        return out + [_check("kubevirt.kvm_device", UNKNOWN,
                             "/dev/kvm is not visible through the API before KubeVirt's device plugin")]
    if st != "ok":
        return out + [_unavailable("kubevirt.installed", snap, "kubevirt_crd"),
                      _check("kubevirt.kvm_device", UNKNOWN, "KubeVirt state unknown")]
    st, data = _q(snap, "kubevirt_cr")
    if st != "ok":
        out.append(_unavailable("kubevirt.installed", snap, "kubevirt_cr"))
    else:
        items = data.get("items", [])
        dep = [k for k in items if k["status"].get("phase") == "Deployed"]
        ver = {k["status"].get("observedKubeVirtVersion") for k in dep}
        if not dep:
            out.append(_check("kubevirt.installed", FAIL, f"{len(items)} KubeVirt CR(s), none Deployed"))
        elif pins.get("kubevirt_version") and ver != {pins["kubevirt_version"]}:
            out.append(_check("kubevirt.installed", FAIL, f"observed {sorted(map(str, ver))} != pin"))
        elif not pins.get("kubevirt_version"):
            out.append(_check("kubevirt.installed", UNKNOWN, f"Deployed {sorted(map(str, ver))}, no pin"))
        else:
            out.append(_check("kubevirt.installed", PASS, f"Deployed {sorted(ver)} (presence only)"))
    kvm = [n["metadata"]["name"] for n in chosen
           if quantity(n["status"].get("allocatable", {}).get("devices.kubevirt.io/kvm", 0)) > 0]
    if kvm:
        out.append(_check("kubevirt.kvm_device", PASS, f"devices.kubevirt.io/kvm allocatable on {kvm}"))
    else:
        out.append(_check("kubevirt.kvm_device", UNKNOWN if not chosen else FAIL,
                          "no dedicated node advertises devices.kubevirt.io/kvm"))
    return out


GATES = {
    # Plan phase 4: lightweight canary only on a dedicated pool (co-location is out of scope here).
    "lightweight_canary_prerequisites": ["context.name", "context.api_server", "context.cluster_uid",
                                         "version.kubernetes", "version.cilium",
                                         "placement.dedicated_pool",
                                         "placement.protected_workloads_absent",
                                         "headroom.lightweight", "storage.workspace_class"],
    # Plan phase 1/5: the spike may START when this passes; its outcome is the live evidence.
    "kubevirt_spike_prerequisites": ["context.name", "context.api_server", "context.cluster_uid",
                                     "version.kubernetes", "version.cilium", "placement.dedicated_pool",
                                     "placement.protected_workloads_absent",
                                     "headroom.developer_vm", "kubevirt.pin"],
}


def _rollup(statuses: list[str]) -> str:
    if FAIL in statuses:
        return FAIL
    if UNKNOWN in statuses or not statuses:
        return UNKNOWN
    return PASS


def evaluate(snap: dict, b: dict) -> dict:
    errs = validate_binding(b)
    if snap.get("schema") != SNAPSHOT_SCHEMA:
        errs.append(f"snapshot schema must be {SNAPSHOT_SCHEMA}")
    if errs:
        return {"schema": REPORT_SCHEMA, "result": FAIL, "binding_errors": errs, "checks": [],
                "gates": {}, "capabilities": {}, "qualification": "not-established-by-preflight"}
    checks = check_context(snap, b) + check_versions(snap, b)
    pc, chosen = check_placement(snap, b)
    checks += pc
    checks += check_protected_workloads(snap, b, chosen, pc[0]["status"])
    checks += check_headroom(snap, b, chosen, pc[0]["status"])
    checks += check_storage(snap, b) + check_runtimeclasses(snap, b, chosen)
    checks += check_kubevirt(snap, b, chosen)
    by = {c["id"]: c["status"] for c in checks}
    gates = {g: {"status": _rollup([by.get(i, UNKNOWN) for i in ids]), "requires": ids}
             for g, ids in GATES.items()}
    caps = {k: {"status": v["status"], "source": "binding",
                **({"receipt_sha256": v["receipt_sha256"]} if v["status"] == "qualified" else {})}
            for k, v in b["capabilities"].items()}
    return {"schema": REPORT_SCHEMA, "context": snap.get("context"),
            "collected_at": snap.get("collected_at"), "result": _rollup(list(by.values())),
            "checks": checks, "gates": gates, "capabilities": caps,
            "qualification": "not-established-by-preflight",
            "note": "pass = prerequisite visible, not a qualification; capability status comes from the "
                    "binding and its evidence (docs/runbooks/relay-worker-kubevirt-feasibility.md)"}


def exit_code(report: dict) -> int:
    return {PASS: 0, FAIL: 1}.get(report["result"], 2)


def _print_text(r: dict) -> None:
    for e in r.get("binding_errors", []):
        print(f"[FAIL] binding: {e}")
    for c in r["checks"]:
        print(f"[{c['status'].upper():7}] {c['id']}: {c['detail']}")
    for g, v in r["gates"].items():
        print(f"gate {g}: {v['status']}")
    print(f"qualification: {r['qualification']}; result: {r['result']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--context", required=True)
    c.add_argument("--kubeconfig")
    c.add_argument("--out", required=True,
                   help="snapshot file (JSON) in an explicit retained evidence store; /tmp refused")
    e = sub.add_parser("evaluate")
    e.add_argument("--snapshot", required=True)
    e.add_argument("--binding", required=True)
    e.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        out = pathlib.Path(a.out).resolve()
        if any(out.is_relative_to(t) for t in (pathlib.Path("/tmp"), pathlib.Path("/var/tmp"),
                                               pathlib.Path("/dev/shm"))):
            print("refusing ephemeral --out: write evidence to an explicit retained store "
                  "(operator EVIDENCE_DIR, or /workspace/archive/<task>/ on a dev-worker)",
                  file=sys.stderr)
            return 2
        snap = collect(a.context, a.kubeconfig)
        pathlib.Path(a.out).write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
        bad = {k: v["status"] for k, v in snap["queries"].items()
               if v["status"] not in ("ok", "notfound")}
        print(f"wrote {a.out}: context_exists={snap['context_exists']} non-ok={bad}")
        return 0 if snap["context_exists"] and not bad else 2
    snap = json.loads(pathlib.Path(a.snapshot).read_text())
    rep = evaluate(snap, json.loads(pathlib.Path(a.binding).read_text()))
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        _print_text(rep)
    return exit_code(rep)


if __name__ == "__main__":
    sys.exit(main())
