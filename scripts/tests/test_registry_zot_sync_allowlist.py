#!/usr/bin/env python3
"""The docker.io on-demand sync in the Zot registry must never match a namespace we publish.

Subject: ansible/roles/registry_zot/templates/config.json.j2 + defaults/main.yml.

WHY (ailab#420, recurred 2026-09-26). The mirror.gcr.io / registry-1.docker.io sync entry had
`"content": [{"prefix": "**"}]`, which also matches `strive/**` and every other namespace CI
pushes. Every TAG lookup of our own images then starts an upstream on-demand sync for something
that cannot exist upstream (9,036 `failed to sync image` in one day, almost all `strive/*`), and a
sync that never settles wedges every later request for that reference ("image already demanded,
waiting on channel"): tag-manifest reads hang while `/v2/` stays green (cchifor/platform#788). The
allowlist that fixed it on 2026-08-26 was applied by hand and silently lost when the role re-rendered
the config on 2026-09-25. These tests pin it in the IaC. Zot content filters have no negation, so an
explicit allowlist is the only way to express "never proxy our own namespaces".

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import json
import unittest

from test_registry_zot_retention import _load_defaults, _render_config

#: Namespaces CI/agents PUSH to registry.chifor.me (checked against the live store 2026-09-26).
#: None may ever be eligible for docker.io on-demand sync.
LOCAL_NAMESPACES = {
    "strive", "agentforge", "testpool", "trueswarm", "trueswarm-admin", "muse-stream", "test",
}

#: Docker Hub namespaces the platform/ailab stacks pull through the mirror today (the live store
#: plus every docker.io reference in both repos). Dropping one makes that image 404 on the mirror
#: and fall back to anonymous Docker Hub (the 429 ADR 0014 exists to avoid).
MUST_MIRROR = [
    "library/**", "pgvector/**", "rustfs/**", "valkey/**", "grafana/**", "prom/**", "qdrant/**",
    "curlimages/**", "dpage/**", "minio/**", "nginxinc/**", "testcontainers/**", "moby/**",
    "docker/**", "openbao/**", "gitea/**", "verdaccio/**", "koalaman/**", "headlamp-k8s/**",
    "alpine/**", "cloudflare/**",
    # root-level repos pulled by explicit short path (registry.chifor.me/debian, …/postgres)
    "debian", "postgres",
]


def _dockerhub_entry(parsed: dict) -> dict:
    for reg in parsed["extensions"]["sync"]["registries"]:
        if "https://mirror.gcr.io" in reg["urls"]:
            return reg
    raise AssertionError("no mirror.gcr.io sync registry in the rendered config")


class RegistryZotSyncAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.parsed = json.loads(_render_config())
        self.prefixes = [c["prefix"] for c in _dockerhub_entry(self.parsed)["content"]]

    def test_no_catch_all(self):
        for p in self.prefixes:
            self.assertFalse(p.startswith("*"), f"wildcard-leading prefix {p!r} matches everything")

    def test_no_local_namespace_is_synced(self):
        for p in self.prefixes:
            self.assertNotIn(
                p.split("/", 1)[0], LOCAL_NAMESPACES, f"{p!r} would sync a namespace we publish"
            )

    def test_every_pulled_through_namespace_is_kept(self):
        missing = [p for p in MUST_MIRROR if p not in self.prefixes]
        self.assertEqual(missing, [], "these would 404 on the mirror and fall back to Docker Hub")

    def test_local_namespaces_are_declared_in_defaults(self):
        # The role asserts against this list at apply time; keep the two in step.
        self.assertEqual(set(_load_defaults()["registry_zot_local_namespaces"]), LOCAL_NAMESPACES)

    def test_other_upstreams_unchanged(self):
        regs = {r["urls"][0]: [c["prefix"] for c in r["content"]]
                for r in self.parsed["extensions"]["sync"]["registries"]}
        self.assertEqual(regs["https://quay.io"], ["keycloak/**"])
        self.assertEqual(regs["https://mcr.microsoft.com"], ["playwright", "playwright/**"])


if __name__ == "__main__":
    unittest.main()
