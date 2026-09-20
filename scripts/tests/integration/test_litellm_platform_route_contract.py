"""Exercise the configured PAID document route through the pinned LiteLLM HTTP adapter.

Run with the manifest's LiteLLM image and networking disabled:
    python scripts/tests/integration/test_litellm_platform_route_contract.py
Only the upstream HTTP transport is mocked; routing, parameter transformation,
SDK serialization, error translation and retry decisions use the real packages.

Since 2026-09-20 (ADR 0027) the caller-facing name `gpt-5.6-sol` is served from
the ChatGPT subscription (test_litellm_chatgpt_chat_handler_contract.py covers
that path) and the OpenAI route lives on as `gpt-5.6-sol-api`, retained so a
replacement key can be proven before the name is pointed back. The wire contract
here -- api.openai.com/v1/chat/completions, the caller's exact body preserved,
exactly one attempt per outcome -- moves with the alias, unchanged.
"""

import os
import socket

# These must be set before importing LiteLLM: use its bundled metadata and never
# contact telemetry or fetch a model catalog during this offline contract test.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_DISABLE_TELEMETRY"] = "true"
os.environ["DO_NOT_TRACK"] = "1"
NETWORK_ATTEMPTS = []


def _deny_connect(_socket, address):
    NETWORK_ATTEMPTS.append(str(address))
    raise AssertionError("Unexpected network access in offline route contract")


socket.socket.connect = _deny_connect
socket.socket.connect_ex = _deny_connect

import asyncio
import copy
import hashlib
import importlib.metadata
import json
import pathlib
import sys
import unittest

import httpx
import litellm
import yaml
from litellm import Router
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER


ROOT = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "kubernetes/apps/apps/ai/litellm.yaml"
MANIFEST_BYTES = MANIFEST.read_bytes()
DOCUMENTS = list(yaml.safe_load_all(MANIFEST_BYTES))
CONFIG = yaml.safe_load(next(
    document for document in DOCUMENTS
    if isinstance(document, dict) and document.get("kind") == "ConfigMap"
    and document["metadata"]["name"] == "litellm-config"
)["data"]["config.yaml"])
ROUTE_NAME = "gpt-5.6-sol-api"
ROUTE = next(entry for entry in CONFIG["model_list"]
             if entry["model_name"] == ROUTE_NAME)
UPSTREAM_MODEL = ROUTE["litellm_params"]["model"].split("/", 1)[1]  # openai/<id> -> <id>
REQUEST = {
    "model": ROUTE_NAME,
    "messages": [{"role": "user", "content": "Offline adapter fixture."}],
    "max_completion_tokens": 32000,
    "reasoning_effort": "medium",
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "document_schema",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        },
    },
}


class DocumentRouteContract(unittest.TestCase):
    async def _exercise(self, outcome):
        sent = []

        async def respond(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
            self.assertEqual(request.method, "POST")
            sent.append(json.loads(request.content))
            if outcome == "timeout":
                raise httpx.ReadTimeout("Controlled offline unknown outcome", request=request)
            if outcome != "success":
                return httpx.Response(int(outcome), json={"error": {
                    "message": "Controlled offline rejection",
                    "type": "invalid_request_error" if outcome == "400" else "server_error",
                    "code": "offline_fixture",
                }}, headers={"retry-after": "0"}, request=request)
            return httpx.Response(200, json={
                "id": "chatcmpl-offline", "object": "chat.completion", "created": 1,
                "model": "gpt-5.6-sol",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"ok":true}',
                }}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            }, request=request)

        # The SDK client cache is process-wide. Distinct inert credentials keep
        # cases isolated in its SDK client cache; retries within each case
        # retain the same client and are counted at its HTTP transport boundary.
        os.environ["OPENAI_API_KEY"] = "sk-offline-fixture-" + outcome
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
            litellm.aclient_session = client
            litellm.drop_params = CONFIG["litellm_settings"]["drop_params"]
            litellm.telemetry = False
            router = Router(model_list=[copy.deepcopy(ROUTE)],
                            **copy.deepcopy(CONFIG["router_settings"]))
            response = None
            error = None
            try:
                response = await asyncio.wait_for(
                    router.acompletion(**copy.deepcopy(REQUEST)), timeout=25,
                )
            except Exception as exc:
                error = type(exc).__name__
            finally:
                # Drain callbacks before this case resets its Router callbacks.
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                router.reset()
        self.assertEqual(NETWORK_ATTEMPTS, [], "No socket connection may be attempted")
        self.assertEqual(len(sent), 1, f"{outcome}: expected exactly one upstream attempt; error={error}")
        # The alias resolves to the deployment's upstream id; everything else is byte-for-byte the caller's.
        self.assertEqual(sent[0], {**REQUEST, "model": UPSTREAM_MODEL},
                         "Do not drop or override the evaluated caller parameters")
        print(json.dumps({"outcome": outcome, "upstream_attempts": len(sent),
                          "error": error, "exact_request_preserved": True}))
        return response, error

    def test_configured_route_transport_contract(self):
        async def run_cases():
            expected_errors = {
                "success": None, "400": "BadRequestError", "429": "RateLimitError",
                "500": "InternalServerError", "timeout": "Timeout",
            }
            try:
                for outcome, expected_error in expected_errors.items():
                    with self.subTest(outcome=outcome):
                        response, error = await self._exercise(outcome)
                        self.assertEqual(error, expected_error)
                        if outcome == "success":
                            self.assertEqual(response.model, "gpt-5.6-sol")  # the upstream id, not the alias
                            self.assertEqual(response.choices[0].message.content, '{"ok":true}')
                            self.assertEqual(response.usage.prompt_tokens, 12)
                            self.assertEqual(response.usage.completion_tokens, 4)
                            self.assertEqual(response.usage.total_tokens, 16)
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                await GLOBAL_LOGGING_WORKER.stop()
        # LiteLLM's logging worker is process-wide; all subcases share its loop.
        asyncio.run(run_cases())


if __name__ == "__main__":
    package = pathlib.Path(litellm.__file__).parent
    sources = ("router.py", "llms/openai/openai.py", "llms/openai/chat/gpt_5_transformation.py")
    print(json.dumps({
        "python": sys.version,
        "packages": {name: importlib.metadata.version(name)
                     for name in ("litellm", "openai", "httpx", "pydantic")},
        "manifest_sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
        "adapter_source_sha256": {name: hashlib.sha256((package / name).read_bytes()).hexdigest()
                                  for name in sources},
    }))
    unittest.main(verbosity=2)
