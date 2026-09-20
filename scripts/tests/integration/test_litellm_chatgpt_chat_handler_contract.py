"""Exercise the gpt-5.6-sol route through the estate's chatgpt-chat custom provider (ADR 0027).

Run with the manifest's LiteLLM image and networking disabled:
    python scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py
Only the upstream HTTP transport is mocked; the handler module is loaded from the
manifest path the way the proxy loads it, and routing, the provider's auth-file
handling, the chat->Responses bridge, SSE parsing, stream aggregation, error
translation and retry decisions use the real packages.

What this proves, in the order the cases run:
  (a) the platform's exact NON-streaming body (strict json_schema response_format,
      max_completion_tokens, reasoning_effort) returns a normal completion --
      content == the fixture's JSON, finish_reason stop, role assistant, usage
      exactly the fixture's -- from ONE upstream POST whose body carries
      text.format == the schema and reasoning.effort medium and NO max_output_tokens;
  (b) the gpt-6-astra-realjaynesage route (dsh's request shape) still sends the
      byte-identical body it sent before the handler existed -- no `text` -- even
      when a wrapped and an unwrapped call run concurrently;
  (c) streaming through the route yields the role-only first chunk, the text
      deltas, a terminal finish_reason stop and (with include_usage) a usage chunk;
      a consumer that abandons the stream, and an upstream that stalls between
      chunks, both end with the upstream transport closed (the stall at the deadline);
  (d) upstream 401 / 429 / 503 map to AuthenticationError / RateLimitError /
      ServiceUnavailableError from ONE POST each (no inner retry), and a stalled
      upstream hits the handler's deadline from ONE POST with the stream closed;
  (e) an HTML 403 (the Cloudflare challenge seen live on 2026-09-20), a stream
      truncated before response.completed, a response.incomplete terminal event
      and a refusal item each fail to be a `stop` completion with usable content;
  (f) the placeholder auth.json builds the Router with no socket attempt, and a
      MISSING file still sends the first call to the device flow (the existing
      guard is unchanged by the handler);
  (g) one outer request produces exactly one success-callback record, under the
      outer provider, and the inner `no-log` call produces none;
  (h) a cost-map entry for the inner id that says supports_native_streaming false is
      overridden by the handler's pins, so the upstream call still streams natively.
The retained-memory bounds (chunk count, content bytes) are exercised with the limits lowered.
Closure is asserted on the mock TRANSPORT (its aclose()), after a normal drain and after a
mid-drain stall at the deadline.
"""

import atexit
import os
import shutil
import socket
import tempfile

# These must be set before importing LiteLLM: use its bundled metadata and never
# contact telemetry or fetch a model catalog during this offline contract test.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_DISABLE_TELEMETRY"] = "true"
os.environ["DO_NOT_TRACK"] = "1"
TOKEN_ROOT = tempfile.mkdtemp(prefix="chatgpt-chat-auth-")
atexit.register(shutil.rmtree, TOKEN_ROOT, ignore_errors=True)
os.environ["CHATGPT_TOKEN_DIR"] = TOKEN_ROOT
NETWORK_ATTEMPTS = []


def _deny_connect(_socket, address):
    NETWORK_ATTEMPTS.append(str(address))
    raise AssertionError("Unexpected network access in offline route contract")


def _deny_resolve(host, port, *_args, **_kwargs):
    NETWORK_ATTEMPTS.append(f"resolve:{host}:{port}")
    raise AssertionError("Unexpected name resolution in offline route contract")


socket.socket.connect = _deny_connect
socket.socket.connect_ex = _deny_connect
socket.getaddrinfo = _deny_resolve

import asyncio
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import pathlib
import sys
import time
import unittest
from unittest import mock

import httpx
import litellm
import yaml
from litellm import Router
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.utils import custom_llm_setup


ROOT = pathlib.Path(__file__).resolve().parents[3]
AI_DIR = ROOT / "kubernetes/apps/apps/ai"
MANIFEST = AI_DIR / "litellm.yaml"
MANIFEST_BYTES = MANIFEST.read_bytes()
DOCUMENTS = list(yaml.safe_load_all(MANIFEST_BYTES))
CONFIG = yaml.safe_load(next(
    document for document in DOCUMENTS
    if isinstance(document, dict) and document.get("kind") == "ConfigMap"
    and document["metadata"]["name"] == "litellm-config"
)["data"]["config.yaml"])
# The routes are found by SHAPE, not by name: the subscription route is whichever entry is served
# by the custom provider, the paid route whichever is openai/gpt-5.6-sol + the key. After the ADR 0027
# switch-back (gpt-5.6-sol pointed back at the API key, the handler retired) there is no
# subscription route and this module SKIPS instead of failing a required check.
SUBSCRIPTION_ROUTE = next((entry for entry in CONFIG["model_list"]
                           if str(entry["litellm_params"].get("model", "")).startswith("chatgpt-chat/")), None)
