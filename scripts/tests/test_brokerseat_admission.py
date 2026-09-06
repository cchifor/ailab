#!/usr/bin/env python3
"""Unit tests for kubernetes/apps/infrastructure/agentforge-broker/brokerseat-admission.yaml (plan A1, D7).

The CEL LOGIC of the two policies is proven by scripts/check-seat-guard-cel.py (cel-python, run in
.gitea/workflows/tenant-guard-cel.yaml — the CI test runner has no cel-python, so it is not run
here). What THIS module pins is everything around the logic that decides whether it applies at all:

  * each policy fires for EXACTLY its ServiceAccount (one username matchCondition naming the CP or
    the provisioner SA — the SAs as declared elsewhere in the tree), fails CLOSED (failurePolicy
    Fail) and is bound with validationActions [Deny], no param, no scope narrowing;
  * the CP guard covers BrokerSeat create (+ update and the status subresource);
  * the objects guard covers EVERY child kind brokerseat-rbac.yaml lets the provisioner create or
    update — an RBAC widening without a matching guard rule goes red here;
  * the designed invariants are present in the CEL text (image digest regex, Flux label, shared
    store, broker SA, no target.template) and the image regex has the intended semantics;
  * the harness that evaluates the CEL targets this file and is actually wired into CI.

stdlib-only, read from the real tree (see _brokerseat_support.py). Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
from __future__ import annotations

import re
import unittest

import _brokerseat_support as sup

CP_GUARD = "agentforge-cp-brokerseat-guard"
OBJECTS_GUARD = "agentforge-provisioner-seat-objects-guard"
IMAGE_REGEX_LITERAL = "'^registry[.]chifor[.]me/agentforge/orchestrator@sha256:[0-9a-f]{64}$'"


class Guards(unittest.TestCase):
    def setUp(self) -> None:
        self.policies = sup.policies()
        self.bindings = sup.bindings()

    def test_exactly_the_two_designed_policies(self) -> None:
        self.assertEqual(sorted(self.policies), sorted([CP_GUARD, OBJECTS_GUARD]))
        self.assertEqual(sorted(self.bindings), sorted([CP_GUARD, OBJECTS_GUARD]))

    def test_vaps_match_only_their_sa_and_fail_closed(self) -> None:
        expected = {CP_GUARD: sup.CP_USERNAME, OBJECTS_GUARD: sup.PROVISIONER_USERNAME}
        for policy_name, username in expected.items():
            p = self.policies[policy_name]
            self.assertEqual(p["failurePolicy"], "Fail", policy_name)
            self.assertFalse(p["paramKind"], policy_name)
            self.assertEqual(len(p["matchConditions"]), 1, policy_name)
            self.assertEqual(
                p["matchConditions"][0]["expression"],
                f"request.userInfo.username == '{username}'",
                policy_name,
            )
            other = {v for k, v in expected.items() if k != policy_name}
            for cond in p["matchConditions"]:
                for o in other:
                    self.assertNotIn(o, cond["expression"], f"{policy_name} must not admit {o}")
        # the two usernames are the SAs the tree actually declares
        for (n, ns), username in ((sup.CP_SA, sup.CP_USERNAME), (sup.PROVISIONER_SA, sup.PROVISIONER_USERNAME)):
            self.assertEqual(username, f"system:serviceaccount:{ns}:{n}")
        self.assertIn(sup.CP_SA, sup.service_account_docs(
            sup.REPO / "kubernetes/apps/apps/agentforge/serviceaccount-service.yaml"))
        self.assertIn(sup.PROVISIONER_SA, sup.service_account_docs(
            sup.REPO / "kubernetes/apps/infrastructure/security/openbao/unseal-rbac.yaml"))

    def test_bindings_deny_without_param_or_scope_narrowing(self) -> None:
        for policy_name in (CP_GUARD, OBJECTS_GUARD):
            b = self.bindings[policy_name]
            self.assertEqual(b["policyName"], policy_name)
            self.assertEqual(b["validationActions"], ["Deny"], policy_name)
            self.assertFalse(b["paramRef"], policy_name)
            self.assertFalse(b["matchResources"], f"{policy_name}: the SA gate is the matchCondition, not a namespace selector")

    def test_cp_guard_covers_brokerseat_create_update_and_status(self) -> None:
        rules = self.policies[CP_GUARD]["resourceRules"]
        self.assertEqual(len(rules), 1)
        (rule,) = rules
        self.assertEqual(rule["apiGroups"], ["agentforge.io"])
        self.assertEqual(sorted(rule["operations"]), ["CREATE", "UPDATE"])
        self.assertEqual(sorted(rule["resources"]), ["brokerseats", "brokerseats/status"])

    def test_objects_guard_covers_every_child_kind_the_provisioner_may_write(self) -> None:
        provisioner = next(
            sup.role_rules(d) for d in sup.docs(sup.RBAC_FILE)
            if sup.kind(d) == "Role" and sup.name(d) == "agentforge-provisioner-brokerseats"
        )
        writable = {key for key, verbs in provisioner.items()
                    if key[0] != "agentforge.io" and verbs & {"create", "update", "patch"}}
        self.assertEqual(len(writable), 5, writable)
        rules = self.policies[OBJECTS_GUARD]["resourceRules"]
        for group, resource in sorted(writable):
            covering = [r for r in rules if sup.rule_covers(r, group, resource)]
            self.assertTrue(covering, f"{group}/{resource} is writable by the provisioner but unguarded")
            for r in covering:
                self.assertEqual(sorted(r["operations"]), ["CREATE", "UPDATE"], (group, resource))
                self.assertEqual(r["apiVersions"], ["*"], (group, resource))
        # and nothing beyond those five (a guard on a kind RBAC does not grant is dead weight that
        # hides a gap the other way round)
        guarded = {(g, r) for rule in rules for g in rule["apiGroups"] for r in rule["resources"]}
        self.assertEqual(guarded, writable)

    def test_cp_guard_pins_the_five_bare_fields_and_the_name(self) -> None:
        v = self.policies[CP_GUARD]["validations"]
        self.assertEqual(v[:6], [
            "!has(object.metadata.finalizers)",
            "!has(object.metadata.ownerReferences)",
            "!has(object.metadata.labels)",
            "!has(object.metadata.annotations)",
            "object.metadata.name == 'broker-' + object.spec.provider + '-' + object.spec.account",
            "!has(object.status)",
        ])
        self.assertEqual(len(v), 6)

    def test_objects_guard_pins_the_designed_invariants(self) -> None:
        p = self.policies[OBJECTS_GUARD]
        text = "\n".join(p["validations"])
        self.assertEqual(len(p["validations"]), 12)
        # the stem is READ from the ownership label (a variable) and every clause derives from it
        self.assertEqual(
            p["variables"]["stem"],
            "'agentforge.io/broker-seat' in variables.labels ? variables.labels['agentforge.io/broker-seat'] : ''",
        )
        self.assertEqual(
            p["variables"]["seatSecrets"],
            "[variables.stem + '-oauth', variables.stem + '-kids', variables.stem + '-ledger']",
        )
        for needle in (
            "request.namespace == 'agentforge-broker'",
            "variables.stem.matches('^broker-(anthropic|openai)-[a-z0-9]+(-[a-z0-9]+)*$')",
            "size(variables.stem) <= 52",
            "!('kustomize.toolkit.fluxcd.io/name' in variables.labels)",
            "!('kustomize.toolkit.fluxcd.io/name' in variables.oldLabels)",
            "variables.oldLabels['agentforge.io/broker-seat'] == variables.stem",
            "variables.owner.kind == 'BrokerSeat'",
            "variables.owner.controller == true",
            "variables.owner.blockOwnerDeletion == true",
            "oldObject.metadata.ownerReferences[0].uid == variables.owner.uid",
            f"variables.ctrs[0].image.matches({IMAGE_REGEX_LITERAL})",
            "variables.podSpec.serviceAccountName == 'agentforge-broker'",
            "variables.podSpec.automountServiceAccountToken == false",
            "size(variables.ctrs) == 1",
            "!has(variables.podSpec.initContainers) && !has(variables.podSpec.ephemeralContainers)",
            "v.secret.secretName in variables.seatSecrets",
            "e.valueFrom.secretKeyRef.name in variables.seatSecrets",
            "e.secretRef.name in variables.seatSecrets",
            "object.spec.secretStoreRef.name == 'agentforge-broker-store'",
            "object.spec.secretStoreRef.kind == 'SecretStore'",
            "object.spec.target.name == object.metadata.name",
            "!has(object.spec.target.template)",
            "!has(d.find) && !has(d.sourceRef) && !has(d.generatorRef) && !has(d.rewrite)",
            "'broker-' + k.split('/')[2] + '-' + k.split('/')[3] == variables.stem",
            "object.spec.endpointSelector == {'matchLabels': {'app.kubernetes.io/name': variables.stem}}",
            "!has(object.specs)",
            "!has(object.spec.ingressDeny)",
            "f.matchName in variables.fqdnAllow",
            "object.spec.selector == {'app.kubernetes.io/name': variables.stem}",
        ):
            self.assertIn(needle, text, needle)
        self.assertEqual(
            sorted(p["variables"]),
            sorted(["kind", "labels", "stem", "oldLabels", "seatSecrets", "owner", "podSpec", "ctrs",
                    "vols", "podLabels", "esKeys", "fqdnAllow"]),
        )

    def test_image_regex_semantics(self) -> None:
        regex = re.compile(IMAGE_REGEX_LITERAL.strip("'"))
        digest = "a1" * 32
        self.assertIsNotNone(regex.fullmatch(f"registry.chifor.me/agentforge/orchestrator@sha256:{digest}"))
        for bad in (
            "registry.chifor.me/agentforge/orchestrator:latest",
            f"registry.chifor.me/agentforge/p1-worker@sha256:{digest}",
            f"registryXchifor.me/agentforge/orchestrator@sha256:{digest}",
            f"docker.io/registry.chifor.me/agentforge/orchestrator@sha256:{digest}",
            f"registry.chifor.me/agentforge/orchestrator@sha256:{digest[:-1]}",
            f"registry.chifor.me/agentforge/orchestrator@sha256:{digest}x",
            f"registry.chifor.me/agentforge/orchestrator@sha256:{digest.upper()}",
        ):
            self.assertIsNone(regex.fullmatch(bad), bad)
        # and the literal in the policy is the SAME text (a regex edited in one place only goes red)
        text = "\n".join(self.policies[OBJECTS_GUARD]["validations"])
        self.assertEqual(text.count(IMAGE_REGEX_LITERAL), 1)


class Harness(unittest.TestCase):
    """The CEL is only evaluated by scripts/check-seat-guard-cel.py; a harness that points at the
    wrong file, or that CI never runs, guards nothing (this estate has watched exactly that rot)."""

    def test_cel_harness_targets_this_policy(self) -> None:
        src = sup.HARNESS.read_text(encoding="utf-8")
        self.assertIn('POLICY = BROKER_DIR / "brokerseat-admission.yaml"', src)
        self.assertIn('BROKER_DIR = REPO_ROOT / "kubernetes/apps/infrastructure/agentforge-broker"', src)
        for name in (CP_GUARD, OBJECTS_GUARD):
            self.assertIn(f'"{name}"', src)

    def test_cel_harness_runs_in_the_tenant_guard_workflow(self) -> None:
        raw = sup.WORKFLOW.read_text(encoding="utf-8")
        # comment lines dropped: the header TALKS about `|| true` / `continue-on-error` as the things
        # it forbids, and a commented-out invocation must not count as one.
        wf = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("#")) + "\n"
        invocation = '"$PY" scripts/check-seat-guard-cel.py'
        # The step is `if uv ... else pip ... fi`: the harness must run in BOTH arms (an arm that
        # only runs the tenant table is a runner-dependent silent skip of the seat table).
        step = wf[wf.index("if command -v uv"):]
        uv_arm, _, rest = step.partition("\n          else\n")
        pip_arm, _, _ = rest.partition("\n          fi\n")
        self.assertIn(invocation, uv_arm, "uv arm must run the seat harness")
        self.assertIn(invocation, pip_arm, "pip arm must run the seat harness")
        self.assertNotIn("|| true", step)
        self.assertNotIn("continue-on-error", wf)
        self.assertNotIn("\n        if:", wf)
        # triggered on BOTH pull_request and push for the policy dir (its baseline is a git seat) and
        # the harness itself — each path once per trigger list.
        for path in (
            "kubernetes/apps/infrastructure/agentforge-broker/**",
            "scripts/check-seat-guard-cel.py",
        ):
            self.assertEqual(wf.count(f'- "{path}"'), 2, f"workflow must trigger on {path} for pull_request AND push")
        # still runs the tenant-guard harness byte-for-byte as before, in both arms
        self.assertIn('"$PY" scripts/check-tenant-guard-cel.py', uv_arm)
        self.assertIn('"$PY" scripts/check-tenant-guard-cel.py', pip_arm)


if __name__ == "__main__":
    unittest.main()
