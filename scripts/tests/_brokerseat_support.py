"""Shared stdlib-only readers for the three test_brokerseat_*.py modules.

NOT a test module (the `_` prefix keeps it outside `unittest discover -p "test_*.py"`); imported
by name because `unittest discover -s scripts/tests` puts this directory on sys.path.

Everything here is a TARGETED reader over the raw manifest text, on purpose: this repo's "Script
unit tests" CI step (.gitea/workflows/broker-inventory.yaml) installs no dependencies and the
runner has NO PyYAML (scripts/tests/test_cp_env.py, test_manifest_paths.py document this), so the
subject files are read with the same private block/field helpers scripts/gen-broker-inventory.py
uses to derive the seat inventory (loaded by path, its filename is hyphenated) plus a few regexes
over the exact layouts brokerseat-{crd,rbac,admission}.yaml are written in. A layout these readers
cannot parse unambiguously raises, so a reshaped file fails a test loudly rather than matching
nothing. No cluster, no docker, no network.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
BROKER_DIR = REPO / "kubernetes/apps/infrastructure/agentforge-broker"
CRD_FILE = BROKER_DIR / "brokerseat-crd.yaml"
RBAC_FILE = BROKER_DIR / "brokerseat-rbac.yaml"
ADMISSION_FILE = BROKER_DIR / "brokerseat-admission.yaml"
KUSTOMIZATION = BROKER_DIR / "kustomization.yaml"
HARNESS = REPO / "scripts/check-seat-guard-cel.py"
WORKFLOW = REPO / ".gitea/workflows/tenant-guard-cel.yaml"
NEW_FILES = ("brokerseat-crd.yaml", "brokerseat-rbac.yaml", "brokerseat-admission.yaml")

CP_SA = ("agentforge-platform", "agentforge")
PROVISIONER_SA = ("agentforge-provisioner", "openbao")
CP_USERNAME = "system:serviceaccount:agentforge:agentforge-platform"
PROVISIONER_USERNAME = "system:serviceaccount:openbao:agentforge-provisioner"

_MOD_PATH = REPO / "scripts" / "gen-broker-inventory.py"
_spec = importlib.util.spec_from_file_location("gen_broker_inventory", _MOD_PATH)
gbi = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("gen_broker_inventory", gbi)
_spec.loader.exec_module(gbi)  # performs no I/O at import time


def docs(path: pathlib.Path) -> list[str]:
    """Non-empty YAML documents of `path`, comment lines stripped (the readers below key on layout,
    and a commented-out rule must never count as a rule)."""
    out = []
    for doc in gbi._docs(path.read_text(encoding="utf-8")):
        body = "\n".join(l for l in doc.splitlines() if not l.lstrip().startswith("#"))
        if body.strip():
            out.append(body)
    return out


def kind(doc: str) -> str | None:
    return gbi._kind(doc)


def name(doc: str) -> str | None:
    return gbi._name(doc)


def namespace(doc: str) -> str | None:
    return gbi._field(gbi._top_block(doc, "metadata"), "namespace", 2)


def flow_list(raw: str) -> list[str]:
    """`["get", "list"]` / `[bseat]` -> ["get", "list"] / ["bseat"]."""
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise ValueError(f"not a flow list: {raw!r}")
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [p.strip().strip('"').strip("'") for p in inner.split(",")]


# -- RBAC -------------------------------------------------------------------------------------

_RULE = re.compile(
    r"^\s*-\s*apiGroups:\s*(\[.*?\])\s*\n"
    r"\s*resources:\s*(\[.*?\])\s*\n"
    r"(?:\s*resourceNames:\s*(\[.*?\])\s*\n)?"
    r"\s*verbs:\s*(\[.*?\])\s*$",
    re.M,
)


def role_rules(doc: str) -> dict[tuple[str, str], frozenset[str]]:
    """{(apiGroup, resource): verbs} for a Role document, failing if any rule is not the
    apiGroups/resources[/resourceNames]/verbs flow-list layout this file is written in."""
    block = gbi._top_block(doc, "rules")
    entries = [l for l in block.splitlines() if re.match(r"^\s*-\s*apiGroups:", l)]
    matches = list(_RULE.finditer(block))
    if len(matches) != len(entries) or not entries:
        raise AssertionError(f"could not parse every rule of Role {name(doc)!r} ({len(matches)}/{len(entries)})")
    out: dict[tuple[str, str], frozenset[str]] = {}
    for m in matches:
        groups, resources, names, verbs = m.groups()
        if names is not None:
            raise AssertionError(f"Role {name(doc)!r} uses resourceNames; the readers here do not model it")
        for g in flow_list(groups):
            for r in flow_list(resources):
                key = (g, r)
                if key in out:
                    raise AssertionError(f"Role {name(doc)!r} grants {key} twice")
                out[key] = frozenset(flow_list(verbs))
    return out


def role_ref(doc: str) -> tuple[str, str]:
    block = gbi._top_block(doc, "roleRef")
    return (gbi._field(block, "kind", 2) or "", gbi._field(block, "name", 2) or "")


def subjects(doc: str) -> list[tuple[str, str, str]]:
    """[(kind, name, namespace)] of a RoleBinding's subjects."""
    block = gbi._top_block(doc, "subjects")
    out = []
    for m in re.finditer(r"-\s*kind:\s*(\S+)\s*\n\s*name:\s*(\S+)\s*\n\s*namespace:\s*(\S+)", block):
        out.append((m.group(1), m.group(2), m.group(3)))
    if not out:
        raise AssertionError(f"could not parse subjects of RoleBinding {name(doc)!r}")
    return out


