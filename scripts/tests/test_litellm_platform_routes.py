"""Keep the deployed document-table consumer's evaluated model reachable.

An otherwise healthy Strive release indexed documents but failed its first
table draft because the gateway did not register the requested model name.
This checks the serving ConfigMap, rather than a separate test-only catalog.
"""

import pathlib
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]


class PlatformModelRoutes(unittest.TestCase):
    def test_document_table_model_routes_to_the_same_openai_model(self):
        path = ROOT / "kubernetes/apps/apps/ai/litellm.yaml"
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        config_map = next(doc for doc in documents if doc.get("kind") == "ConfigMap"
                          and doc["metadata"]["name"] == "litellm-config")
        config = yaml.safe_load(config_map["data"]["config.yaml"])
        routes = [entry for entry in config["model_list"]
                  if entry["model_name"] == "gpt-5.6-sol"]
        self.assertEqual(len(routes), 1, "Document-table requests must resolve to one model route")
        params = routes[0]["litellm_params"]
        self.assertEqual(params["model"], "openai/gpt-5.6-sol")
        self.assertEqual(params["api_key"], "os.environ/OPENAI_API_KEY")
        self.assertEqual(set(params), {"model", "api_key"},
                         "Preserve the caller's evaluated protocol and generation parameters")
        fallbacks = config.get("router_settings", {}).get("fallbacks", [])
        self.assertFalse(any("gpt-5.6-sol" in rule for rule in fallbacks),
                         "Document-table extraction must not silently switch models")


if __name__ == "__main__":
    unittest.main()
