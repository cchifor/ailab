"""Content check of a targeted Talos control-plane plan (docs/runbooks/relay-researcher-r7-audit-policy.md).

talos provider 0.12 makes `machine_configuration_input` write-only (`machine_configuration_input_wo`), so a
plan can no longer show whether the input changed. This compares content instead and prints key PATHS only,
never values (the machine configs hold cluster secrets):

  (a) the control plane's live MachineConfig vs the state's `machine_configuration` (plan `before`);
  (b) that `before` vs the planned `after`.

PASS only if no other resource changes, (a) is empty and (b) is exactly the expected paths (default: the
audit policy rules, for both the apply and the removal of the R7 audit policy).

Usage: talos-cp-plan-check.py PLANJSON MCJSON NODEKEY [EXPECTED_PATH ...]
  PLANJSON  `tofu show -json <saved targeted plan>`
  MCJSON    `talosctl -n <cp> get machineconfig -o json` (one object, id v1alpha1; .spec is a string
            holding the multi-document config)
  NODEKEY   cp1, cp2 or cp3 (the `talos_machine_configuration_apply.cp` key)
Exit 0 on PASS, 1 on STOP.

Docs are keyed by (kind, name), the v1alpha1 document as "v1alpha1". Lists are compared element-wise
when their lengths are equal, otherwise reported as one changed path (so a rules list going from 1 to 3
entries is the single path .../auditPolicy/rules).
"""
import json
import sys

import yaml

DEFAULT_EXPECTED = ["v1alpha1~/cluster/apiServer/auditPolicy/rules"]


def docs(s):
    out = {}
    for i, d in enumerate(yaml.safe_load_all(s)):
        if d is None:
            continue
        out[(d.get("kind") or ("v1alpha1" if "machine" in d else f"doc{i}"), d.get("name", ""))] = d
    return out


def diff(a, b, p=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                yield f"+{p}/{k}"
            elif k not in b:
                yield f"-{p}/{k}"
            else:
                yield from diff(a[k], b[k], f"{p}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, f"{p}[{i}]")
    elif a != b:
        yield f"~{p}"


def cmp(x, y):
    dx, dy = docs(x), docs(y)
    out = []
    for k in sorted(set(dx) | set(dy), key=str):
        if k not in dx:
            out.append(f"+doc {k}")
        elif k not in dy:
            out.append(f"-doc {k}")
        else:
            out += [f"{k[0]}{line}" for line in diff(dx[k], dy[k])]
    return out


def check(plan, mc_text, node, expected):
    address = f'talos_machine_configuration_apply.cp["{node}"]'
    rc = [r for r in plan["resource_changes"] if r["address"] == address]
    others = [r["address"] for r in plan["resource_changes"]
              if r["change"]["actions"] != ["no-op"] and r["address"] != address]
    if len(rc) != 1:
        return False, [f"STOP: {address} is not exactly one resource change in the plan"]
    live = json.JSONDecoder().raw_decode(mc_text.lstrip())[0]["spec"]
    before = (rc[0]["change"].get("before") or {}).get("machine_configuration")
    after = (rc[0]["change"].get("after") or {}).get("machine_configuration")
    if not isinstance(before, str):
        return False, [f"STOP: {address} has no machine_configuration in its state (fresh or tainted)"]
    if not isinstance(after, str):
        return False, [f"STOP: the planned machine_configuration of {address} is unknown"]
    a, b = cmp(live, before), cmp(before, after)
    lines = [f"other resources changing: {others or 'none'}",
             f"live vs state-before: {a or 'IDENTICAL'}",
             f"state-before vs planned: {b}"]
    return (not others and not a and b == expected), lines


def main(argv):
    if len(argv) < 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: talos-cp-plan-check.py PLANJSON MCJSON NODEKEY [EXPECTED_PATH ...]", file=sys.stderr)
        return 2
    plan = json.load(open(argv[1], encoding="utf-8"))
    mc_text = open(argv[2], encoding="utf-8").read()
    ok, lines = check(plan, mc_text, argv[3], argv[4:] or DEFAULT_EXPECTED)
    for line in lines:
        print(line)
    print("VERDICT:", "PASS" if ok else "STOP")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
