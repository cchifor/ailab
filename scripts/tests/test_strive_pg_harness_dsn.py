#!/usr/bin/env python3
"""Shape test for the ExternalSecret `strive-pg-harness-dsn` in strive-pg-harness/eso.yaml.

The harness service's HARNESS_DATABASE_URL comes from this Secret (key `database-url` ONLY), rendered
from the same OpenBao password the bootstrap Job applies. Nothing here talks to a cluster.
"""
import pathlib
import unittest

import yaml

ESO = pathlib.Path(__file__).resolve().parents[2] / "kubernetes/apps/infrastructure/strive-pg-harness/eso.yaml"
DSN = (
    "postgres://harness:{{ .password | urlquery }}"
    "@strive-pg-rw.strive-ailab.svc.cluster.local:5432/harness"
)


def docs():
    with open(ESO, encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


class DsnExternalSecret(unittest.TestCase):
    def setUp(self):
        found = [d for d in docs() if d["kind"] == "ExternalSecret" and d["metadata"]["name"] == "strive-pg-harness-dsn"]
        self.assertEqual(len(found), 1, "exactly one strive-pg-harness-dsn ExternalSecret")
        self.es = found[0]

    def test_api_namespace_store(self):
        self.assertEqual(self.es["apiVersion"], "external-secrets.io/v1")
        self.assertEqual(self.es["metadata"]["namespace"], "strive-ailab")
        self.assertEqual(self.es["spec"]["secretStoreRef"], {"name": "strive-pg-harness-store", "kind": "SecretStore"})

    def test_data_is_the_password_only(self):
        self.assertEqual(
            self.es["spec"]["data"],
            [{"secretKey": "password", "remoteRef": {"key": "strive/pg-harness", "property": "password"}}],
        )

    def test_template_has_exactly_one_key(self):
        tgt = self.es["spec"]["target"]
        self.assertEqual(tgt["name"], "strive-pg-harness-dsn")
        self.assertEqual(tgt["creationPolicy"], "Owner")
        tpl = tgt["template"]
        self.assertEqual(tpl["engineVersion"], "v2")
        self.assertEqual(tpl["mergePolicy"], "Replace")
        self.assertEqual(tpl["data"], {"database-url": DSN})

    def test_existing_objects_untouched(self):
        kinds = sorted((d["kind"], d["metadata"]["name"]) for d in docs())
        self.assertEqual(
            kinds,
            sorted([
                ("ServiceAccount", "strive-pg-harness-eso"),
                ("Certificate", "strive-pg-harness-openbao-ca"),
                ("SecretStore", "strive-pg-harness-store"),
                ("ExternalSecret", "strive-pg-harness"),
                ("ExternalSecret", "strive-pg-harness-dsn"),
            ]),
        )


if __name__ == "__main__":
    unittest.main()