RETIRED = "no chatgpt-chat/ route in the manifest: the subscription handler is retired (ADR 0027); nothing to contract"     if SUBSCRIPTION_ROUTE is None else ""
if RETIRED:
    # Placeholders so the module-level constants below still build; the TestCase is skipped
    # (`skipIf`), which every runner -- direct execution, `-m unittest <name>`, discovery, pytest --
    # reports as a skip, unlike a module-level SystemExit or SkipTest.
    SUBSCRIPTION_ROUTE = {"model_name": "gpt-5.6-sol", "litellm_params": {"model": "chatgpt-chat/gpt-5.6-sol"}}
PAID_ROUTE = next((entry for entry in CONFIG["model_list"]
                   if entry["litellm_params"].get("model") == "openai/gpt-5.6-sol"), None)
assert PAID_ROUTE is not None, "the retained paid route (openai/gpt-5.6-sol + OPENAI_API_KEY) is missing from model_list"
DSH_ROUTE = next((entry for entry in CONFIG["model_list"] if entry["model_name"] == "gpt-6-astra-realjaynesage"), None)
assert DSH_ROUTE is not None, "the gpt-6-astra-realjaynesage route (ADR 0026) is missing from model_list; case (b) contracts it"
ROUTES = {SUBSCRIPTION_ROUTE["model_name"]: SUBSCRIPTION_ROUTE, PAID_ROUTE["model_name"]: PAID_ROUTE,
          DSH_ROUTE["model_name"]: DSH_ROUTE}
SUBSCRIPTION_NAME = SUBSCRIPTION_ROUTE["model_name"]
INNER_MODEL = SUBSCRIPTION_ROUTE["litellm_params"]["model"].split("/", 1)[1]
PROVIDER_MAP = CONFIG["litellm_settings"]["custom_provider_map"]
DEPLOYMENT = next(
    document for document in DOCUMENTS
    if isinstance(document, dict) and document.get("kind") == "Deployment"
    and document["metadata"]["name"] == "litellm"
)
CONTAINER = next(item for item in DEPLOYMENT["spec"]["template"]["spec"]["containers"]
                 if item["name"] == "litellm")
CONTAINER_ENV = {item["name"]: item.get("value") for item in CONTAINER["env"]}
HANDLER_PATH = AI_DIR / "chatgpt_chat.py"
HANDLER_BYTES = HANDLER_PATH.read_bytes()
UPSTREAM_URL = "https://chatgpt.com/backend-api/codex/responses"
DEVICE_FLOW_HOST = "auth.openai.com"
# The auth.json shape litellm-chatgpt-eso.yaml renders before a seat logs in (the device-flow guard).
PLACEHOLDER_AUTH = {"access_token": "unconfigured", "account_id": "unconfigured", "expires_at": 4102444800}

# ---- the platform adapter's exact request (services/workflow/src/worker/document_fields/model.py) ----
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["answer", "n"],
    "additionalProperties": False,
}
PLATFORM_REQUEST = {
    "model": SUBSCRIPTION_NAME,
    "messages": [{"role": "user", "content": "Offline adapter fixture."}],
    "max_completion_tokens": 32000,
    "reasoning_effort": "medium",
    "response_format": {
        "type": "json_schema",
        "json_schema": {"name": "document_schema", "strict": True, "schema": SCHEMA},
    },
}
ANSWER = '{"answer":"OK","n":7}'
# Responses-API usage as chatgpt.com reports it; the bridge maps input->prompt, output->completion.
USAGE = {"input_tokens": 55, "input_tokens_details": {"cached_tokens": 0},
         "output_tokens": 17, "output_tokens_details": {"reasoning_tokens": 6}, "total_tokens": 72}
