#!/usr/bin/env python3
"""The shape of `searxng-allow` (kubernetes/apps/apps/dsh/searxng.yaml) is pinned here.

WHY THIS EXISTS. ADR 0023 admits the strive platform's search services to SearXNG
through ONE extra `from` element that carries a namespaceSelector AND a
podSelector. Every failure mode of that element is silent in production:

  * split into two list items, the selectors are OR'ed and the bare
    namespaceSelector admits EVERY pod in strive-ailab (the trap dsh-allow
    documents for `edge`) -- SearXNG keeps answering, nothing goes red;
  * `Exists` instead of `In`, or a widened `values`, admits platform services
    that have no business here;
  * an ipBlock, a second port, or a loosened egress rule turns the one pod in
    this namespace with a route out into something broader than a search box.

kubeconform validates syntax, not intent; a cluster only shows the effect after
Flux has applied it. This test reads the RAW manifest with PyYAML -- no kustomize
build, no cluster -- and asserts the documented shape element by element, so the
diff that loosens the policy is the diff that fails CI.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "kubernetes" / "apps" / "apps" / "dsh" / "searxng.yaml"
POLICY_NAME = "searxng-allow"

# The dsh pod: the consumer SearXNG was deployed for (the header of searxng.yaml).
DSH_PEER = {"podSelector": {"matchLabels": {"app": "dsh"}}}
OAUTH2_PROXY_PEER = {"podSelector": {"matchLabels": {"app": "oauth2-proxy-searxng"}}}

# ADR 0023: the platform's search services, BOTH selectors in ONE element (AND).
STRIVE_NAMESPACE = "strive-ailab"
STRIVE_SERVICE_LABEL = "strive.io/service"
STRIVE_SERVICES = ["workflow-worker", "workflow", "integration"]
STRIVE_PEER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": STRIVE_NAMESPACE}},
    "podSelector": {
        "matchExpressions": [
            {"key": STRIVE_SERVICE_LABEL, "operator": "In", "values": STRIVE_SERVICES},
        ],
    },
}

INGRESS_PORTS = [{"port": 8080, "protocol": "TCP"}]
POLICY_TYPES = ["Ingress", "Egress"]

# Egress is documented in the manifest and unchanged by ADR 0023: DNS to
# kube-system, then 443 ONLY to public addresses with every private range excluded.
EGRESS = [
    {
        "to": [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}},
        ],
        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
    },
    {
        "to": [
            {
                "ipBlock": {
                    "cidr": "0.0.0.0/0",
                    "except": [
                        "10.0.0.0/8",
                        "172.16.0.0/12",
                        "192.168.0.0/16",
                        "169.254.0.0/16",
                        "127.0.0.0/8",
                    ],
                },
            },
        ],
        "ports": [{"port": 443, "protocol": "TCP"}],
    },
]


def _policy():
    """The one NetworkPolicy named searxng-allow, from the raw manifest."""
    found = [
        doc
        for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
        if doc
        and doc.get("kind") == "NetworkPolicy"
        and doc.get("metadata", {}).get("name") == POLICY_NAME
    ]
    if len(found) != 1:
        raise AssertionError(f"expected exactly one NetworkPolicy {POLICY_NAME!r}, found {len(found)}")
    return found[0]


class SearxngAllowShape(unittest.TestCase):
    def setUp(self):
        self.policy = _policy()
        self.spec = self.policy["spec"]

    def test_selects_the_searxng_pod_in_dsh_for_ingress_and_egress(self):
        self.assertEqual(self.policy["metadata"].get("namespace"), "dsh")
        self.assertEqual(self.spec.get("podSelector"), {"matchLabels": {"app": "searxng"}})
        self.assertEqual(
            self.spec.get("policyTypes"),
            POLICY_TYPES,
            "policyTypes must stay [Ingress, Egress]: dropping Egress would lift the internet "
            "restriction on the one pod here with a route out",
        )

    def test_ingress_is_one_rule_with_exactly_two_peers_on_port_8080(self):
        ingress = self.spec.get("ingress")
        self.assertIsInstance(ingress, list)
        self.assertEqual(len(ingress), 1, "one ingress rule: peers are the from-list of that rule")
        rule = ingress[0]
        self.assertEqual(
            rule.get("ports"),
            INGRESS_PORTS,
            "ingress must admit port 8080/TCP only (a rule without `ports` admits every port)",
        )
        self._peers()

    def _peers(self):
        peers = self.spec["ingress"][0].get("from")
        self.assertIsInstance(peers, list, "a rule without `from` admits every source")
        self.assertEqual(
            len(peers), 3, f"exactly three peers: dsh, oauth2-proxy-searxng, strive-ailab; got {peers!r}"
        )
        return peers

    def _strive_peer(self):
        peers = self._peers()
        # The strive element is the LAST peer and the only one carrying a namespaceSelector.
        with_ns = [p for p in peers if "namespaceSelector" in p]
        self.assertEqual(len(with_ns), 1, f"exactly one cross-namespace peer; got {peers!r}")
        self.assertIs(with_ns[0], peers[-1], "the strive-ailab peer must stay last (after dsh and oauth2-proxy)")
        return with_ns[0]

    def test_first_peer_is_the_dsh_pod_by_label(self):
        self.assertEqual(self._peers()[0], DSH_PEER)

    def test_oauth2_proxy_peer_is_the_sso_gate_by_label(self):
        self.assertEqual(self._peers()[1], OAUTH2_PROXY_PEER)

    def test_strive_peer_ands_the_namespace_with_the_service_label(self):
        peer = self._strive_peer()
        # Both keys in ONE element is the whole point of ADR 0023: as two items they would be
        # OR'ed, and a bare namespaceSelector alone admits every pod in strive-ailab.
        self.assertEqual(
            sorted(peer),
            ["namespaceSelector", "podSelector"],
            f"the strive peer must carry namespaceSelector AND podSelector in one element; got {peer!r}",
        )
        self.assertEqual(
            peer["namespaceSelector"],
            STRIVE_PEER["namespaceSelector"],
            "the namespace half must match kubernetes.io/metadata.name (API-server-stamped, "
            "immutable), not a relabel-able key",
        )
        self.assertEqual(
            peer["podSelector"],
            STRIVE_PEER["podSelector"],
            f"the pod half must be exactly {STRIVE_SERVICE_LABEL} In {STRIVE_SERVICES}: `Exists` or a "
            "wider values list admits platform services that have no business here",
        )
        self.assertEqual(peer, STRIVE_PEER)

    def test_no_ingress_peer_is_an_ip_block(self):
        for i, peer in enumerate(self._peers()):
            self.assertNotIn(
                "ipBlock",
                peer,
                f"ingress peer {i} is an ipBlock; SearXNG admits callers by pod identity only",
            )

    def test_egress_is_unchanged_from_the_documented_shape(self):
        self.assertEqual(
            self.spec.get("egress"),
            EGRESS,
            "egress must stay DNS-to-kube-system plus 443/TCP to public addresses with the private "
            "ranges excluded (ADR 0023 changes who may ask SearXNG a question, never where it may go)",
        )


if __name__ == "__main__":
    unittest.main()
