#!/usr/bin/env python3
"""Unit tests for kubernetes/apps/infrastructure/agentforge-broker/brokerseat-crd.yaml (plan A1).

The CRD is the FIRST gate of the BrokerSeat design (plans/2026-09-06-brokerseat-controller-plan.md
D1): its CEL rules are what make a controller seat's name a pure function of its spec, its spec
immutable, and its status a subresource only the provisioner may write. Nothing in this repo
evaluates CRD CEL (kubeconform runs -ignore-missing-schemas and skips the kind), so these tests pin
the rules' PRESENCE and PLACEMENT — a rule that is deleted, moved out of the `spec` block, or edited
into a different invariant goes red here — plus the two cross-file facts that keep the git seats
honest: the reserved-stem list equals the set of hand-named git seats, and the three new files are
invisible to scripts/gen-broker-inventory.py's broker-*.yaml glob (so `--check` still counts only
the git seats).

stdlib-only, read from the real tree (see _brokerseat_support.py). Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
from __future__ import annotations

import fnmatch
import ipaddress
import re
import subprocess
import sys
import unittest

import _brokerseat_support as sup

gbi = sup.gbi


class CrdIdentity(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = sup.crd_doc()

    def test_group_kind_names_scope_and_version(self) -> None:
        spec = gbi._top_block(self.doc, "spec")
        self.assertEqual(sup.name(self.doc), "brokerseats.agentforge.io")
        self.assertEqual(gbi._field(spec, "group", 2), "agentforge.io")
        self.assertEqual(gbi._field(spec, "scope", 2), "Namespaced")
        names = gbi._sub_block(spec, "names", 2)
        self.assertEqual(gbi._field(names, "kind", 4), "BrokerSeat")
        self.assertEqual(gbi._field(names, "plural", 4), "brokerseats")
        self.assertEqual(gbi._field(names, "singular", 4), "brokerseat")
        self.assertEqual(sup.flow_list(gbi._field(names, "shortNames", 4) or "[]"), ["bseat"])
        versions = gbi._sub_block(spec, "versions", 2)
        self.assertRegex(versions, r"(?m)^    - name: v1alpha1\s*$")
        self.assertRegex(versions, r"(?m)^      served: true\s*$")
        self.assertRegex(versions, r"(?m)^      storage: true\s*$")
        self.assertEqual(versions.count("- name: v"), 1, "exactly one served+storage version")


class CrdCelRules(unittest.TestCase):
    """The rules by content AND placement (root vs spec vs account), because a rule that moves is a
    rule that changes meaning: `self == oldSelf` at the root would freeze status too."""

    NAME_RULE = "self.metadata.name == 'broker-' + self.spec.provider + '-' + self.spec.account"
    SIZE_RULE = "size(self.metadata.name) <= 52"
    IMMUTABLE_RULE = "self == oldSelf"

    def setUp(self) -> None:
        self.rules = sup.crd_rules()
        self.root_indent = min(indent for indent, _ in self.rules)

    def _at_root(self) -> list[str]:
        return [r for indent, r in self.rules if indent == self.root_indent]

    def test_crd_has_name_and_immutability_cel_rules(self) -> None:
        root = self._at_root()
        self.assertIn(self.NAME_RULE, root, "the mechanical-name rule must be a ROOT rule")
        self.assertIn(self.SIZE_RULE, root, "the _STEM_MAX rule must be a ROOT rule")
        # `self == oldSelf` must sit INSIDE properties.spec (spec immutable), not at the root (which
        # would freeze status/metadata) and not on a single field.
        lines = sup.crd_doc().splitlines()
        spec_line = next(i for i, l in enumerate(lines) if re.match(r"^            spec:\s*$", l))
        status_line = next(i for i, l in enumerate(lines) if re.match(r"^            status:\s*$", l))
        hits = [
            i for i, l in enumerate(lines)
            if re.match(rf'^\s*-\s*rule:\s*"{re.escape(self.IMMUTABLE_RULE)}"\s*$', l)
        ]
        self.assertEqual(len(hits), 1, "exactly one `self == oldSelf` transition rule")
        self.assertTrue(spec_line < hits[0] < status_line, "the transition rule must be inside properties.spec")
        indent = len(lines[hits[0]]) - len(lines[hits[0]].lstrip(" "))
        provider_line = next(i for i, l in enumerate(lines) if re.match(r"^                provider:\s*$", l))
        self.assertTrue(hits[0] < provider_line, "the transition rule guards the whole spec, not one field")
        self.assertEqual(indent, 16, "spec-level x-kubernetes-validations entry indent")

    def test_size_rule_matches_the_account_max_length(self) -> None:
        """52 = len('broker-') + the longest provider + '-' + account maxLength: the size rule and
        the schema bound must agree, or one of them is dead."""
        doc = sup.crd_doc()
        m = re.search(r"(?m)^\s*maxLength:\s*(\d+)\s*$", gbi._sub_block(doc, "account", 16))
        self.assertIsNotNone(m, "spec.account.maxLength")
        providers = sup.flow_list(re.search(r"(?m)^\s*enum:\s*(\[.*?\])\s*$",
                                            gbi._sub_block(doc, "provider", 16)).group(1))
        self.assertEqual(sorted(providers), ["anthropic", "openai"])
        self.assertEqual(len("broker-") + max(map(len, providers)) + 1 + int(m.group(1)), 52)
        self.assertEqual(len("broker-") + max(map(len, providers)) + 1 + int(m.group(1)) + len("-headless"), 61)

    def test_reserved_git_stems_cover_exactly_the_hand_named_git_seats(self) -> None:
        """A hand-named git seat (broker-anthropic-max1 = anthropic/claude-max-1) ALSO satisfies the
        mechanical rule for another account slug; the root reserved list must name every such seat
        and nothing else (a retired seat must leave the list, or its name is refused forever)."""
        reserved = None
        for rule in self._at_root():
            m = re.fullmatch(r"!\(self\.metadata\.name in \[(.*)\]\)", rule)
            if m:
                reserved = {p.strip().strip("'") for p in m.group(1).split(",")}
        self.assertIsNotNone(reserved, "a root rule reserving the hand-named git stems")
        seats = gbi.load_seats()
        hand_named = {s.deployment for s in seats if s.deployment != f"broker-{s.provider}-{s.account}"}
        self.assertTrue(hand_named, "the tree currently has hand-named git seats")
        self.assertEqual(reserved, hand_named)
        mechanical = {s.deployment for s in seats} - hand_named
        self.assertTrue(reserved.isdisjoint(mechanical))

    def test_reserved_account_slugs(self) -> None:
        slug_rules = [r for indent, r in self.rules if indent > 16]
        self.assertEqual(len(slug_rules), 1, "one field-level rule (the reserved account slugs)")
        for word in ("tenants", "operator", "shared", "default", "headless"):
            self.assertIn(f"'{word}'", slug_rules[0])
        self.assertTrue(slug_rules[0].startswith("!(self in ["))


class CrdSchema(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = sup.crd_doc()

    def test_crd_status_subresource_and_printer_columns(self) -> None:
        versions = gbi._sub_block(gbi._top_block(self.doc, "spec"), "versions", 2)
        self.assertRegex(versions, r"(?m)^      subresources:\s*\n        status: \{\}\s*$")
        cols = re.findall(
            r"(?m)^\s*-\s*\{\s*name:\s*(\w+),\s*type:\s*(\w+),\s*jsonPath:\s*(\S+?)(?:,\s*priority:\s*(\d+))?\s*\}\s*$",
            gbi._sub_block(versions, "additionalPrinterColumns", 6),
        )
        self.assertEqual(
            cols,
            [
                ("Aud", "string", ".status.aud", ""),
                ("Phase", "string", ".status.phase", ""),
                ("ClusterIP", "string", ".spec.clusterIP", ""),
                ("Ready", "integer", ".status.readyReplicas", ""),
                ("Reason", "string", ".status.reason", "1"),
                ("Age", "date", ".metadata.creationTimestamp", ""),
            ],
        )
        status = gbi._sub_block(self.doc, "status", 12)
        self.assertRegex(status, r"(?m)^\s*enum:\s*\[Pending, Seeding, Rendering, Ready, Degraded, Terminating\]\s*$")
        self.assertRegex(status, r"(?m)^\s*x-kubernetes-list-type:\s*map\s*$")
        self.assertRegex(status, r"(?m)^\s*x-kubernetes-list-map-keys:\s*\[type\]\s*$")
        self.assertRegex(status, r"CredentialPresent, CasRequired, Seeded, Rendered, Available, Ready, Entitled, Collision")

    def test_spec_requires_exactly_the_cp_triple(self) -> None:
        spec = gbi._sub_block(self.doc, "spec", 12)
        self.assertRegex(spec, r"(?m)^              required:\s*\[provider, account, clusterIP\]\s*$")
        props = gbi._sub_block(spec, "properties", 14)
        self.assertEqual(re.findall(r"(?m)^                (\w+):\s*$", props), ["provider", "account", "clusterIP"],
                         "spec carries provider/account/clusterIP and NOTHING else (no image/kidBarrier/entitleLike)")

    def test_account_pattern_is_the_inventory_slug(self) -> None:
        m = re.search(r'(?m)^\s*pattern:\s*"(.*)"\s*$', gbi._sub_block(self.doc, "account", 16))
        self.assertEqual(sup.yaml_double_quoted(m.group(1)), gbi._SLUG.pattern)

    def test_cluster_ip_pattern_is_exactly_the_service_cidr(self) -> None:
        m = re.search(r'(?m)^\s*pattern:\s*"(.*)"\s*$', gbi._sub_block(self.doc, "clusterIP", 16))
        pattern = re.compile(sup.yaml_double_quoted(m.group(1)))
        cidr = ipaddress.ip_network(gbi.SERVICE_CIDR)
        for ip in ("10.96.0.0", "10.96.0.192", "10.96.0.255", "10.100.20.3", "10.111.255.255",
                   "10.95.255.255", "10.112.0.0", "10.0.0.1", "192.168.0.40", "10.96.0.256", "10.96.00.1"):
            try:
                inside = ipaddress.ip_address(ip) in cidr
            except ValueError:
                inside = False
            self.assertEqual(bool(pattern.fullmatch(ip)), inside, ip)
        self.assertIsNone(pattern.fullmatch("10.96.0.1 "))
        self.assertIsNone(pattern.fullmatch("None"))


class SeatFilesOutsideTheBrokerGlob(unittest.TestCase):
    """The generator derives the seat inventory from `broker-*.yaml` (load_seats); a file caught by
    that glob that is not a seat makes parse_seat raise. The three A1 files are named so the
    question never comes up — pinned here so a rename cannot quietly break the inventory gate."""

    def test_seat_files_do_not_match_the_broker_glob(self) -> None:
        for fname in sup.NEW_FILES:
            self.assertTrue((sup.BROKER_DIR / fname).is_file(), fname)
            self.assertFalse(fnmatch.fnmatch(fname, "broker-*.yaml"), fname)
        globbed = {p.name for p in sup.BROKER_DIR.glob("broker-*.yaml")}
        self.assertTrue(globbed.isdisjoint(sup.NEW_FILES))

    def test_load_seats_never_derives_a_seat_from_them(self) -> None:
        sources = {s.source for s in gbi.load_seats()}
        for fname in sup.NEW_FILES:
            self.assertFalse(any(src.endswith(fname) for src in sources), fname)
        expected = sorted(p.name for p in sup.BROKER_DIR.glob("broker-*.yaml") if p != gbi.INVENTORY)
        self.assertEqual(sorted(src.rsplit("/", 1)[-1] for src in sources), expected)

    def test_gen_broker_inventory_check_stays_green(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(sup._MOD_PATH), "--check"],
            cwd=str(sup.REPO), capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        n = len(gbi.load_seats())
        self.assertIn(f"OK — {n} seats", proc.stdout)

    def test_kustomization_lists_the_three_files(self) -> None:
        text = sup.KUSTOMIZATION.read_text(encoding="utf-8")
        listed = re.findall(r"(?m)^  - (\S+)", text)
        for fname in sup.NEW_FILES:
            self.assertIn(fname, listed, fname)


if __name__ == "__main__":
    unittest.main()