# What the wire body of the wrapped call must be, byte for byte: the provider's allow-list plus
# `text`, which the handler re-adds. `input` is what the bridge makes of the one user message.
EXPECTED_WRAPPED_BODY = {
    "model": INNER_MODEL,
    "input": [{"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "Offline adapter fixture."}]}],
    "instructions": None,  # filled from the Deployment env in setUpClass
    "stream": True,
    "store": False,
    "include": ["reasoning.encrypted_content"],
    "reasoning": {"effort": "medium"},
    "text": {"format": {"type": "json_schema", "name": "document_schema", "schema": SCHEMA, "strict": True}},
}
# dsh's request on the existing route and its measured wire body (test_litellm_chatgpt_route_contract.py).
DSH_INPUT = [
    {"role": "developer", "content": "You are dsh's offline fixture. Answer with OK."},
    {"role": "user", "content": "Offline adapter fixture."},
]
DSH_REQUEST = {
    "model": "gpt-6-astra-realjaynesage",
    "input": DSH_INPUT,
    "reasoning": {"effort": "high", "summary": "auto"},
    "include": ["reasoning.encrypted_content"],
    "store": False,
    "max_output_tokens": 64,
    "text": {"verbosity": "low"},
}
EXPECTED_DSH_BODY = {
    "model": "gpt-6-astra",
    "input": DSH_INPUT,
    "instructions": None,  # filled from the Deployment env in setUpClass
    "stream": True,
    "store": False,
    "include": ["reasoning.encrypted_content"],
    "reasoning": {"effort": "high", "summary": "auto"},
}
CLOUDFLARE_HTML = ("<html><head><title>Just a moment...</title></head><body><div class=\"container\">"
                   "Enable JavaScript and cookies to continue</div></body></html>")


def sse(events):
    """Encode Responses events the way chatgpt.com does: `event:` line, `data:` line, blank line."""
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def chatgpt_stream(text=ANSWER, model="gpt-5.6-sol", terminal="completed", usage=USAGE, refusal=None):
    """The 17-event shape measured live on 2026-09-20 for one gpt-5.6-sol answer: the message
    arrives in output_item.done and response.completed carries output: []."""
    resp = {"id": "resp_fixture", "object": "response", "created_at": 1, "model": model,
            "status": "in_progress", "error": None, "incomplete_details": None, "output": []}
    msg_id = "msg_fixture"
    events = [
        {"type": "response.created", "sequence_number": 0, "response": resp},
        {"type": "response.in_progress", "sequence_number": 1, "response": resp},
        {"type": "response.output_item.added", "sequence_number": 2, "output_index": 0,
         "item": {"id": msg_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}},
    ]
    seq = 3
    if refusal is None:
        events.append({"type": "response.content_part.added", "sequence_number": seq, "output_index": 0,
                       "item_id": msg_id, "content_index": 0,
                       "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""}})
        seq += 1
        for piece in [text[i:i + 3] for i in range(0, len(text), 3)]:
            events.append({"type": "response.output_text.delta", "sequence_number": seq, "output_index": 0,
                           "item_id": msg_id, "content_index": 0, "delta": piece, "logprobs": [], "obfuscation": ""})
            seq += 1
        if terminal == "truncated":
            return sse(events)  # EOF before the message is done
        events.append({"type": "response.output_text.done", "sequence_number": seq, "output_index": 0,
                       "item_id": msg_id, "content_index": 0, "text": text, "logprobs": []})
        seq += 1
        events.append({"type": "response.content_part.done", "sequence_number": seq, "output_index": 0,
                       "item_id": msg_id, "content_index": 0,
                       "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": text}})
        seq += 1
        content = [{"type": "output_text", "annotations": [], "logprobs": [], "text": text}]
    else:
        events.append({"type": "response.refusal.done", "sequence_number": seq, "output_index": 0,
                       "item_id": msg_id, "content_index": 0, "refusal": refusal})
        seq += 1
        content = [{"type": "refusal", "refusal": refusal}]
    events.append({"type": "response.output_item.done", "sequence_number": seq, "output_index": 0,
                   "item": {"id": msg_id, "type": "message", "status": "completed", "role": "assistant",
                            "content": content}})
    seq += 1
    if terminal == "incomplete":
        final = {**resp, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                 "output": []}
        if usage is not None:
            final["usage"] = usage
        events.append({"type": "response.incomplete", "sequence_number": seq, "response": final})
    else:
        final = {**resp, "status": "completed", "completed_at": 2, "output": []}
        if usage is not None:
            final["usage"] = usage
        events.append({"type": "response.completed", "sequence_number": seq, "response": final})
    return sse(events)


def _write_auth(directory, content):
    target = pathlib.Path(directory) / "auth.json"
    staging = pathlib.Path(directory) / "auth.json.tmp"
    staging.write_text(content if isinstance(content, str) else json.dumps(content))
    os.replace(staging, target)


def _use_token_dir(name, auth=PLACEHOLDER_AUTH):
    path = pathlib.Path(TOKEN_ROOT) / name
    path.mkdir()
    os.environ["CHATGPT_TOKEN_DIR"] = str(path)
    if auth is not None:
        _write_auth(path, auth)
    return path


class _ByteStream(httpx.AsyncByteStream):
    """The upstream SSE body as httpx would stream it, 64 bytes per read, optionally stalling for
    good part-way through (a mid-drain hang). aclose() is the transport-level closure the handler
    must reach after a normal drain AND after abandoning the stream."""

    def __init__(self, payload, closed, stall_after=None):
        self._payload, self._closed, self._stall_after = payload, closed, stall_after

    async def __aiter__(self):
        for offset in range(0, len(self._payload), 64):
            if self._stall_after is not None and offset >= self._stall_after:
                await asyncio.sleep(3600)
            yield self._payload[offset:offset + 64]

    async def aclose(self):
        self._closed.append("transport")


def _mock_post(sent, outcome, closed):
    """Stand in for AsyncHTTPHandler.post, the one seam the Responses path uses. Records the wire
    request and answers like the real method: a 2xx Response is returned (a streaming body), a
    non-2xx raises httpx.HTTPStatusError as raise_for_status() would. `outcome` is a body kind or a
    status code; "stall" hangs before any response, "stall-mid" hangs inside the body."""
    loads = json.loads

    async def post(_self, url, data=None, json=None, params=None, headers=None,
                   timeout=None, stream=False, files=None, content=None, logging_obj=None):
        body = loads(data) if data else json
        sent.append({"url": url, "headers": dict(headers or {}), "body": body, "stream": stream})
        kind = outcome(body) if callable(outcome) else outcome
        request = httpx.Request("POST", url)
        if kind == "stall":
            await asyncio.sleep(3600)
        if kind in ("success", "stall-mid", "truncated", "incomplete", "refusal", "nousage"):
            payload = {
                "success": lambda: chatgpt_stream(model=body["model"]),
                "stall-mid": lambda: chatgpt_stream(model=body["model"]),
                "truncated": lambda: chatgpt_stream(model=body["model"], terminal="truncated"),
                "incomplete": lambda: chatgpt_stream(model=body["model"], terminal="incomplete"),
                "refusal": lambda: chatgpt_stream(model=body["model"], refusal="I can't help with that."),
                "nousage": lambda: chatgpt_stream(model=body["model"], usage=None),
            }[kind]()

            return httpx.Response(200, stream=_ByteStream(payload, closed, stall_after=256 if kind == "stall-mid" else None),
                                  headers={"content-type": "text/event-stream"}, request=request)
        if kind == "403html":
            response = httpx.Response(403, content=CLOUDFLARE_HTML.encode(),
                                      headers={"content-type": "text/html"}, request=request)
        else:
            response = httpx.Response(int(kind), json={"error": {"message": "offline", "type": "x"}},
                                      headers={"retry-after": "0"}, request=request)
        response.raise_for_status()
        return response
    return post


class _CountingLogger(CustomLogger):
    def __init__(self):
        super().__init__()
        self.successes = []
        self.failures = []

    # A custom provider's success logging runs on the SYNC path (streaming_handler submits
    # run_success_logging_and_cache_storage to an executor), so both variants are counted.
    def _record(self, kwargs):
        self.successes.append({"model": kwargs.get("model"),
                               "provider": (kwargs.get("litellm_params") or {}).get("custom_llm_provider")})

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record(kwargs)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.failures.append({"model": kwargs.get("model")})

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.failures.append({"model": kwargs.get("model")})


def _load_handler_like_the_proxy():
    """get_instance_fn: spec_from_file_location(<module>, <config dir>/<module>.py), then getattr."""
    spec = importlib.util.spec_from_file_location("chatgpt_chat", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["chatgpt_chat"] = module
    return module


@unittest.skipIf(bool(RETIRED), RETIRED)
class ChatGPTChatHandlerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert CONTAINER_ENV.get("CHATGPT_TOKEN_DIR") == "/chatgpt-auth", "Deployment must mount the auth dir"
        cls.instructions = CONTAINER_ENV.get("CHATGPT_DEFAULT_INSTRUCTIONS")
        assert cls.instructions, "CHATGPT_DEFAULT_INSTRUCTIONS must be set and non-empty"
        os.environ["CHATGPT_DEFAULT_INSTRUCTIONS"] = cls.instructions
        EXPECTED_WRAPPED_BODY["instructions"] = cls.instructions
        EXPECTED_DSH_BODY["instructions"] = cls.instructions
        assert PROVIDER_MAP == [{"provider": "chatgpt-chat", "custom_handler": "chatgpt_chat.handler"}], PROVIDER_MAP
        cls.module = _load_handler_like_the_proxy()
        litellm.custom_provider_map = [{"provider": item["provider"],
                                        "custom_handler": getattr(cls.module, item["custom_handler"].split(".")[-1])}
                                       for item in PROVIDER_MAP]
        custom_llm_setup()
        litellm.drop_params = CONFIG["litellm_settings"]["drop_params"]
        litellm.telemetry = False
        cls.logger = _CountingLogger()
        litellm.callbacks = [cls.logger]

    def _router(self):
        # Production router_settings (num_retries: 2, least-busy, fallbacks...) and the three routes
        # as the manifest declares them: the contract is what the deployed proxy does.
        return Router(model_list=[copy.deepcopy(ROUTES[name]) for name in ROUTES],
                      **copy.deepcopy(CONFIG["router_settings"]))

    async def _call(self, router, request, **extra):
        """One call; a streaming response is drained here, inside the caller's mock context, and
        returned as the list of its chunks."""
        try:
            response = await asyncio.wait_for(router.acompletion(**copy.deepcopy(request), **extra), timeout=25)
            if extra.get("stream"):
                chunks = []

                async def drain():
                    async for chunk in response:
                        chunks.append(chunk)
                await asyncio.wait_for(drain(), timeout=25)
                return chunks, None
            return response, None
        except Exception as exc:
            return None, exc

    async def _exercise(self, label, outcome, request, auth=PLACEHOLDER_AUTH, **extra):
        _use_token_dir(label, auth)
        sent, closed = [], []  # closed: transport-level aclose() calls (see _ByteStream)
        started = time.monotonic()
        with mock.patch.object(AsyncHTTPHandler, "post", _mock_post(sent, outcome, closed)):
            router = self._router()
            litellm.callbacks = [self.logger]  # Router.reset() at the end of the previous case cleared it
            self.logger.successes.clear()
            try:
                response, error = await self._call(router, request, **extra)
                if error is None:
                    # Success logging is submitted to an executor; let it land before reset() clears
                    # the callbacks, or (g) would race it.
                    for _ in range(60):
                        if self.logger.successes:
                            break
                        await asyncio.sleep(0.05)
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                router.reset()
        elapsed = time.monotonic() - started
        self.assertEqual(NETWORK_ATTEMPTS, [], f"{label}: no socket connection may be attempted")
        self.assertLess(elapsed, 20, f"{label}: seconds, never the device flow's minutes")
        print(json.dumps({"case": label, "upstream_attempts": len(sent), "closed": len(closed),
                          "error": type(error).__name__ if error else None, "elapsed_s": round(elapsed, 2)}))
        return sent, closed, response, error

    def _assert_wrapped_wire(self, label, entry):
        self.assertEqual(entry["url"], UPSTREAM_URL, label)
        self.assertIs(entry["stream"], True, f"{label}: native streaming -- never should_fake_stream's plain POST")
        self.assertEqual(entry["headers"].get("Authorization"), "Bearer unconfigured", label)
        self.assertEqual(entry["body"], EXPECTED_WRAPPED_BODY,
                         f"{label}: text and reasoning present, max_output_tokens absent, nothing else changed; "
                         f"actual={json.dumps(entry['body'], sort_keys=True)}")

    # (a) -------------------------------------------------------------------------------------
    async def _case_a_platform_body(self):
        sent, closed, response, error = await self._exercise("a", "success", PLATFORM_REQUEST)
        self.assertIsNone(error, f"a: {error!r}")
        self.assertEqual(len(sent), 1, "a: exactly one upstream POST")
        self._assert_wrapped_wire("a", sent[0])
        choice = response.choices[0]
        self.assertEqual(response.model, "gpt-5.6-sol")
        self.assertEqual(choice.finish_reason, "stop")
        self.assertEqual(choice.message.role, "assistant")
        self.assertEqual(choice.message.content, ANSWER)
        self.assertIsNone(getattr(choice.message, "refusal", None))
        self.assertFalse(choice.message.tool_calls)
        self.assertEqual((response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens),
                         (USAGE["input_tokens"], USAGE["output_tokens"], USAGE["total_tokens"]),
                         "a: usage is the fixture's, never fabricated")
        details = getattr(response.usage, "completion_tokens_details", None)
        self.assertEqual(getattr(details, "reasoning_tokens", None), USAGE["output_tokens_details"]["reasoning_tokens"])
        self.assertEqual(closed, ["transport"], "a: the upstream transport is closed after the drain")
        # (g) one success record for the OUTER call only, under the outer provider
        self.assertEqual(len(self.logger.successes), 1, f"g: {self.logger.successes}")
        self.assertEqual(self.logger.successes[0]["provider"], "chatgpt-chat")
        self.assertIn("gpt-5.6-sol", self.logger.successes[0]["model"])

    # (b) -------------------------------------------------------------------------------------
    async def _case_b_dsh_route_unchanged_and_concurrent(self):
        _use_token_dir("b")
        sent, closed = [], []
        with mock.patch.object(AsyncHTTPHandler, "post", _mock_post(sent, "success", closed)):
            router = self._router()
            try:
                async def dsh():
                    response = await asyncio.wait_for(router.aresponses(**copy.deepcopy(DSH_REQUEST)), timeout=25)
                    if hasattr(response, "__aiter__"):
                        async for _ in response:
                            pass
                    return response
                results = await asyncio.gather(self._call(router, PLATFORM_REQUEST), dsh(), self._call(router, PLATFORM_REQUEST),
                                               return_exceptions=True)
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                router.reset()
        self.assertEqual(NETWORK_ATTEMPTS, [])
        self.assertEqual(len(sent), 3, "b: three upstream POSTs, one per call")
        bodies = {json.dumps(e["body"], sort_keys=True) for e in sent}
        self.assertIn(json.dumps(EXPECTED_DSH_BODY, sort_keys=True), bodies,
                      "b: the dsh route's body is byte-identical to the pre-handler contract (no text)")
        wrapped = [e for e in sent if e["body"]["model"] == "gpt-5.6-sol"]
        self.assertEqual(len(wrapped), 2)
        for entry in wrapped:
            self._assert_wrapped_wire("b", entry)
        for result in (results[0], results[2]):
            self.assertIsNone(result[1], f"b: wrapped call failed: {result[1]!r}")
            self.assertEqual(result[0].choices[0].message.content, ANSWER)
        self.assertNotIsInstance(results[1], Exception, f"b: dsh call failed: {results[1]!r}")
        print(json.dumps({"case": "b", "upstream_attempts": len(sent), "concurrent": True}))

    # (c) -------------------------------------------------------------------------------------
    async def _case_c_streaming(self):
        sent, closed, chunks, error = await self._exercise(
            "c", "success", PLATFORM_REQUEST, stream=True, stream_options={"include_usage": True})
        self.assertIsNone(error, f"c: {error!r}")
        self.assertEqual(len(sent), 1)
        self._assert_wrapped_wire("c", sent[0])
        first = chunks[0].choices[0].delta
        self.assertEqual(first.role, "assistant", "c: the role-only first chunk passes through")
        text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices and c.choices[0].delta)
        self.assertEqual(text, ANSWER)
        finishes = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]
        self.assertEqual(finishes, ["stop"], "c: exactly one terminal chunk, finish_reason stop")
        usages = [c.usage for c in chunks if getattr(c, "usage", None)]
        self.assertTrue(usages, "c: include_usage surfaces a usage chunk")
        # Measured in the pinned image: for a STREAMING caller the proxy's outer wrapper computes
        # the include_usage chunk from its own token count, as it does for every custom provider
        # (9 here, against the fixture's 17). Exact upstream usage is a guarantee of the
        # non-streaming path -- the platform's -- case (a); here only its presence and sanity.
        self.assertGreater(usages[-1].completion_tokens, 0)
        self.assertGreater(usages[-1].prompt_tokens, 0)
        self.assertTrue(all(c.model == "gpt-5.6-sol" for c in chunks), "c: chunks carry the bare model id")
        self.assertEqual(closed, ["transport"], "c: the upstream transport is closed after the last chunk")
        print(json.dumps({"case": "c", "chunks": len(chunks), "finish": finishes}))

    async def _case_c_streaming_cancellation(self, kind):
        """Streaming, abandoned: (abandon) the consumer stops after two chunks and closes the outer
        stream -- the proxy's path on a client disconnect -- and the upstream transport must be closed
        promptly; (stall-mid) the upstream hangs between two chunks and the consumer's next read must
        hit the handler's deadline, with the transport closed, not hang or cancel elsewhere."""
        _use_token_dir(f"c-{kind}")
        sent, closed = [], []
        outcome = "stall-mid" if kind == "stall-mid" else "success"
        started = time.monotonic()
        with mock.patch.object(AsyncHTTPHandler, "post", _mock_post(sent, outcome, closed)):
            router = self._router()
            litellm.callbacks = [self.logger]
            try:
                stream = await asyncio.wait_for(
                    router.acompletion(**copy.deepcopy(PLATFORM_REQUEST), stream=True, timeout=2), timeout=25)
                got, error = [], None
                try:
                    async for chunk in stream:
                        got.append(chunk)
                        if kind == "abandon" and len(got) == 2:
                            break
                except Exception as exc:  # noqa: BLE001
                    error = exc
                await stream.aclose()
                await asyncio.sleep(0.05)
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                router.reset()
        elapsed = time.monotonic() - started
        self.assertEqual(NETWORK_ATTEMPTS, [])
        self.assertEqual(len(sent), 1, f"c-{kind}: one upstream request")
        if kind == "abandon":
            self.assertIsNone(error)
            self.assertEqual(len(got), 2)
        else:
            self.assertEqual(type(error).__name__, "Timeout", f"c-stall-mid: {error!r}")
            self.assertLess(elapsed, 15, "c-stall-mid: the handler's deadline, delivered to the consumer's read")
        self.assertEqual(closed, ["transport"], f"c-{kind}: the upstream transport is closed on abandonment")
        print(json.dumps({"case": f"c-{kind}", "chunks": len(got), "error": type(error).__name__ if error else None,
                          "closed": closed, "elapsed_s": round(elapsed, 2)}))

    # (d) -------------------------------------------------------------------------------------
    async def _case_d_upstream_rejection(self, status, error_name):
        sent, closed, response, error = await self._exercise(f"d-{status}", status, PLATFORM_REQUEST)
        self.assertEqual(type(error).__name__, error_name, f"d-{status}: {error!r}")
        self.assertEqual(len(sent), 1, f"d-{status}: ONE upstream attempt -- no inner retry, no outer retry")
        self.assertIsNone(response)

    async def _case_d_stall(self, kind):
        started = time.monotonic()
        sent, closed, response, error = await self._exercise(f"d-{kind}", kind, PLATFORM_REQUEST, timeout=2)
        self.assertEqual(type(error).__name__, "Timeout", f"d-{kind}: {error!r}")
        self.assertEqual(len(sent), 1, f"d-{kind}: one upstream request, no retry after the deadline")
        self.assertLess(time.monotonic() - started, 15, f"d-{kind}: the handler's deadline, not the stall")
        if kind == "stall-mid":
            self.assertEqual(closed, ["transport"],
                             "d-stall-mid: abandoning the stream at the deadline closes the upstream transport")

    async def _case_e_bounds(self, kind):
        """The retained-memory bounds, with the limits lowered for the fixture: a stream of more
        chunks than MAX_CHUNKS, and text over MAX_CONTENT_BYTES. The handler also counts refusal,
        reasoning and tool-call payloads, but the 1.101.0 bridge emits none of them on its chunks
        (the refusal case above measures that), so text is the only payload a fixture can reach."""
        patch = {"many-chunks": ("MAX_CHUNKS", 5), "oversized-text": ("MAX_CONTENT_BYTES", 8)}[kind]
        with mock.patch.object(self.module, patch[0], patch[1]):
            sent, closed, response, error = await self._exercise(f"e-{kind}", "success", PLATFORM_REQUEST)
        self.assertEqual(len(sent), 1)
        self.assertEqual(type(error).__name__, "BadGatewayError", f"e-{kind}: {error!r}")
        self.assertIsNone(response)
        self.assertEqual(closed, ["transport"], f"e-{kind}: the abandoned upstream transport is closed")

    async def _case_h_incompatible_map_entry(self):
        """A map entry for the inner id that says supports_native_streaming false / mode chat (the
        remote map could ship one) must not put the upstream call on the plain-POST path: the
        handler pins the capability over the entry, not only when the entry is missing."""
        inner = f"chatgpt/{INNER_MODEL}"
        saved = litellm.model_cost.get(inner)
        litellm.model_cost[inner] = {"litellm_provider": "chatgpt", "mode": "chat", "supports_native_streaming": False,
                                     "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
        self.module._REGISTERED.discard(INNER_MODEL)
        try:
            before = litellm.get_model_info(inner)
            self.assertEqual((before.get("mode"), before.get("supports_native_streaming")), ("chat", False),
                             "h: the incompatible entry is in place before the call")
            sent, closed, response, error = await self._exercise("h-map", "success", PLATFORM_REQUEST)
            after = litellm.get_model_info(inner)  # BEFORE restoration: what the handler merged in
            self.assertEqual((after.get("mode"), after.get("supports_native_streaming")), ("responses", True),
                             "h: the pins are merged over the existing entry")
        finally:
            if saved is None:
                litellm.model_cost.pop(inner, None)
            else:
                litellm.model_cost[inner] = saved
            self.module._REGISTERED.discard(INNER_MODEL)
        self.assertIsNone(error, f"h: {error!r}")
        self.assertEqual(len(sent), 1)
        self._assert_wrapped_wire("h", sent[0])  # includes the native-streaming assertion
        self.assertEqual(response.choices[0].message.content, ANSWER)

    # (e) -------------------------------------------------------------------------------------
    async def _case_e_negative(self, kind):
        sent, closed, response, error = await self._exercise(f"e-{kind}", kind, PLATFORM_REQUEST)
        self.assertEqual(len(sent), 1, f"e-{kind}: one attempt")
        if kind in ("403html", "truncated", "nousage"):
            # Measured in the pinned image: the bridge turns a stream truncated before its terminal
            # event into an error, and the handler refuses a stream whose usage the upstream never
            # reported (nousage) -- stream_chunk_builder would otherwise back-fill token ESTIMATES
            # the consumer would record as billed truth.
            self.assertIsNotNone(error, f"e-{kind}: must not become a completion (got {response!r})")
            self.assertIsNone(response)
            return
        self.assertIsNone(error, f"e-{kind}: {error!r}")
        choice = response.choices[0]
        if kind == "incomplete":
            self.assertEqual(choice.finish_reason, "length",
                             "e-incomplete: response.incomplete(max_output_tokens) is a length stop, never a stop")
        elif kind == "refusal":
            # Measured in the pinned image (ADR 0027 accepted loss): the bridge carries no refusal
            # text on its chunks, so the refusal comes back as EMPTY content with refusal None --
            # never as an answer. The consumer fails closed on it (empty content is not its JSON),
            # though not through its refusal guard. If a pin bump starts carrying the text, the
            # second assertion flips and this note gets retired.
            self.assertEqual(choice.message.content, "", "e-refusal: a refusal never yields answer text")
            self.assertIsNone(getattr(choice.message, "refusal", None),
                              "e-refusal: 1.101.0 drops the refusal text (measured); revisit on a pin bump")
        print(json.dumps({"case": f"e-{kind}", "finish": choice.finish_reason,
                          "content": (choice.message.content or "")[:40],
                          "refusal": getattr(choice.message, "refusal", None)}))

    # (f) -------------------------------------------------------------------------------------
    async def _case_f_missing_file_device_flow(self):
        directory = _use_token_dir("f-missing", auth=None)
        self.assertFalse((directory / "auth.json").exists())
        NETWORK_ATTEMPTS.clear()
        sent, closed = [], []
        started = time.monotonic()
        with mock.patch.object(AsyncHTTPHandler, "post", _mock_post(sent, "success", closed)):
            # Only the custom route: a chatgpt-chat/ deployment does not run the authenticator at
            # construction (a chatgpt/ deployment does -- test_litellm_chatgpt_route_contract.py
            # case c covers that), so construction must succeed and the FIRST CALL must be the
            # device-flow attempt.
            router = Router(model_list=[copy.deepcopy(ROUTES["gpt-5.6-sol"])],
                            **copy.deepcopy(CONFIG["router_settings"]))
            self.assertEqual(NETWORK_ATTEMPTS, [], "f: construction must not reach for the network")
            try:
                response, error = await self._call(router, PLATFORM_REQUEST)
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                router.reset()
        self.assertIsNotNone(error, "f: the first call must fail without a token")
        self.assertTrue(NETWORK_ATTEMPTS and all(DEVICE_FLOW_HOST in a for a in NETWORK_ATTEMPTS),
                        f"f: the device flow is still the failure mode of a missing file: {NETWORK_ATTEMPTS}")
        self.assertEqual(sent, [], "f: nothing reaches the upstream without a token")
        self.assertLess(time.monotonic() - started, 20)
        print(json.dumps({"case": "f-missing", "error": type(error).__name__, "network_attempts": NETWORK_ATTEMPTS}))
        NETWORK_ATTEMPTS.clear()

    def test_chatgpt_chat_handler_contract(self):
        async def run_cases():
            try:
                with self.subTest(case="a+g"):
                    await self._case_a_platform_body()
                with self.subTest(case="b"):
                    await self._case_b_dsh_route_unchanged_and_concurrent()
                with self.subTest(case="c"):
                    await self._case_c_streaming()
                for kind in ("abandon", "stall-mid"):
                    with self.subTest(case=f"c-{kind}"):
                        await self._case_c_streaming_cancellation(kind)
                for status, error_name in (("401", "AuthenticationError"), ("429", "RateLimitError"),
                                           ("503", "ServiceUnavailableError")):
                    with self.subTest(case=f"d-{status}"):
                        await self._case_d_upstream_rejection(status, error_name)
                for kind in ("stall", "stall-mid"):
                    with self.subTest(case=f"d-{kind}"):
                        await self._case_d_stall(kind)
                for kind in ("403html", "truncated", "incomplete", "refusal", "nousage"):
                    with self.subTest(case=f"e-{kind}"):
                        await self._case_e_negative(kind)
                for kind in ("many-chunks", "oversized-text"):
                    with self.subTest(case=f"e-{kind}"):
                        await self._case_e_bounds(kind)
                with self.subTest(case="h"):
                    await self._case_h_incompatible_map_entry()
                with self.subTest(case="f"):
                    await self._case_f_missing_file_device_flow()
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                await GLOBAL_LOGGING_WORKER.stop()
        asyncio.run(run_cases())


if __name__ == "__main__":
    if RETIRED:
        print(RETIRED)
    package = pathlib.Path(litellm.__file__).parent
    sources = ("router.py", "llms/custom_llm.py", "llms/chatgpt/responses/transformation.py",
               "completion_extras/litellm_responses_transformation/transformation.py",
               "litellm_core_utils/streaming_handler.py")
    print(json.dumps({
        "python": sys.version,
        "packages": {name: importlib.metadata.version(name)
                     for name in ("litellm", "openai", "httpx", "pydantic")},
        "manifest_sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
        "handler_sha256": hashlib.sha256(HANDLER_BYTES).hexdigest(),
        "adapter_source_sha256": {name: hashlib.sha256((package / name).read_bytes()).hexdigest()
                                  for name in sources},
    }))
    unittest.main(verbosity=2)
