#!/usr/bin/env python3
r"""Fail when the dev-worker SLOT enumerations disagree across the repo.

A dev-worker "slot" (dev-worker-N / dwN) is named in many places — OpenBao AppRoles, the two
credential syncs, the per-slot ServiceAccounts in two trees, tofu, the ansible inventory, the
per-slot Gitea logins — and a slot retirement (plans/2026-09-21-retire-dev-workers-3-6-plan.md) or
addition has to touch every one of them. This is the gate codex asked for in the ADR 0028 plan
review: it extracts the LIVE and RETIRED slot sets from each file with a narrow, file-specific regex
and prints every source next to its set, so a missed edit names the file rather than showing up as a
red sync a day later.

FAIL-CLOSED, and that is the whole point: every source below is MANDATORY and so are the counts
(three RoleBindings, two env blocks in pg-sync.yaml — the CronJob and its bootstrap Job). A regex
that stops matching because a file was reformatted, a RoleBinding that was deleted, or an env entry
that was dropped is an ERROR here, never a source that quietly leaves the comparison.

It also checks the PROJECT-SCOPED grants (ADR 0028, amendment 2026-09-29): a purpose-named Role
bound to one or two slots (e.g. clusters/ai/trueswarm-observer.yaml) is not an enumeration of every
slot, so it cannot join the equality comparison — but it must never name a slot that is not live.
Every uncommented `platform-dw<N>` token under kubernetes/apps/ must be a live slot, so retiring a
slot fails CI until its per-project bindings are removed too. (Grants authored in OTHER repos, e.g.
cchifor/trueswarm-admin deploy/*/observer.yaml, are outside this scan: the runbook's retire
checklist names them.)

Run from the repo root (CI: .gitea/workflows/manifests.yaml). Exit 1 on any disagreement, on a
source that yields nothing, on a slot that is both live and retired, or on a platform-dw<N>
reference to a slot that is not live.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

K8STOKEN = ROOT / "kubernetes/apps/infrastructure/security/openbao/k8stoken-sync.yaml"
PROVISION = ROOT / "kubernetes/apps/infrastructure/security/openbao/devworker-provision-job.yaml"
PA_RBAC = ROOT / "kubernetes/apps/infrastructure/platform-access/rbac.yaml"
PA_PGSYNC = ROOT / "kubernetes/apps/infrastructure/platform-access/pg-sync.yaml"
E2E_SYNC = ROOT / "kubernetes/apps/trueswarm-e2e-tokens/token-sync.yaml"
HELMTEST_NS = ROOT / "kubernetes/apps/infrastructure/helmtest/namespaces.yaml"
TOFU = ROOT / "kubernetes/infra/dev-workers/variables.tf"
INVENTORY = ROOT / "inventory/hosts.yml"
GROUP_VARS = ROOT / "ansible/group_vars/dev_workers.yml"
APPS = ROOT / "kubernetes/apps"

# One RoleBinding of the observer ClusterRole per platform namespace (platform-access/rbac.yaml),
# and one env block per workload in pg-sync.yaml (the CronJob and the bootstrap Job).
EXPECTED_ROLEBINDING_NAMESPACES = ("strive-ailab", "strive-sandboxes-ailab", "workbricks-edge")
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


def strict_yaml(text, source):
    """The document as YAML decodes it, refusing a repeated mapping key. Text matching cannot see every
    spelling of a key (an explicit `? key`, a quoted key with escapes), and YAML keeps the LAST one, so
    a repeat would hand Ansible a value the text check never read. Ansible's own tags (`!vault`,
    `!unsafe`) load as their plain scalar, so a vaulted value elsewhere in the file cannot break the gate.

    PyYAML is not installed by CI: it is the runner image's system package (python3-yaml on the Ubuntu
    24.04 cloud image), the same dependency .gitea/workflows/manifests.yaml's LiteLLM drift step and
    broker-inventory.yaml's "Script unit tests" step already rely on. Without it this check fails, never
    skips."""
    try:
        import yaml
    except ImportError:
        sys.exit("%s: PyYAML is needed to read it as YAML; failing closed" % source)

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=True)
            try:
                repeated = key in seen
            except TypeError:
                raise yaml.constructor.ConstructorError(None, None, "unhashable key",
                                                        key_node.start_mark)
            if repeated:
                raise yaml.constructor.ConstructorError(
                    None, None, "duplicate key %r (YAML keeps the LAST one silently)" % (key,),
                    key_node.start_mark)
            seen.add(key)
        return loader.construct_mapping(node, deep=deep)

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    for tag in ("!vault", "!unsafe"):
        UniqueKeyLoader.add_constructor(tag, lambda loader, node: loader.construct_scalar(node))
    try:
        return yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as e:
        sys.exit("%s: not loadable as YAML: %s" % (source, e))


def gitea_user_slots(text, source):
    """The slots of `dev_worker_gitea_users` (one `dev-worker-N-bot` login per live slot, ailab plan
    2026-10-11-worker-identities). Read to the END of its block, like the reviewbot allowlist test
    does: a regex that stops at the first line it cannot match would let a stray login sit unseen
    below a comment. Anything inside the block that is not a slot login — another account, a flow
    list, a duplicate — is an error, never a skip.

    The login must be spelled exactly as Gitea knows it, `dev-worker-<n>-bot`: no zero padding
    (`dev-worker-04-bot` is a different account), no quoting, nothing trailing. A YAML comment needs
    whitespace before its `#`, so `- dev-worker-4-bot  # note` is slot 4 while `- dev-worker-4-bot#x`
    is the literal string `dev-worker-4-bot#x` and fails. A second definition of the key, in any
    spelling, fails too: YAML keeps the last one, so Ansible would read a list this check never saw.

    TWO readers, and they must agree: this text parser, and the file decoded as YAML by a loader that
    refuses any repeated key (strict_yaml). Whatever YAML could make of the file that the text does not
    show - an explicit or escaped duplicate key, an anchor, a tag - fails the comparison."""
    decoded = strict_yaml(text, source)
    if len(re.findall(r"""^(["']?)dev_worker_gitea_users\1[ \t]*:""", text, re.M)) > 1:
        sys.exit("%s: dev_worker_gitea_users is defined twice (YAML keeps the LAST one silently)"
                 % source)
    key = one(r"^dev_worker_gitea_users[ \t]*:(.*)$", text, source)
    if re.split(r"(?:^|\s)#", key.group(1), maxsplit=1)[0].strip():
        sys.exit("%s: dev_worker_gitea_users must be a block list (one `- dev-worker-N-bot` per line), "
                 "got %r" % (source, key.group(0)))
    slots, indents = [], set()
    for line in text[key.end():].splitlines()[1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue                      # blank / comment INSIDE the block: skip, don't stop
        if not line[:1].isspace():
            break                         # dedent to column 0 = the next top-level key
        m = re.match(r"^([ \t]+)- dev-worker-([1-9][0-9]*)-bot(?:[ \t]+#.*)?[ \t]*$", line)
        if m is None:
            sys.exit("%s: unparsable dev_worker_gitea_users entry %r (expected `- dev-worker-N-bot`)"
                     % (source, line))
        indents.add(m.group(1))
        slots.append(int(m.group(2)))
    if len(indents) > 1:
        sys.exit("%s: dev_worker_gitea_users entries are indented inconsistently" % source)
    if len(slots) != len(set(slots)):
        sys.exit("%s: dev_worker_gitea_users names a slot twice: %s" % (source, sorted(slots)))
    as_yaml = decoded.get("dev_worker_gitea_users") if isinstance(decoded, dict) else None
    if not isinstance(as_yaml, list) or not all(
            isinstance(u, str) and re.fullmatch(r"dev-worker-[1-9][0-9]*-bot", u) for u in as_yaml):
        sys.exit("%s: dev_worker_gitea_users decodes to %r, not a list of canonical dev-worker-N-bot "
                 "logins" % (source, as_yaml))
    if as_yaml != ["dev-worker-%d-bot" % n for n in slots]:
        sys.exit("%s: dev_worker_gitea_users decodes to %r, but its text lists %r"
                 % (source, as_yaml, ["dev-worker-%d-bot" % n for n in slots]))
    return frozenset(slots)


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
    # ADR 0035: the Trueswarm e2e token sync (CronJob + bootstrap Job, same two-copy shape as pg-sync).
    out["trueswarm-e2e-tokens/token-sync.yaml: LIVE_SLOTS"] = env_values(
        read(E2E_SYNC), "LIVE_SLOTS", "trueswarm-e2e-tokens/token-sync.yaml")

    out["helmtest/namespaces.yaml: Namespaces"] = ints(
        re.findall(r"^kind: Namespace\nmetadata:\n  name: helmtest-dw(\d+)$", read(HELMTEST_NS), re.M))
    out["infra/dev-workers/variables.tf: map keys"] = ints(
        re.findall(r'^\s{4}"dev-worker-(\d+)" = \{', read(TOFU), re.M))
    out["inventory/hosts.yml: dev_workers hosts"] = ints(
        re.findall(r"^\s{8}dev-worker-(\d+):\s*$", read(INVENTORY), re.M))
    out["group_vars/dev_workers.yml: dev_worker_gitea_users"] = gitea_user_slots(
        read(GROUP_VARS), "group_vars/dev_workers.yml")
    return out


def retired_sets():
    out = {}
    m = one(r'^\s*RETIRED_SLOTS="([^"]*)"', read(PROVISION), "devworker-provision-job.yaml RETIRED_SLOTS")
    out["devworker-provision-job.yaml: RETIRED_SLOTS"] = ints(re.findall(r"dev-worker-(\d+)", m.group(1)))
    out["platform-access/pg-sync.yaml: RETIRED_SLOTS"] = env_values(
        read(PA_PGSYNC), "RETIRED_SLOTS", "platform-access/pg-sync.yaml")
    return out


def platform_sa_references():
    """(file:line, slot) for every uncommented `platform-dw<N>` token under kubernetes/apps/.

    Deliberately NOT tied to one YAML spelling: a subject can be `name: platform-dw7`,
    `name: "platform-dw7"`, `name:   platform-dw7`, a flow mapping, a `resourceNames` entry or a
    `kind: User` subject `system:serviceaccount:platform-access:platform-dw7` — every one of them
    grants, so every one is counted. Only comment text is excluded (a `#` at line start or after
    whitespace, as YAML defines a comment), so prose about a retired slot is not a false positive."""
    refs = []
    for path in sorted(list(APPS.rglob("*.yaml")) + list(APPS.rglob("*.yml"))):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = re.split(r"(?:^|\s)#", line, maxsplit=1)[0]
            for n in re.findall(r"\bplatform-dw(\d+)\b", code):
                refs.append(("%s:%d" % (path.relative_to(ROOT).as_posix(), no), int(n)))
    return refs


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

    refs = platform_sa_references()
    if not refs:
        print("EMPTY   no `platform-dw<N>` under kubernetes/apps/ (platform-access/rbac.yaml "
              "changed shape?)")
        bad = True
    stale = [(where, n) for where, n in refs if n not in ref_live]
    print("platform-dw<N> references under kubernetes/apps/: %d, slots %s"
          % (len(refs), sorted(set(n for _, n in refs))))
    for where, n in stale:
        print("  STALE %s names platform-dw%d, which is not a live slot" % (where, n))
        bad = True

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
