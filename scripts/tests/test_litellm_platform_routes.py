"""Keep the deployed document-table consumer's evaluated model reachable.

An otherwise healthy Strive release indexed documents but failed its first
table draft because the gateway did not register the requested model name.
This checks the serving ConfigMap, rather than a separate test-only catalog.

Since 2026-09-20 (ADR 0027) `gpt-5.6-sol` is served from the ChatGPT
subscription through the estate's `chatgpt-chat/` custom provider while the
OpenAI key is dead; the original paid route lives on as `gpt-5.6-sol-api`.
Both shapes are admissible for `gpt-5.6-sol`, so pointing it back at the API
key when a replacement lands is a config change, not a test rewrite.
"""

import hashlib
import pathlib
import re
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
AI_DIR = ROOT / "kubernetes/apps/apps/ai"

# The two shapes gpt-5.6-sol may take. Either way: one attempt (num_retries 0),
# the caller's evaluated protocol and generation parameters preserved.
SUBSCRIPTION_SHAPE = {
    "model": "chatgpt-chat/gpt-5.6-sol",
    "num_retries": 0,
    "allowed_openai_params": ["reasoning_effort"],
}
API_KEY_SHAPE = {
    "model": "openai/gpt-5.6-sol",
    "api_key": "os.environ/OPENAI_API_KEY",
    "num_retries": 0,
}


def _documents():
    return list(yaml.safe_load_all((AI_DIR / "litellm.yaml").read_text(encoding="utf-8")))


def _config(documents):
    config_map = next(doc for doc in documents if isinstance(doc, dict)
                      and doc.get("kind") == "ConfigMap"
                      and doc["metadata"]["name"] == "litellm-config")
    return yaml.safe_load(config_map["data"]["config.yaml"])


def _routes(config, name):
    return [entry for entry in config["model_list"] if entry["model_name"] == name]


class PlatformModelRoutes(unittest.TestCase):
    def setUp(self):
        self.documents = _documents()
        self.config = _config(self.documents)

    def test_document_table_model_has_one_route_in_an_admissible_shape(self):
        routes = _routes(self.config, "gpt-5.6-sol")
        self.assertEqual(len(routes), 1, "Document-table requests must resolve to one model route")
        params = routes[0]["litellm_params"]
        self.assertIn(params, (SUBSCRIPTION_SHAPE, API_KEY_SHAPE),
                      "Preserve the caller's evaluated protocol and generation parameters; "
                      "one attempt, never replayed by the gateway")

    def test_paid_path_is_retained_under_its_own_name(self):
        routes = _routes(self.config, "gpt-5.6-sol-api")
        self.assertEqual(len(routes), 1, "The retained OpenAI route must exist exactly once")
        self.assertEqual(routes[0]["litellm_params"], API_KEY_SHAPE,
                         "gpt-5.6-sol-api keeps the OpenAI contract permanently (ADR 0027)")

    def test_subscription_provider_is_registered_and_its_module_compiles(self):
        if _routes(self.config, "gpt-5.6-sol")[0]["litellm_params"] != SUBSCRIPTION_SHAPE:
            self.skipTest("gpt-5.6-sol is back on the API key; the handler may be gone")
        provider_map = self.config["litellm_settings"].get("custom_provider_map") or []
        self.assertIn({"provider": "chatgpt-chat", "custom_handler": "chatgpt_chat.handler"}, provider_map)
        source = (AI_DIR / "chatgpt_chat.py").read_text(encoding="utf-8")
        compile(source, "chatgpt_chat.py", "exec")  # a syntax error here would fail every pod start
        self.assertIn("handler = ChatGPTChat()", source)

    def test_handler_is_merged_into_litellm_config_without_a_name_hash(self):
        kustomization = yaml.safe_load((AI_DIR / "kustomization.yaml").read_text(encoding="utf-8"))
        generators = [g for g in kustomization.get("configMapGenerator", []) if g.get("name") == "litellm-config"]
        self.assertEqual(len(generators), 1, "chatgpt_chat.py must be merged into litellm-config exactly once")
        generator = generators[0]
        self.assertEqual(generator.get("behavior"), "merge")
        self.assertEqual(generator.get("namespace"), "ai")
        self.assertIs(generator.get("options", {}).get("disableNameSuffixHash"), True,
                      "the ConfigMap must keep its name so a restarting pod reads config and handler together")
        self.assertEqual(generator.get("files"), ["chatgpt_chat.py"])

    def test_handler_edits_roll_the_gateway(self):
        text = (AI_DIR / "litellm.yaml").read_text(encoding="utf-8")
        match = re.search(r'checksum/chatgpt-chat:\s*"([0-9a-f]{12})"', text)
        self.assertIsNotNone(match, "checksum/chatgpt-chat pod-template annotation is the roll trigger")
        actual = hashlib.sha256((AI_DIR / "chatgpt_chat.py").read_bytes()).hexdigest()[:12]
        self.assertEqual(match.group(1), actual,
                         "stamp checksum/chatgpt-chat with sha256sum kubernetes/apps/apps/ai/chatgpt_chat.py | cut -c1-12")

    def test_document_table_extraction_is_never_replayed_by_a_fallback(self):
        router = self.config.get("router_settings", {})
        self.assertFalse(router.get("default_fallbacks"),
                         "Document-table extraction must not inherit a model fallback")
        guarded = {"gpt-5.6-sol", "gpt-5.6-sol-api", "*"}
        for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
            for rule in router.get(key, []):
                self.assertIsInstance(rule, dict)
                self.assertFalse(guarded.intersection(rule),
                                 f"Document-table extraction must not use {key}")


if __name__ == "__main__":
    unittest.main()
