#!/usr/bin/env python3
"""Unit tests for the control-plane Deployment's AFP_BROKER_CLUSTERIP_POOL env var (C-P0-09).

Reads the live value straight out of
kubernetes/apps/apps/agentforge/deployment.yaml (the WS4 env block) rather than
duplicating the literal here, so a future edit that breaks the invariants this
guards is caught even if nobody remembers to update this test's expectations.

stdlib-only (ipaddress + yaml, both already used by scripts/manifest-paths.py in
this repo) — no docker, no network, no cluster access. Run:

    python3 -m unittest discover -s scripts/tests -v
"""
from __future__ import annotations

import ipaddress
import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEPLOYMENT_PATH = (
    _REPO_ROOT / "kubernetes" / "apps" / "apps" / "agentforge" / "deployment.yaml"
)

# The Service ClusterIPs already hand-pinned for the three pre-existing broker
# accounts (broker-anthropic-max1.yaml, broker-anthropic-max2.yaml,
# broker-openai-codex.yaml) — the pool must not be able to allocate any of
# these, or the CP's PR-gated add flow could render a Service the apiserver
# refuses (address already assigned to a different Service).
_PINNED_ADDRESSES = (
    "10.108.137.32",
    "10.109.144.42",
    "10.108.162.59",
)

# This cluster's Service CIDR (CLAUDE.md / docs/decisions — Talos + Cilium
# default; also asserted by broker-anthropic-max1.yaml's own clusterIP being
# inside it).
_SERVICE_CIDR = ipaddress.ip_network("10.96.0.0/12")


def _env_value(name: str) -> str:
    """The `value:` of one `env:` entry on the agentforge Deployment's first container.

    Fails the test (not a collection error) if the Deployment shape, the
    container, or the named var is missing — a moved/renamed anchor should
    show up as a failing assertion, not a silent skip.
    """
    with _DEPLOYMENT_PATH.open() as f:
        manifest = yaml.safe_load(f)
    containers = manifest["spec"]["template"]["spec"]["containers"]
    for container in containers:
        for entry in container.get("env", []):
            if entry.get("name") == name:
                return entry["value"]
    raise AssertionError(
        f"no env var {name!r} found on any container in {_DEPLOYMENT_PATH}"
    )


class BrokerClusterIpPoolEnv(unittest.TestCase):
    def setUp(self) -> None:
        raw = _env_value("AFP_BROKER_CLUSTERIP_POOL")
        # Mirrors agentforge_platform.domain.clusterip.parse_pool's own
        # ipaddress.ip_network(spec, strict=True) call (that module lives in
        # agentforge-platform, not this repo — re-implemented here with the
        # stdlib so this repo's test suite stays dependency-free of a sibling
        # repo's package).
        self.network = ipaddress.ip_network(raw, strict=True)

    def test_value_parses_as_a_strict_ipv4_network(self) -> None:
        self.assertIsInstance(self.network, ipaddress.IPv4Network)

    def test_value_is_private(self) -> None:
        self.assertTrue(self.network.is_private)

    def test_prefixlen_is_at_least_16(self) -> None:
        # parse_pool's _MIN_PREFIXLEN: not wider than a /16, so the allocator
        # can never hand out an address belonging to another Service.
        self.assertGreaterEqual(self.network.prefixlen, 16)

    def test_lies_inside_the_cluster_service_cidr(self) -> None:
        self.assertTrue(self.network.subnet_of(_SERVICE_CIDR))

    def test_disjoint_from_every_hand_pinned_broker_address(self) -> None:
        for address in _PINNED_ADDRESSES:
            with self.subTest(address=address):
                self.assertNotIn(ipaddress.ip_address(address), self.network)


if __name__ == "__main__":
    unittest.main()
