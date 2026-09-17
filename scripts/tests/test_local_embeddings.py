"""Serving contract for the pinned local embedding space."""

import json
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
AI = ROOT / "kubernetes/apps/apps/ai"


class LocalEmbeddingContract(unittest.TestCase):
    def setUp(self):
        self.documents = list(yaml.safe_load_all((AI / "text-embeddings.yaml").read_text()))
        self.deployment = next(d for d in self.documents if d["kind"] == "Deployment")
        self.pod = self.deployment["spec"]["template"]["spec"]
        self.server = self.pod["containers"][0]
        self.args = dict(zip(self.server["args"][::2], self.server["args"][1::2]))

    def test_pinned_nonroot_bounded_inference(self):
        self.assertRegex(self.server["image"], r"@sha256:[a-f0-9]{64}$")
        self.assertEqual(self.server["image"], self.pod["initContainers"][0]["image"])
        self.assertTrue(self.pod["securityContext"]["runAsNonRoot"])
        self.assertFalse(self.pod["automountServiceAccountToken"])
        self.assertEqual(self.args["--served-model-name"], "bge-m3-local-v1")
        self.assertEqual(self.args["--auto-truncate"], "false")
        limit = json.loads((AI / "embedding-input-limit.json").read_text())["max_seq_length"]
        self.assertEqual(int(self.args["--max-batch-tokens"]), limit)
        self.assertEqual(limit, 1024)
        self.assertTrue(self.server["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(self.server["readinessProbe"]["httpGet"]["path"], "/health")

    def test_every_artifact_has_a_digest_and_downloads_are_pinned(self):
        files = {}
        for line in (AI / "embedding-assets.sha256").read_text().splitlines():
            digest, path = line.split()
            self.assertRegex(digest, r"^[a-f0-9]{64}$")
            self.assertNotIn(path, files)
            self.assertNotIn("..", pathlib.PurePosixPath(path).parts)
            files[path] = digest
        self.assertIn("onnx/model.onnx_data", files)
        self.assertIn("tokenizer.json", files)
        script = (AI / "prepare-embedding-model.sh").read_text()
        revision = re.search(r"^revision=([a-f0-9]{40})$", script, re.M).group(1)
        self.assertIn(revision, self.args["--model-id"])

    def test_gateway_never_falls_back_to_a_different_encoder(self):
        docs = list(yaml.safe_load_all((AI / "litellm.yaml").read_text()))
        config = yaml.safe_load(next(d for d in docs if d.get("kind") == "ConfigMap"
                                    and d["metadata"]["name"] == "litellm-config")["data"]["config.yaml"])
        routes = [r for r in config["model_list"] if r["model_name"] == "bge-m3-local-v1"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["model_info"]["mode"], "embedding")
        self.assertEqual(routes[0]["litellm_params"]["model"], "openai/bge-m3-local-v1")
        self.assertEqual(routes[0]["litellm_params"]["num_retries"], 0)
        router = config.get("router_settings", {})
        self.assertFalse(router.get("default_fallbacks"))
        for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
            for rule in router.get(key, []):
                self.assertFalse({"bge-m3-local-v1", "*"}.intersection(rule))

    def test_service_is_internal_and_accepts_only_gateway_traffic(self):
        service = next(d for d in self.documents if d["kind"] == "Service")
        self.assertEqual(service["spec"].get("type", "ClusterIP"), "ClusterIP")
        policy = next(d for d in self.documents if d["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["ingress"][0]["from"], [
            {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}}}
        ])


if __name__ == "__main__":
    unittest.main()
