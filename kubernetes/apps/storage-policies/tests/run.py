#!/usr/bin/env python3
"""Offline fixtures for trident-attacher-timeout.yaml, run through the Kyverno CLI test harness.

    python kubernetes/apps/storage-policies/tests/run.py [--kyverno /path/to/kyverno]

Each case takes base-deployment.json (the operator's rendering of trident/trident-controller with the
stock 60s attacher timeout), applies a transformation, writes a one-case `kyverno test` directory and
checks the verdict: mutated cases must PASS with the expected patched resource (the harness compares
the engine's output with `patchedResource` field by field), no-op and unmatched cases must SKIP.

`kyverno apply` cannot be used here: its static variable check treats the foreach variables
(`element`, `element0`, `elementIndex0/1`) as "required variables not provided" and skips the policy;
the `test` harness runs the real engine. The policy's match is exact (trident/trident-controller), so
these fixtures are the only way to exercise the rule offline; the integration proof is the
server-side dry-run in docs/runbooks/qnap-storage-setup.md § "Attach/detach timeout".

Needs: the kyverno CLI (1.13.x) on PATH or via --kyverno, and PyYAML.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = pathlib.Path(__file__).resolve().parent
POLICY = HERE.parent / "trident-attacher-timeout.yaml"
POLICY_NAME = "trident-attacher-timeout"
RULE = "raise-csi-attacher-timeout"
BASE = json.loads((HERE / "base-deployment.json").read_text(encoding="utf-8"))


def containers(obj):
    return obj["spec"]["template"]["spec"]["containers"]


def attacher(obj):
    return next(c for c in containers(obj) if c["name"] == "csi-attacher")


def with_attacher_args(obj, args):
    attacher(obj)["args"] = args
    return obj


def case_60s(o):
    return o


def case_already_600s(o):
    return with_attacher_args(o, [a.replace("60s", "600s") if a.startswith("--timeout=") else a for a in attacher(o)["args"]])


def case_containers_reordered(o):
    containers(o).reverse()
    return o


def case_args_reordered(o):
    attacher(o)["args"].reverse()
    return o


def case_duplicate_flag(o):
    return with_attacher_args(o, attacher(o)["args"] + ["--timeout=30s"])


def case_absent_flag(o):
    return with_attacher_args(o, [a for a in attacher(o)["args"] if not a.startswith("--timeout=")])


def case_split_flag(o):
    args = []
    for a in attacher(o)["args"]:
        if a.startswith("--timeout="):
            args += ["--timeout", a.split("=", 1)[1]]
        else:
            args.append(a)
    return with_attacher_args(o, args)


def case_no_args(o):
    attacher(o).pop("args", None)
    return o


def case_sidecar_renamed(o):
    attacher(o)["name"] = "external-attacher"
    return o


def case_other_name(o):
    o["metadata"]["name"] = "trident-node"
    return o


def case_other_namespace(o):
    o["metadata"]["namespace"] = "kube-system"
    return o


def expected_mutation(before):
    """Attacher --timeout=* → 600s; every other container and argument identical."""
    after = copy.deepcopy(before)
    for c in containers(after):
        if c["name"] == "csi-attacher" and "args" in c:
            c["args"] = [("--timeout=600s" if x.startswith("--timeout=") else x) for x in c["args"]]
    return after


# (name, transform, expected harness result)
CASES = [
    ("60s -> 600s", case_60s, "pass"),
    # an already-600s value is replaced with itself: no effective patch, which the harness reports
    # as "skip" — that IS the idempotence we want (nothing rewritten, nothing rolled)
    ("already 600s (idempotent)", case_already_600s, "skip"),
    ("containers reordered", case_containers_reordered, "pass"),
    ("args reordered", case_args_reordered, "pass"),
    ("duplicate flag (both set)", case_duplicate_flag, "pass"),
    ("absent flag (no-op)", case_absent_flag, "skip"),
    ("split --timeout 60s (no-op)", case_split_flag, "skip"),
    ("no args (no-op)", case_no_args, "skip"),
    ("sidecar renamed (no-op)", case_sidecar_renamed, "skip"),
    ("other Deployment name (unmatched)", case_other_name, "skip"),
    ("other namespace (unmatched)", case_other_namespace, "skip"),
]

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run_case(kyverno, workdir, name, before, expected):
    d = workdir / re.sub(r"[^a-z0-9]+", "-", name.lower())
    d.mkdir()
    shutil.copy(POLICY, d / "policy.yaml")
    (d / "resource.yaml").write_text(yaml.safe_dump(before), encoding="utf-8")
    ns, res = before["metadata"]["namespace"], before["metadata"]["name"]
    result = {
        "policy": POLICY_NAME,
        "rule": RULE,
        "kind": "Deployment",
        "namespace": ns,
        "resources": [f"{ns}/{res}"],
        "result": expected,
    }
    if expected == "pass":
        (d / "patched.yaml").write_text(yaml.safe_dump(expected_mutation(before)), encoding="utf-8")
        result["patchedResource"] = "patched.yaml"
    (d / "kyverno-test.yaml").write_text(
        yaml.safe_dump({
            "apiVersion": "cli.kyverno.io/v1alpha1",
            "kind": "Test",
            "metadata": {"name": "attacher"},
            "policies": ["policy.yaml"],
            "resources": ["resource.yaml"],
            "results": [result],
        }),
        encoding="utf-8",
    )
    p = subprocess.run([kyverno, "test", str(d)], capture_output=True, text=True)
    out = ANSI.sub("", p.stdout + p.stderr)
    ok = p.returncode == 0 and "0 tests failed" in out and "invalid policy" not in out
    reason = "" if ok else next((l.strip() for l in out.splitlines() if "│" in l and ("Fail" in l or "Pass" in l)), out[-400:])
    return ok, reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kyverno", default=shutil.which("kyverno") or "kyverno")
    a = ap.parse_args()
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        for name, transform, expected in CASES:
            before = transform(copy.deepcopy(BASE))
            ok, reason = run_case(a.kyverno, pathlib.Path(td), name, before, expected)
            failed += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expect {expected}){'' if ok else ': ' + reason}")
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
