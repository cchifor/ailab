#!/usr/bin/env python3
"""The owner-accepted OpenBao witness store keeps its conditions.

    python3 -m unittest scripts.tests.test_relay_budget_witness_store

relay#277 (comments 120994, 121330) accepted af/relay-budget/witness in OpenBao on conditions that
docs/runbooks/relay-worker-budget-authority.md ("External witness") records. This pins the parts a
source tree can prove:
  * no automation writes the witness: every OpenBao policy in the repo that reaches the path, wildcards
    included, grants exactly `read` on the KV data endpoint, and only the two consumer policies do;
    no script, Job or manifest outside the tests writes it; no ESO PushSecret can;
  * the delivery the recovery steps rely on: hourly ESO refresh, a Velero-excluded rendered Secret,
    read-only subPath mounts;
  * the runbook section and the openbao-recovery.md restore step and inventory row stay present.
"""

from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROVISION = ROOT / "kubernetes/apps/infrastructure/security/openbao/relay-budget-provision-job.yaml"
CREDENTIALS = ROOT / "kubernetes/apps/relay-budget-authority/credentials"
COMPONENTS = {
    "relay": ROOT / "kubernetes/components/relay-worker-budget-client/deployment.yaml",
    "llm-router": ROOT / "kubernetes/components/llm-router-worker-budget-reader/deployment.yaml",
}
RUNBOOK = ROOT / "docs/runbooks/relay-worker-budget-authority.md"
RECOVERY = ROOT / "docs/runbooks/openbao-recovery.md"
CONSUMER_POLICIES = {"af-app-relay-budget", "af-app-router-budget"}
# Every KV-v2 API path through which the witness document could be read or changed.
WITNESS_API = [f"af/{api}/relay-budget/witness"
               for api in ("data", "metadata", "delete", "undelete", "destroy")]
# Production trees: what runs or renders. Tests may write drifted policies on purpose.
PRODUCTION = [ROOT / "kubernetes", ROOT / "ansible", ROOT / "scripts", ROOT / ".gitea"]
POLICY_RULE = re.compile(r'path\s+"([^"]+)"\s*\{([^}]*)\}', re.S)
TEXT_SUFFIXES = {".yaml", ".yml", ".sh", ".py", ".hcl", ".j2", ".mjs", ".js", ".json", ".tf", ".md"}


def production_files():
    for base in PRODUCTION:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if (ROOT / "scripts/tests") in path.parents:
                continue
            yield path


def glob_matches(glob, target):
    """OpenBao/Vault policy path semantics: `+` is one segment, a trailing `*` a prefix.
    A template expansion in a rendered policy (shell ${host} or $TENANT, Jinja {{ item }}) can
    hold anything, slashes included, so it matches anything: an over-match fails loudly, an
    under-match would pass silently."""
    pattern = ""
    rest = glob[:-1] if glob.endswith("*") else glob
    for token in re.split(r"(\+|\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{.*?\}\})", rest):
        if token == "+":
            pattern += "[^/]+"
        elif token.startswith("$") or token.startswith("{{"):
            pattern += ".*"
        else:
            pattern += re.escape(token)
    pattern += ".*" if glob.endswith("*") else ""
    return re.fullmatch(pattern, target) is not None


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


