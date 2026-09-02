#!/usr/bin/env python3
"""Unit tests for the control-plane Deployment's AFP_BROKER_CLUSTERIP_POOL env var (C-P0-09).

Reads the live value straight out of
kubernetes/apps/apps/agentforge/deployment.yaml (the WS4 env block) rather than
duplicating the literal here, so a future edit that breaks the invariants this
guards is caught even if nobody remembers to update this test's expectations.

stdlib-only (ipaddress + re) — no PyYAML. This repo's "Script unit tests" CI
step (.gitea/workflows/broker-inventory.yaml) installs no dependencies and
scripts/tests/test_manifest_paths.py documents that PyYAML is NOT on that
runner (its regex fallback is "what runs in CI"); scripts/manifest-paths.py
guards its own `import yaml` behind try/except ImportError for the same
reason. A flow-mapping env entry like the one this test reads is a single
regular line, so a small targeted regex over the raw text is enough — no
general YAML parser (real or fallback) is needed here. No docker, no network,
no cluster access. Run:

    python3 -m unittest discover -s scripts/tests -v
"""
from __future__ import annotations

import ipaddress
import pathlib
import re
import unittest

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
    """The `value:` of one `env:` entry on the agentforge Deployment.

    Matches the two env-entry shapes this file actually uses:
    flow-mapping (`- { name: X, value: "Y" }`, one line) and block-style
    (`- name: X` then an indented `value: "Y"` on the next line, as rendered
    for a `>-` folded scalar's first line too). Deliberately NOT a general
    YAML parser — this repo's CI runner has no PyYAML (see module docstring)
    — so a moved/renamed anchor or a `valueFrom:`-only entry (no literal
    `value:`) fails this assertion rather than silently matching nothing.
    """
    text = _DEPLOYMENT_PATH.read_text()
    escaped = re.escape(name)
    flow = re.search(
        r"-\s*\{\s*name:\s*" + escaped + r'\s*,\s*value:\s*"([^"]*)"\s*\}',
        text,
    )
    if flow:
        return flow.group(1)
    block = re.search(
        r"-\s*name:\s*" + escaped + r'\s*\n\s*value:\s*"?([^"\n]*)"?\s*(?:\n|$)',
        text,
    )
    if block:
        return block.group(1)
    raise AssertionError(
        f"no env var {name!r} found in {_DEPLOYMENT_PATH}"
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


class EnvValueParserFallsClosed(unittest.TestCase):
    """The regex parser itself: proves it fails loudly, not silently, on a
    missing var — the same shape of guarantee test_manifest_paths.py asks of
    its own stdlib fallback (never silently drop what it can't parse)."""

    def test_missing_var_raises_assertion_error(self) -> None:
        with self.assertRaises(AssertionError):
            _env_value("AFP_DOES_NOT_EXIST")


if __name__ == "__main__":
    unittest.main()
