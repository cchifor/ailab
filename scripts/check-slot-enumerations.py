#!/usr/bin/env python3
r"""Fail when the dev-worker SLOT enumerations disagree across the repo.

A dev-worker "slot" (dev-worker-N / dwN) is named in many places — OpenBao AppRoles, the two
credential syncs, the per-slot ServiceAccounts in three trees, tofu, the ansible inventory — and a
slot retirement (plans/2026-09-21-retire-dev-workers-3-6-plan.md) or addition has to touch every
one of them. This is the gate codex asked for in the ADR 0028 plan review: it extracts the LIVE and
RETIRED slot sets from each file with a narrow, file-specific regex and prints every source next to
its set, so a missed edit names the file rather than showing up as a red sync a day later.

FAIL-CLOSED, and that is the whole point: every source below is MANDATORY and so are the counts
(three RoleBindings, two env blocks in pg-sync.yaml — the CronJob and its bootstrap Job). A regex
that stops matching because a file was reformatted, a RoleBinding that was deleted, or an env entry
that was dropped is an ERROR here, never a source that quietly leaves the comparison.

Run from the repo root (CI: .gitea/workflows/manifests.yaml). Exit 1 on any disagreement, on a
source that yields nothing, or on a slot that is both live and retired.
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

# One RoleBinding of the observer ClusterRole per platform namespace (platform-access/rbac.yaml),
# and one env block per workload in pg-sync.yaml (the CronJob and the bootstrap Job).
EXPECTED_ROLEBINDING_NAMESPACES = ("strive-ailab", "strive-sandboxes-ailab", "platform-edge")
EXPECTED_PGSYNC_ENVS = 2


def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.exit("%s: missing" % path.relative_to(ROOT))


def ints(strings):
    return frozenset(int(s) for s in strings)


def one(pattern, text, source, flags=re.M):
    """The single match a source must have. A source that stops matching is a failure, not a skip."""
    m = re.search(pattern, text, flags)
    if m is None:
        sys.exit("%s: no match for %r — the file changed shape, so this enumeration would have "
                 "silently dropped out of the comparison" % (source, pattern))
    return m


def env_values(text, var, source):
    """The value of an env entry that must appear once per workload, all agreeing."""
    values = re.findall(r'\{ name: ' + var + r', value: "([\d ]*)" \}', text)
    if len(values) != EXPECTED_PGSYNC_ENVS:
        sys.exit("%s: found %d %s env entries, expected %d (the CronJob and the bootstrap Job)"
                 % (source, len(values), var, EXPECTED_PGSYNC_ENVS))
    if len(set(values)) != 1:
        sys.exit("%s: the CronJob and the bootstrap Job disagree on %s" % (source, var))
    return ints(values[0].split())


def live_sets():
    """name -> set of live slot numbers, one entry per enumeration (a file can contribute several)."""
    out = {}

    text = read(K8STOKEN)
    m = one(r"^\s*for _n in \(([\d,\s]+)\):", text, "k8stoken-sync.yaml")
    out["k8stoken-sync.yaml: for _n in (...)"] = ints(re.findall(r"\d+", m.group(1)))

    text = read(PROVISION)
    m = one(r"^\s*for host in ((?:dev-worker-\d+\s*)+); do", text, "devworker-provision-job.yaml")
    out["devworker-provision-job.yaml: for host in ..."] = ints(re.findall(r"dev-worker-(\d+)", m.group(1)))

    text = read(PA_RBAC)
    out["platform-access/rbac.yaml: ServiceAccounts"] = ints(
        re.findall(r"^metadata: \{ name: platform-dw(\d+), namespace: platform-access \}", text, re.M))
    m = one(r"resourceNames: \[([^\]]*platform-dw[^\]]*)\]", text, "platform-access/rbac.yaml mint Role")
    out["platform-access/rbac.yaml: mint resourceNames"] = ints(re.findall(r"platform-dw(\d+)", m.group(1)))
    bindings = dict(
        (ns, ints(re.findall(r"name: platform-dw(\d+)", subjects)))
        for ns, subjects in re.findall(
            r"kind: RoleBinding\nmetadata:\n  name: dev-worker-platform-observer\n  namespace: (\S+)\n"
            r"subjects:\n((?:  - \{[^\n]*\}\n)+)", text))
    for ns in EXPECTED_ROLEBINDING_NAMESPACES:
        if ns not in bindings:
            sys.exit("platform-access/rbac.yaml: no dev-worker-platform-observer RoleBinding for "
                     "namespace %s (one per platform namespace is the contract)" % ns)
        out["platform-access/rbac.yaml: RoleBinding subjects in %s" % ns] = bindings[ns]

    out["platform-access/pg-sync.yaml: LIVE_SLOTS"] = env_values(
        read(PA_PGSYNC), "LIVE_SLOTS", "platform-access/pg-sync.yaml")

    out["testpool/tep-access.yaml: ServiceAccounts"] = ints(
        re.findall(r"^metadata: \{ name: tep-dw(\d+), namespace: testpool \}", read(TEP), re.M))
    out["helmtest/namespaces.yaml: Namespaces"] = ints(
        re.findall(r"^kind: Namespace\nmetadata:\n  name: helmtest-dw(\d+)$", read(HELMTEST_NS), re.M))
    out["infra/dev-workers/variables.tf: map keys"] = ints(
        re.findall(r'^\s{4}"dev-worker-(\d+)" = \{', read(TOFU), re.M))
    out["inventory/hosts.yml: dev_workers hosts"] = ints(
        re.findall(r"^\s{8}dev-worker-(\d+):\s*$", read(INVENTORY), re.M))
    return out


def retired_sets():
    out = {}
    m = one(r'^\s*RETIRED_SLOTS="([^"]*)"', read(PROVISION), "devworker-provision-job.yaml RETIRED_SLOTS")
    out["devworker-provision-job.yaml: RETIRED_SLOTS"] = ints(re.findall(r"dev-worker-(\d+)", m.group(1)))
    out["platform-access/pg-sync.yaml: RETIRED_SLOTS"] = env_values(
        read(PA_PGSYNC), "RETIRED_SLOTS", "platform-access/pg-sync.yaml")
    return out


def main():
    live, retired = live_sets(), retired_sets()
    bad = False

    for name, slots in live.items():
        if not slots:
            print("EMPTY   %-58s (matched nothing — file changed shape?)" % name)
            bad = True

    ref_live = max(live.values(), key=len, default=frozenset())
    ref_retired = max(retired.values(), key=len, default=frozenset())

    print("live slots (reference): %s" % sorted(ref_live))
    for name, slots in live.items():
        bad |= slots != ref_live
        print("  %s %-58s %s" % ("ok  " if slots == ref_live else "DIFF", name, sorted(slots)))
    print("retired slots (reference): %s" % sorted(ref_retired))
    for name, slots in retired.items():
        bad |= slots != ref_retired
        print("  %s %-58s %s" % ("ok  " if slots == ref_retired else "DIFF", name, sorted(slots)))

    both = ref_live & ref_retired
    if both:
        print("slot(s) both live and retired: %s" % sorted(both))
        bad = True
    if bad:
        sys.exit("check-slot-enumerations: the dev-worker slot lists disagree (see DIFF/EMPTY above)")
    print("check-slot-enumerations: OK (%d live enumerations, %d retired enumerations agree)"
          % (len(live), len(retired)))


if __name__ == "__main__":
    main()