class NoAutomationWritesTheWitness(unittest.TestCase):
    def test_glob_semantics(self):
        self.assertTrue(glob_matches("af/data/relay-budget/witness", WITNESS_API[0]))
        self.assertTrue(glob_matches("af/data/*", WITNESS_API[0]))
        self.assertTrue(glob_matches("af/+/relay-budget/+", WITNESS_API[1]))
        self.assertTrue(glob_matches("af/data/${host}/*", WITNESS_API[0]))
        self.assertTrue(glob_matches("af/data/{{ item }}/*", WITNESS_API[0]))
        self.assertTrue(glob_matches("af/{{ api }}/relay-budget/*", WITNESS_API[1]))
        self.assertTrue(glob_matches("af/data/{{ prefix }}", WITNESS_API[0]))  # may span segments
        self.assertFalse(glob_matches("af/data/relay-budget/ca", WITNESS_API[0]))
        self.assertFalse(glob_matches("af/data/dev-workers/*", WITNESS_API[0]))

    def test_only_the_consumer_policies_reach_it_and_only_read_data(self):
        reaching = []
        for path in production_files():
            for glob, body in POLICY_RULE.findall(path.read_text(encoding="utf-8", errors="replace")):
                hits = [api for api in WITNESS_API if glob_matches(glob, api)]
                if hits:
                    capabilities = re.findall(r'"([a-z]+)"', body)
                    reaching.append((path, glob, hits, capabilities))
        self.assertTrue(reaching, "the consumer policies must grant read on the witness")
        for path, glob, hits, capabilities in reaching:
            self.assertEqual(path, PROVISION, f"{path} reaches the witness through {glob}")
            self.assertEqual(hits, [WITNESS_API[0]], f"{glob} reaches a non-data witness endpoint")
            self.assertEqual(capabilities, ["read"], f"{glob} grants {capabilities}")
        # Both consumer policies carry exactly that one grant on the witness.
        script = docs(PROVISION)[0]["data"]["provision.sh"]
        heredocs = dict(re.findall(r"bao policy write (\S+) - <<'EOF'\n(.*?)\nEOF", script, re.S))
        self.assertEqual(set(heredocs), CONSUMER_POLICIES)
        for name, policy in heredocs.items():
            self.assertIn('path "af/data/relay-budget/witness" { capabilities = ["read"] }', policy, name)

    def test_nothing_outside_the_tests_writes_it(self):
        writer = re.compile(
            r"\b(kv\s+(put|patch|rollback|delete|destroy|undelete|metadata)|bao\s+write|vault\s+(write|kv))\b"
            r"[^\n]*relay-budget")
        mentions = set()
        for path in production_files():
            if path.suffix == ".md":
                continue  # runbooks describe the operator's own hand-written commands
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(writer.search(text), f"{path} writes a relay-budget document")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            if "relay-budget/witness" in code:
                mentions.add(path)
        # The two ESO readers and the read-only policies; nothing else names the path outside comments.
        self.assertEqual(mentions, {PROVISION, CREDENTIALS / "relay.yaml", CREDENTIALS / "llm-router.yaml"})

    def test_no_push_secret_can_use_the_budget_stores(self):
        stores = {"relay-budget-store", "router-budget-store"}
        for path in (ROOT / "kubernetes").rglob("*.yaml"):
            text = path.read_text(encoding="utf-8", errors="replace")
            # Only files that could matter are parsed (some repo YAML is a template); one that
            # matters and does not parse fails the test rather than being skipped.
            if "kind: PushSecret" not in text and not any(p in text for p in CONSUMER_POLICIES):
                continue
            for d in docs(path):
                if not isinstance(d, dict):
                    continue
                if d.get("kind") == "PushSecret":
                    refs = {r.get("name") for r in d.get("spec", {}).get("secretStoreRefs", [])}
                    self.assertFalse(refs & stores, f"{path}: PushSecret into a budget store")
                if d.get("kind") in ("SecretStore", "ClusterSecretStore"):
                    role = (d.get("spec", {}).get("provider", {}).get("vault", {})
                            .get("auth", {}).get("kubernetes", {}).get("role"))
                    if role in CONSUMER_POLICIES:
                        self.assertEqual(d["kind"], "SecretStore", path)
                        self.assertIn(d["metadata"]["name"], stores, path)


class DeliveryTheRecoveryStepsRelyOn(unittest.TestCase):
    def test_hourly_refresh_backup_exclusion_and_exact_source(self):
        for namespace in ("relay", "llm-router"):
            [es] = [d for d in docs(CREDENTIALS / f"{namespace}.yaml") if d["kind"] == "ExternalSecret"]
            spec = es["spec"]
            # The runbook's "Changing the witness" forces a sync because of this interval.
            self.assertEqual(spec["refreshInterval"], "1h")
            self.assertNotIn("dataFrom", spec)
            refs = {d["secretKey"]: d["remoteRef"] for d in spec["data"]}
            self.assertEqual(refs["witness"], {"key": "relay-budget/witness", "property": "witness"})
            template = spec["target"]["template"]
            self.assertEqual(template["data"]["witness.json"], "{{ .witness }}")
            # The rendered witness Secret stays out of every Velero schedule.
            self.assertEqual(template["metadata"]["labels"]["velero.io/exclude-from-backup"], "true")

    def test_consumers_mount_the_witness_read_only_as_subpath(self):
        for namespace, path in COMPONENTS.items():
            [c] = yaml.safe_load(path.read_text())["spec"]["template"]["spec"]["containers"]
            [mount] = [m for m in c["volumeMounts"] if m.get("subPath") == "witness.json"]
            self.assertIs(mount["readOnly"], True, namespace)
            self.assertTrue(mount["mountPath"].startswith("/run/"), namespace)


class TheProceduresStayDocumented(unittest.TestCase):
    def test_runbook_and_recovery_anchors(self):
        runbook = RUNBOOK.read_text()
        for anchor in ("## External witness: the owner-accepted OpenBao exception",
                       "### Offline issuance record", "### Who writes it", "### Changing the witness",
                       "### After an OpenBao restore", "### Drills before production `ready=true`",
                       "`af/relay-budget/witness`", "at least 20 seconds", "`refreshInterval: 1h`",
                       # A negative drill counts only with the gate proven open at the observation.
                       "**Drill protocol.**", "**Prove the gate is open, immediately before observing.**",
                       "**Drill injections are never issuance entries.**"):
            self.assertIn(anchor, runbook)
        recovery = RECOVERY.read_text()
        self.assertIn("### After any restore: the Relay budget witness", recovery)
        # Listed in the inventory of KV a wipe loses, re-entered only from the offline record.
        [row] = [line for line in recovery.splitlines() if line.startswith("| **OPERATOR-OWNED (ceremony)**")]
        self.assertIn("`relay-budget/witness.witness`", row)
        self.assertIn("offline issuance record", row)


if __name__ == "__main__":
    unittest.main()
