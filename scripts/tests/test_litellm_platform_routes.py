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
        config_map = next(doc for doc in documents if isinstance(doc, dict)
                          and doc.get("kind") == "ConfigMap"
                          and doc["metadata"]["name"] == "litellm-config")
        config = yaml.safe_load(config_map["data"]["config.yaml"])
        routes = [entry for entry in config["model_list"]
                  if entry["model_name"] == "gpt-5.6-sol"]
        self.assertEqual(len(routes), 1, "Document-table requests must resolve to one model route")
        params = routes[0]["litellm_params"]
        self.assertEqual(params["model"], "openai/gpt-5.6-sol")
        self.assertEqual(params["api_key"], "os.environ/OPENAI_API_KEY")
        self.assertEqual(params.get("num_retries"), 0,
                         "An admitted document-table request must not be replayed by the gateway")
        self.assertEqual(set(params), {"model", "api_key", "num_retries"},
                         "Preserve the caller's evaluated protocol and generation parameters")
        router = config.get("router_settings", {})
        self.assertFalse(router.get("default_fallbacks"),
                         "Document-table extraction must not inherit a model fallback")
        for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
            for rule in router.get(key, []):
                self.assertIsInstance(rule, dict)
                self.assertFalse({"gpt-5.6-sol", "*"}.intersection(rule),
                                 f"Document-table extraction must not use {key}")


if __name__ == "__main__":
    unittest.main()
