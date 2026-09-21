#!/usr/bin/env python3
"""Fail when the dev-worker SLOT enumerations disagree across the repo.

A dev-worker "slot" (dev-worker-N / dwN) is named in many places — OpenBao AppRoles, the two
credential syncs, the per-slot ServiceAccounts in three trees, tofu, the ansible inventory — and a
slot retirement (plans/2026-09-21-retire-dev-workers-3-6-plan.md) or addition has to touch every
one of them. This is the gate codex asked for in the ADR 0028 plan review: it extracts the LIVE and
RETIRED slot sets from each file with a narrow, file-specific regex and prints every source next to
its set, so a missed edit names the file rather than showing up as a red sync a day later.

Run from the repo root (CI: .gitea/workflows/manifests.yaml). Exit 1 on any disagreement, on a file
that yields no slots (a regex that silently stopped matching is the same failure), or on a slot that
is both live and retired anywhere.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

K8STOKEN = ROOT / "kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml"
PROVISION = ROOT / "kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml"
PA_RBAC = ROOT / "kubernetes/apps/infrastructure/platform-access/rbac.yaml"
PA_PGSYNC = ROOT / "kubernetes/apps/infrastructure/platform-access/pg-sync.yaml"
TEP = ROOT / "kubernetes/apps/infrastructure/testpool/tep-access.yaml"
HELMTEST_NS = ROOT / "kubernetes/apps/infrastructure/helmtest/namespaces.yaml"
TOFU = ROOT / "kubernetes/infra/dev-workers/variables.tf"
INVENTORY = ROOT / "inventory/hosts.yml"


def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.exit("%s: missing" % path.relative_to(ROOT))


def ints(strings):
    return frozenset(int(s) for s in strings)


def live_sets():
    """name -> set of live slot numbers, one entry per enumeration (a file can contribute several)."""
    out = {}
    s = read(K8STOKEN)
    m = re.search(r"^\s*for _n in \(([\d,\s]+)\):", s, re.M)
    if m:
        out["k8stoken-sync.yaml: for _n in (...)"] = ints(re.findall(r"\d+", m.group(1)))
    s = read(PROVISION)
    m = re.search(r"^\s*for host in ((?:dev-worker-\d+\s*)+); do", s, re.M)
    if m:
        out["devworker-provision-job.yaml: for host in ..."] = ints(re.findall(r"dev-worker-(\d+)", m.group(1)))
    s = read(PA_RBAC)
    out["platform-access/rbac.yaml: ServiceAccounts"] = ints(
        re.findall(r"^metadata: \{ name: platform-dw(\d+), namespace: platform-access \}", s, re.M))
    m = re.search(r'resourceNames: \[([^\]]*platform-dw[^\]]*)\]', s)
    if m:
        out["platform-access/rbac.yaml: mint resourceNames"] = ints(re.findall(r"platform-dw(\d+)", m.group(1)))
    for ns_match in re.finditer(r"kind: RoleBinding\nmetadata:\n  name: dev-worker-platform-observer\n  namespace: (\S+)\nsubjects:\n((?:  - \{[^\n]*\}\n)+)", s):
        out["platform-access/rbac.yaml: RoleBinding subjects in %s" % ns_match.group(1)] = ints(
            re.findall(r"name: platform-dw(\d+)", ns_match.group(2)))
    s = read(PA_PGSYNC)
    for m in re.finditer(r'\{ name: LIVE_SLOTS, value: "([\d ]+)" \}', s):
        out.setdefault("platform-access/pg-sync.yaml: LIVE_SLOTS", ints(m.group(1).split()))
        if out["platform-access/pg-sync.yaml: LIVE_SLOTS"] != ints(m.group(1).split()):
            sys.exit("platform-access/pg-sync.yaml: the CronJob and the bootstrap Job disagree on LIVE_SLOTS")
    s = read(TEP)
    out["testpool/tep-access.yaml: ServiceAccounts"] = ints(
        re.findall(r"^metadata: \{ name: tep-dw(\d+), namespace: testpool \}", s, re.M))
    s = read(HELMTEST_NS)
    out["helmtest/namespaces.yaml: Namespaces"] = ints(
        re.findall(r"^kind: Namespace\nmetadata:\n  name: helmtest-dw(\d+)$", s, re.M))
    s = read(TOFU)
    out["infra/dev-workers/variables.tf: map keys"] = ints(re.findall(r'^\s{4}"dev-worker-(\d+)" = \{', s, re.M))
    s = read(INVENTORY)
    out["inventory/hosts.yml: dev_workers hosts"] = ints(re.findall(r"^\s{8}dev-worker-(\d+):\s*$", s, re.M))
    return out


def retired_sets():
    out = {}
    s = read(PROVISION)
    m = re.search(r'^\s*RETIRED_SLOTS="([^"]*)"', s, re.M)
    if m:
        out["devworker-provision-job.yaml: RETIRED_SLOTS"] = ints(re.findall(r"dev-worker-(\d+)", m.group(1)))
    s = read(PA_PGSYNC)
    vals = set(re.findall(r'\{ name: RETIRED_SLOTS, value: "([\d ]*)" \}', s))
    if len(vals) > 1:
        sys.exit("platform-access/pg-sync.yaml: the CronJob and the bootstrap Job disagree on RETIRED_SLOTS")
    if vals:
        out["platform-access/pg-sync.yaml: RETIRED_SLOTS"] = ints(vals.pop().split())
    return out


def main():
    live, retired = live_sets(), retired_sets()
    bad = False
    for name, slots in list(live.items()) + list(retired.items()):
        if not slots and name in live:
            print("EMPTY   %-58s (regex matched nothing — file changed shape?)" % name)
            bad = True
    ref_live = max(live.values(), key=len, default=frozenset())
    ref_retired = max(retired.values(), key=len, default=frozenset())
    print("live slots (reference): %s" % sorted(ref_live))
    for name, slots in live.items():
        mark = "ok  " if slots == ref_live else "DIFF"
        bad |= slots != ref_live
        print("  %s %-58s %s" % (mark, name, sorted(slots)))
    print("retired slots (reference): %s" % sorted(ref_retired))
    for name, slots in retired.items():
        mark = "ok  " if slots == ref_retired else "DIFF"
        bad |= slots != ref_retired
        print("  %s %-58s %s" % (mark, name, sorted(slots)))
    both = ref_live & ref_retired
    if both:
        print("slot(s) both live and retired: %s" % sorted(both))
        bad = True
    if bad:
        sys.exit("check-slot-enumerations: the dev-worker slot lists disagree (see DIFF/EMPTY above)")
    print("check-slot-enumerations: OK (%d live enumerations, %d retired enumerations agree)" % (len(live), len(retired)))


if __name__ == "__main__":
    main()