def service_account_docs(path: pathlib.Path) -> set[tuple[str, str]]:
    """{(name, namespace)} of every ServiceAccount document in `path`."""
    return {
        (name(d) or "", namespace(d) or "")
        for d in docs(path)
        if kind(d) == "ServiceAccount"
    }


# -- ValidatingAdmissionPolicy -------------------------------------------------------------------


def _entries(block: str, indent: int) -> list[str]:
    """Split a block of `- key: ...` list entries at `indent` into their texts."""
    entries: list[list[str]] = []
    for line in block.splitlines():
        if re.match(rf"^[ ]{{{indent}}}-\s", line):
            entries.append([line])
        elif entries and (not line.strip() or len(line) - len(line.lstrip(" ")) > indent):
            entries[-1].append(line)
        elif line.strip():
            raise AssertionError(f"unexpected line at indent {indent}: {line!r}")
    return ["\n".join(e) for e in entries]


def _scalar_or_folded(entry: str, key: str) -> str:
    """The value of `key:` inside one list entry: a quoted one-liner or a `>-` folded block."""
    m = re.search(rf"(?m)^\s*(?:-\s*)?{re.escape(key)}:\s*(.*)$", entry)
    if not m:
        raise AssertionError(f"no {key!r} in entry:\n{entry}")
    head = m.group(1).strip()
    if head in (">-", ">", "|", "|-"):
        rest = entry[m.end():].splitlines()
        body = []
        for line in rest:
            if not line.strip():
                continue
            if re.match(r"^\s*(?:-\s*)?message:", line):
                break
            body.append(line.strip())
        return " ".join(body)
    if len(head) >= 2 and head[0] == head[-1] and head[0] in "\"'":
        return head[1:-1]  # ONE layer of matching quotes — the expression's own quotes stay
    return head


