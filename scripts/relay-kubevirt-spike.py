#!/usr/bin/env python3
"""Operator-run KubeVirt feasibility spike bundle: deterministic DRY render only. Never applies.

    python3 scripts/relay-kubevirt-spike.py render-operator \
        --source-dir .data/public-pins --out-dir "$EVIDENCE_DIR/kv-spike-bundle"

Reads the exact upstream downloads pinned in docs/relay-workers/runtime-pins.json, fails on any hash
change, unknown resource, tag-only component image or placement outside the dedicated pool, and
writes 10-operator.yaml, 20-kubevirt-cr.yaml and bundle-metadata.json (upstream and rendered
SHA-256). Applying is a separate operator step in docs/runbooks/relay-worker-kubevirt-feasibility.md.
No cluster access, no credentials. This is not the Relay production KubeVirt renderer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import datetime as dt  # noqa: E402

from relay_kubevirt_spike import evidence as gev  # noqa: E402
from relay_kubevirt_spike import guest, pins_check, workload  # noqa: E402
from relay_kubevirt_spike import operator_bundle as ob  # noqa: E402

PINS = ROOT / "docs/relay-workers/runtime-pins.json"
METADATA_SCHEMA = "ailab.relay-kubevirt-spike-bundle/v1alpha1"


def dump(docs: list[dict]) -> str:
    return "".join("---\n" + yaml.safe_dump(d, sort_keys=True, default_flow_style=False) for d in docs)


def render_operator_bundle(pins_path: pathlib.Path, source_dir: pathlib.Path) -> dict[str, str]:
    pins = json.loads(pins_path.read_text())["kubevirt"]
    bundle = pins["spike_bundle"]
    if bundle["components"][0]["image"] != pins["operator_image"]:
        raise ob.BundleError("spike_bundle operator image differs from kubevirt.operator_image")
    sources = ob.verify_sources(bundle, source_dir)
    env = ob.component_env(bundle)
    operator_docs = ob.render_operator(sources["kubevirt-operator-v1.9.1.yaml"].read_text(), env)
    cr = ob.render_cr(sources["kubevirt-cr-v1.9.1.yaml"].read_text())
    ob.check_rendered(operator_docs, cr)
    files = {"10-operator.yaml": dump(operator_docs), "20-kubevirt-cr.yaml": dump([cr])}
    meta = {
        "schema": METADATA_SCHEMA,
        "status": "rendered-not-applied-not-qualified",
        "kubevirt_version": pins["version"],
        "upstream_sha256": {s["file"]: s["sha256"] for s in bundle["sources"]},
        "rendered_sha256": {n: hashlib.sha256(t.encode()).hexdigest() for n, t in sorted(files.items())},
        "component_images": {e["name"]: e["value"] for e in env},
        "placement": {"node_selector": {ob.POOL_LABEL: ob.POOL_VALUE}, "toleration": ob.TOLERATION,
                      "forbidden_node_labels": list(ob.FORBIDDEN_NODE_LABELS)},
        "disabled_feature_gates": ob.DISABLED_GATES,
        "deviations_from_upstream": [
            "virt-operator replicas 2 -> 1: the spike pool is one dedicated node; a second replica "
            "could only stay Pending (soft anti-affinity aside) and must never fall back elsewhere",
            "virt-operator image and VIRT_OPERATOR_IMAGE pinned by digest; every child image env "
            "read by v1.9.1 util/config.go pinned by digest",
            "control-plane affinity/tolerations replaced by dedicated relay-worker placement",
            "KubeVirt spec.infra (replicas 1) and spec.workloads restricted to the dedicated pool",
            "Beta gates Template/Snapshot/ImageVolume/DeclarativeHotplugVolumes disabled; "
            "evictionStrategy None (no live migration)",
        ],
        "cluster_scoped_rbac_note": "Upstream ClusterRoles/ClusterRoleBinding, CRD and PriorityClass "
            "are preserved unchanged: virt-operator installs cluster-wide CRDs, webhooks, APIServices "
            "and RBAC for its components. This is not a namespaced-only install.",
    }
    files["bundle-metadata.json"] = json.dumps(meta, indent=2, sort_keys=True) + "\n"
    return files


def _bundle(pins_path: pathlib.Path) -> dict:
    return json.loads(pins_path.read_text())["kubevirt"]["spike_bundle"]


def _launcher(bundle: dict) -> str:
    return next(c["image"] for c in bundle["components"] if c["env"] == "VIRT_LAUNCHER_IMAGE")


def render_workload_bundle(pins_path: pathlib.Path, evidence: dict, now: dt.datetime) -> dict[str, str]:
    bundle = _bundle(pins_path)
    launcher = _launcher(bundle)
    ev = gev.validate(evidence, launcher_image=launcher, now=now)
    if ev["guest_image"] != bundle["guest_candidate"]["image"]:
        raise ob.BundleError("evidence guest_image is not the pinned guest candidate")
    docs = workload.render(ev, launcher, guest.cloud_init(bundle["compose_smoke_image"]["image"]))
    text = dump(docs)
    meta = {"schema": METADATA_SCHEMA + "+workload", "status": "rendered-not-applied-not-qualified",
            "rendered_sha256": {"30-workload.yaml": hashlib.sha256(text.encode()).hexdigest()},
            "evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
            "guest_image": ev["guest_image"], "prep_image": launcher, "launcher_uid": ev["launcher_uid"]}
    return {"30-workload.yaml": text, "workload-metadata.json": json.dumps(meta, indent=2, sort_keys=True) + "\n"}


def _write(out_dir: pathlib.Path, files: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text)
        print(f"{hashlib.sha256(text.encode()).hexdigest()}  {out_dir / name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render-operator", help="dry-render the pinned operator + CR bundle")
    r.add_argument("--source-dir", type=pathlib.Path, required=True)
    r.add_argument("--out-dir", type=pathlib.Path, required=True)
    r.add_argument("--pins", type=pathlib.Path, default=PINS)
    t = sub.add_parser("evidence-template", help="print the guest-evidence skeleton to fill from measurements")
    t.add_argument("--pins", type=pathlib.Path, default=PINS)
    m = sub.add_parser("measure-guest-layer", help="hash-check + measure an operator-downloaded guest layer blob")
    m.add_argument("--blob", type=pathlib.Path, required=True)
    m.add_argument("--pins", type=pathlib.Path, default=PINS)
    w = sub.add_parser("render-workload", help="dry-render S2 artifacts; refuses without valid evidence")
    w.add_argument("--evidence", type=pathlib.Path, required=True)
    w.add_argument("--out-dir", type=pathlib.Path, required=True)
    w.add_argument("--pins", type=pathlib.Path, default=PINS)
    v = sub.add_parser("verify-image-pins", help="read-only HTTPS: re-resolve each pinned tag and compare digests")
    v.add_argument("--pins", type=pathlib.Path, default=PINS)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "verify-image-pins":
            rows = pins_check.check(_bundle(args.pins))
            print(json.dumps(rows, indent=2))
            # A moved tag is a FAIL: the pin stays authoritative, but the drift must be reviewed.
            return 0 if all(r["status"] == "pass" for r in rows) else 1
        if args.cmd == "render-operator":
            _write(args.out_dir, render_operator_bundle(args.pins, args.source_dir))
        elif args.cmd == "evidence-template":
            b = _bundle(args.pins)
            print(json.dumps(gev.template(b["guest_candidate"]["image"], _launcher(b)), indent=2))
        elif args.cmd == "measure-guest-layer":
            layer = _bundle(args.pins)["guest_candidate"]["amd64_layer"]
            print(json.dumps(guest.measure_layer(args.blob, layer["digest"]), indent=2, sort_keys=True))
        else:
            ev = json.loads(args.evidence.read_text())
            _write(args.out_dir, render_workload_bundle(args.pins, ev, dt.datetime.now(dt.timezone.utc)))
    except (ob.BundleError, KeyError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
