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
    return cmp_docs(docs(x), docs(y))


def cmp_docs(dx, dy):
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
    changes = plan.get("resource_changes") or []
    rc = [r for r in changes if r["address"] == address]
    others = [r["address"] for r in changes
              if r["change"]["actions"] != ["no-op"] and r["address"] != address]
    if len(rc) != 1:
        return False, [f"STOP: {address} is not exactly one resource change in the plan"]
    actions = rc[0]["change"]["actions"]
    if actions != ["update"]:
        # A replace (delete+create) would destroy the apply resource first, whatever its content diff.
        return False, [f"STOP: {address} is not an in-place update: actions {actions}"]
    try:
        live = json.JSONDecoder().raw_decode(mc_text.lstrip())[0]["spec"]
    except (ValueError, KeyError, TypeError, IndexError):
        return False, ["STOP: could not read the live MachineConfig JSON"]
    before = (rc[0]["change"].get("before") or {}).get("machine_configuration")
    after = (rc[0]["change"].get("after") or {}).get("machine_configuration")
    if not isinstance(before, str):
        return False, [f"STOP: {address} has no machine_configuration in its state (fresh or tainted)"]
    if not isinstance(after, str):
        return False, [f"STOP: the planned machine_configuration of {address} is unknown"]
    if not isinstance(live, str):
        return False, ["STOP: the live MachineConfig spec is not a config document string"]
    # PyYAML's errors quote the offending line, which may hold a secret: report only which one failed.
    parsed = {}
    for name, text in (("live", live), ("state", before), ("planned", after)):
        try:
            parsed[name] = docs(text)
        except yaml.YAMLError:
            return False, [f"STOP: could not parse the {name} config"]
    a, b = cmp_docs(parsed["live"], parsed["state"]), cmp_docs(parsed["state"], parsed["planned"])
    lines = [f"other resources changing: {others or 'none'}",
             f"live vs state-before: {a or 'IDENTICAL'}",
             f"state-before vs planned: {b}"]
    return (not others and not a and b == expected), lines


def main(argv):
    if len(argv) < 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: talos-cp-plan-check.py PLANJSON MCJSON NODEKEY [EXPECTED_PATH ...]", file=sys.stderr)
        return 2
    try:
        plan = json.load(open(argv[1], encoding="utf-8"))
        mc_text = open(argv[2], encoding="utf-8").read()
    except (OSError, ValueError):
        # The JSON decoder's message quotes no input, but keep the boundary value-free regardless.
        print("STOP: could not read the plan JSON or the MachineConfig file")
        print("VERDICT: STOP")
        return 1
    try:
        ok, lines = check(plan, mc_text, argv[3], argv[4:] or DEFAULT_EXPECTED)
    except Exception as error:  # never let a traceback print config content
        print(f"STOP: unexpected {type(error).__name__} while checking (no details printed)")
        print("VERDICT: STOP")
        return 1
    for line in lines:
        print(line)
    print("VERDICT:", "PASS" if ok else "STOP")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