def vap(doc: str) -> dict[str, Any]:
    """The parts of a ValidatingAdmissionPolicy the tests assert on."""
    spec = gbi._top_block(doc, "spec")
    mc_block = gbi._sub_block(spec, "matchConstraints", 2)
    rules = []
    for entry in _entries(gbi._sub_block(mc_block, "resourceRules", 4), 6):
        fields = {}
        for k in ("apiGroups", "apiVersions", "operations", "resources"):
            fm = re.search(rf"(?m)^\s*(?:-\s*)?{k}:\s*(\[.*?\])\s*$", entry)
            if not fm:
                raise AssertionError(f"resourceRule without {k}: {entry!r}")
            fields[k] = flow_list(fm.group(1))
        # narrowing keys the CEL harness refuses (scope: Cluster stops matching namespaced requests;
        # resourceNames narrows by name) — surfaced so a stdlib test can pin their absence
        fields["narrowingKeys"] = sorted(
            k for k in ("scope", "resourceNames")
            if re.search(rf"(?m)^\s*(?:-\s*)?{k}:", entry)
        )
        rules.append(fields)
    conditions = [
        {"name": _scalar_or_folded(e, "name"), "expression": _scalar_or_folded(e, "expression")}
        for e in _entries(gbi._sub_block(spec, "matchConditions", 2), 4)
    ]
    validations = [_scalar_or_folded(e, "expression") for e in _entries(gbi._sub_block(spec, "validations", 2), 4)]
    variables = {
        _scalar_or_folded(e, "name"): _scalar_or_folded(e, "expression")
        for e in _entries(gbi._sub_block(spec, "variables", 2), 4)
    }
    narrowing = {
        key: bool(gbi._sub_block(mc_block, key, 4).strip()) or gbi._field(mc_block, key, 4) is not None
        for key in ("namespaceSelector", "objectSelector", "excludeResourceRules", "matchPolicy")
    }
    return {
        "name": name(doc),
        "failurePolicy": gbi._field(spec, "failurePolicy", 2),
        "paramKind": bool(gbi._sub_block(spec, "paramKind", 2).strip()),
        "resourceRules": rules,
        "matchConditions": conditions,
        "variables": variables,
        "validations": validations,
        "narrowing": narrowing,
    }


def cel_string_list(expression: str) -> set[str]:
    """`['a', 'b']` (possibly folded over lines) -> {'a', 'b'}; raises on any other shape."""
    m = re.fullmatch(r"\[\s*((?:'[^']*'\s*,?\s*)+)\]", expression.strip())
    if not m:
        raise AssertionError(f"not a CEL string-list literal: {expression!r}")
    return {p.strip().strip("'") for p in m.group(1).split(",") if p.strip()}


def binding(doc: str) -> dict[str, Any]:
    spec = gbi._top_block(doc, "spec")
    actions = re.search(r"(?m)^  validationActions:\s*(\[.*?\])\s*$", spec)
    return {
        "name": name(doc),
        "policyName": gbi._field(spec, "policyName", 2),
        "validationActions": flow_list(actions.group(1)) if actions else None,
        "matchResources": bool(gbi._sub_block(spec, "matchResources", 2).strip()),
        "paramRef": bool(gbi._sub_block(spec, "paramRef", 2).strip()),
    }


def policies() -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in (vap(d) for d in docs(ADMISSION_FILE) if kind(d) == "ValidatingAdmissionPolicy")}


def bindings() -> dict[str, dict[str, Any]]:
    return {b["name"]: b for b in (binding(d) for d in docs(ADMISSION_FILE) if kind(d) == "ValidatingAdmissionPolicyBinding")}


def rule_covers(rule: dict[str, Any], group: str, resource: str) -> bool:
    return ("*" in rule["apiGroups"] or group in rule["apiGroups"]) and (
        "*" in rule["resources"] or resource in rule["resources"]
    )


# -- CRD ---------------------------------------------------------------------------------------

_CEL_RULE = re.compile(r'(?m)^([ ]*)-\s*rule:\s*"(.*)"\s*$')


def crd_rules() -> list[tuple[int, str]]:
    """[(indent, rule)] of every x-kubernetes-validations rule in the CRD, in file order."""
    (doc,) = [d for d in docs(CRD_FILE) if kind(d) == "CustomResourceDefinition"]
    out = [(len(m.group(1)), m.group(2)) for m in _CEL_RULE.finditer(doc)]
    if not out:
        raise AssertionError("no x-kubernetes-validations rules found in the CRD")
    return out


def crd_doc() -> str:
    (doc,) = [d for d in docs(CRD_FILE) if kind(d) == "CustomResourceDefinition"]
    return doc


def yaml_double_quoted(raw: str) -> str:
    """Undo the escapes a YAML double-quoted scalar applies (only `\\\\` occurs in these files)."""
    return raw.replace("\\\\", "\\")
