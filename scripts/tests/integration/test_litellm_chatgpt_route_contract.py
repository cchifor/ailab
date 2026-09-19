"""Exercise the gpt-6-astra-realjaynesage route through the pinned LiteLLM chatgpt/ provider.

Run with the manifest's LiteLLM image and networking disabled:
    python scripts/tests/integration/test_litellm_chatgpt_route_contract.py
Only the upstream HTTP transport is mocked; routing, the provider's auth-file
handling, parameter transformation, SSE parsing, error translation and retry
decisions use the real packages (ADR 0026; plan 2026-09-19 cases a-e).

What this proves, in the order the cases run:
  (a) the production-shaped auth.json (placeholder token + far-future sentinel
      expires_at, exactly what litellm-chatgpt-eso.yaml renders) builds the Router
      and sends a Responses call to chatgpt.com/backend-api/codex with NO socket
      attempt, the override instructions, store=false and the encrypted-reasoning
      include -- and the transform drops max_output_tokens/text (accepted loss);
  (b) a JWT whose exp is in the PAST behaves identically under the sentinel: the
      file's expires_at is what keeps LiteLLM out of the device flow;
  (c) a MISSING file, an EMPTY token and a MALFORMED file each send LiteLLM into
      the OAuth device flow (auth.openai.com) at Router construction -- the
      executable record of why the ESO template must ALWAYS render (a);
  (d) upstream 401 / 429 map to AuthenticationError / RateLimitError in seconds;
  (e) ONE Router, three calls, the file rewritten atomically between them: each
      call carries the token and account id of the file current at call time,
      so a rotation reaches LiteLLM with nothing restarted.
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
# The chatgpt/ provider resolves CHATGPT_TOKEN_DIR when it constructs its
# Authenticator (once per get_llm_provider / Responses call, never at import), so
# the variable exists before the import and each case points it at a fresh
# directory. The default (~/.config/litellm/chatgpt) would be shared host state.
# Removed at exit: the fixtures are placeholders and unsigned test JWTs, but a
# run outside `docker run --rm` (a pod, a workstation) must not leave them behind.
TOKEN_ROOT = tempfile.mkdtemp(prefix="chatgpt-auth-")
atexit.register(shutil.rmtree, TOKEN_ROOT, ignore_errors=True)
os.environ["CHATGPT_TOKEN_DIR"] = TOKEN_ROOT
NETWORK_ATTEMPTS = []


def _deny_connect(_socket, address):
    NETWORK_ATTEMPTS.append(str(address))
    raise AssertionError("Unexpected network access in offline route contract")


def _deny_resolve(host, port, *_args, **_kwargs):
    # Recorded at the resolver, not only at connect(): CI runs this under
    # `docker run --network none`, where getaddrinfo fails before any connect()
    # is reached, and case (c) must still observe the device-flow attempt there.
    NETWORK_ATTEMPTS.append(f"resolve:{host}:{port}")
    raise AssertionError("Unexpected name resolution in offline route contract")


socket.socket.connect = _deny_connect
socket.socket.connect_ex = _deny_connect
socket.getaddrinfo = _deny_resolve

import asyncio
import base64
import copy
import hashlib
import importlib.metadata
import json
import pathlib
import re
import sys
import time
import unittest
from unittest import mock

import httpx
import litellm
import yaml
from litellm import Router
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler


ROOT = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "kubernetes/apps/apps/ai/litellm.yaml"
MANIFEST_BYTES = MANIFEST.read_bytes()
DOCUMENTS = list(yaml.safe_load_all(MANIFEST_BYTES))
CONFIG = yaml.safe_load(next(
    document for document in DOCUMENTS
    if isinstance(document, dict) and document.get("kind") == "ConfigMap"
    and document["metadata"]["name"] == "litellm-config"
)["data"]["config.yaml"])
ROUTE = next(entry for entry in CONFIG["model_list"]
             if entry["model_name"] == "gpt-6-astra-realjaynesage")
DEPLOYMENT = next(
    document for document in DOCUMENTS
    if isinstance(document, dict) and document.get("kind") == "Deployment"
    and document["metadata"]["name"] == "litellm"
)
CONTAINER = next(item for item in DEPLOYMENT["spec"]["template"]["spec"]["containers"]
                 if item["name"] == "litellm")
CONTAINER_ENV = {item["name"]: item.get("value") for item in CONTAINER["env"]}
UPSTREAM_URL = "https://chatgpt.com/backend-api/codex/responses"
DEVICE_FLOW_HOST = "auth.openai.com"
# The sentinel litellm-chatgpt-eso.yaml renders (2100-01-01T00:00:00Z). The provider
# trusts a numeric expires_at over the token's own exp claim, so this is what keeps
# a stale or placeholder token on the fail-fast (401 upstream) path (plan finding 2).
ESO_MANIFEST = ROOT / "kubernetes/apps/apps/ai/litellm-chatgpt-eso.yaml"
ESO_DOCS = list(yaml.safe_load_all(ESO_MANIFEST.read_bytes()))
EXTERNAL_SECRET = next(d for d in ESO_DOCS if isinstance(d, dict) and d.get("kind") == "ExternalSecret"
                       and d["metadata"]["name"] == "litellm-chatgpt-auth")
AUTH_TEMPLATE = EXTERNAL_SECRET["spec"]["target"]["template"]["data"]["auth.json"]


def render_auth_template(fields):
    """Render the production template the way ESO's v2 engine (sprig) does for the three
    constructs it uses -- `.KEY`, `| default "x"`, `| toJson` -- over the document's fields.

    A tiny evaluator on purpose: it accepts exactly the pipeline shapes the template is allowed to
    carry, so a template edit that drops `default` or `toJson`, or changes the sentinel, changes
    the fixture this test runs LiteLLM against instead of leaving a stale hard-coded one passing
    (codex impl-review round 1). Anything it cannot evaluate is a test failure, not a guess.
    """
    def evaluate(expr):
        stages = [s.strip() for s in expr.split("|")]
        head = stages[0]
        if not head.startswith("."):
            raise AssertionError(f"unsupported template head {head!r}")
        value = fields.get(head[1:])
        if value is None:
            raise AssertionError(f"template references {head}, which the document does not carry")
        for stage in stages[1:]:
            if stage.startswith("default "):
                fallback = json.loads(stage[len("default "):])
                value = value if value != "" else fallback
            elif stage == "toJson":
                value = json.dumps(value)
            else:
                raise AssertionError(f"unsupported template stage {stage!r}")
        return value
    rendered = re.sub(r"\{\{(.*?)\}\}", lambda m: evaluate(m.group(1)), AUTH_TEMPLATE)
    return json.loads(rendered)


# The fixture IS the production rendering over the document exactly as the provisioning Job
# seeds it: every CHATGPT_* key present and EMPTY. What LiteLLM is then handed is what the litellm
# pods hold before the seat logs in -- and the assertions below on its shape are what keep the
# template's three guards (placeholder, sentinel, JSON-safety) from silently disappearing.
SEEDED_EMPTY_DOCUMENT = {"CHATGPT_ACCESS_TOKEN": "", "CHATGPT_ACCOUNT_ID": "",
                         "CHATGPT_ACCOUNT_EMAIL": "", "CHATGPT_EXPIRES_AT": "",
                         "CHATGPT_OPENBAO_CANARY": "provisioned"}
PLACEHOLDER_AUTH = render_auth_template(SEEDED_EMPTY_DOCUMENT)
assert set(PLACEHOLDER_AUTH) == {"access_token", "account_id", "expires_at"}, PLACEHOLDER_AUTH
assert isinstance(PLACEHOLDER_AUTH["access_token"], str) and PLACEHOLDER_AUTH["access_token"] != "", \
    "the template must render a NON-EMPTY placeholder token (device-flow guard)"
assert isinstance(PLACEHOLDER_AUTH["expires_at"], int) and PLACEHOLDER_AUTH["expires_at"] > 4_000_000_000, \
    "the template must carry a far-future numeric expires_at (device-flow guard)"
SENTINEL_EXPIRES_AT = PLACEHOLDER_AUTH["expires_at"]
# JSON-safety: a token carrying a quote must still render valid JSON (that is what `toJson` buys).
assert render_auth_template({**SEEDED_EMPTY_DOCUMENT, "CHATGPT_ACCESS_TOKEN": 'a"b\\c'})["access_token"] == 'a"b\\c'
INPUT = [
    {"role": "developer", "content": "You are dsh's offline fixture. Answer with OK."},
    {"role": "user", "content": "Offline adapter fixture."},
]
# The shape dsh sends on its openai-responses provider: reasoning with an encrypted
# summary, store=false, a token cap and a verbosity hint. The last two are the
# documented loss on this route (litellm.yaml model_list comment, ADR 0026).
REQUEST = {
    "model": "gpt-6-astra-realjaynesage",
    "input": INPUT,
    "reasoning": {"effort": "high", "summary": "auto"},
    "include": ["reasoning.encrypted_content"],
    "store": False,
    "max_output_tokens": 64,
    "text": {"verbosity": "low"},
}
# Measured 2026-09-19 in the pinned image: the provider's allowlist after the
# transform, with every optional key this request actually carries.
EXPECTED_BODY_KEYS = {"include", "input", "instructions", "model", "reasoning", "store", "stream"}
COMPLETED = {
    "type": "response.completed",
    "response": {
        "id": "resp_x", "object": "response", "created_at": 1, "model": "gpt-6-astra",
        "status": "completed",
        "output": [{"type": "message", "id": "msg_x", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK", "annotations": []}]}],
        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    },
}
SSE_BODY = ("event: response.completed\ndata: " + json.dumps(COMPLETED) + "\n\n").encode()


def _jwt(claims):
    """A syntactically valid, unsigned JWT: base64url header.payload.signature."""
    def segment(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}.sig"


def _use_token_dir(name):
    """Point CHATGPT_TOKEN_DIR at a fresh, empty directory for one case."""
    path = pathlib.Path(TOKEN_ROOT) / name
    path.mkdir()
    os.environ["CHATGPT_TOKEN_DIR"] = str(path)
    return path


def _write_auth(directory, content):
    """Write auth.json the way kubelet projects a Secret: whole file, atomic rename."""
    target = directory / "auth.json"
    staging = directory / "auth.json.tmp"
    staging.write_text(content if isinstance(content, str) else json.dumps(content))
    os.replace(staging, target)


def _mock_post(sent, outcome):
    """Stand in for AsyncHTTPHandler.post, the one seam the Responses path uses (it
    never touches litellm.aclient_session). Records the wire request and answers
    like the real method: a 2xx Response is returned, anything else raises
    httpx.HTTPStatusError exactly as its raise_for_status() would, so the
    handler's _handle_error sees the same shape it sees in production."""
    loads = json.loads

    async def post(_self, url, data=None, json=None, params=None, headers=None,
                   timeout=None, stream=False, files=None, content=None, logging_obj=None):
        sent.append({"url": url, "headers": dict(headers or {}),
                     "body": loads(data) if data else json, "stream": stream})
        request = httpx.Request("POST", url)
        if outcome == "success":
            return httpx.Response(200, content=SSE_BODY,
                                  headers={"content-type": "text/event-stream"}, request=request)
        response = httpx.Response(int(outcome), json={"error": {"message": "offline", "type": "x"}},
                                  headers={"retry-after": "0"}, request=request)
        response.raise_for_status()
        return response
    return post


class ChatGPTRouteContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert CONTAINER_ENV.get("CHATGPT_TOKEN_DIR") == "/chatgpt-auth", "Deployment must mount the auth dir"
        cls.instructions = CONTAINER_ENV.get("CHATGPT_DEFAULT_INSTRUCTIONS")
        assert cls.instructions, "CHATGPT_DEFAULT_INSTRUCTIONS must be set and non-empty"
        # The provider reads this variable per request; the Deployment's value is
        # what the contract is about, so every case runs under exactly that value.
        os.environ["CHATGPT_DEFAULT_INSTRUCTIONS"] = cls.instructions
        litellm.drop_params = CONFIG["litellm_settings"]["drop_params"]
        litellm.telemetry = False

    def _router(self):
        # Production router_settings, num_retries: 2 included -- the contract is
        # what the deployed proxy does with this route, retries and all.
        return Router(model_list=[copy.deepcopy(ROUTE)], **copy.deepcopy(CONFIG["router_settings"]))

    async def _call(self, router):
        """One Responses call; drains the stream the provider forces (stream:true)
        and returns (events, error_name)."""
        events = []
        error = None
        try:
            response = await asyncio.wait_for(router.aresponses(**copy.deepcopy(REQUEST)), timeout=25)

            async def drain():
                if hasattr(response, "__aiter__"):
                    async for event in response:
                        events.append(event)
                else:
                    events.append(response)
            await asyncio.wait_for(drain(), timeout=25)
        except Exception as exc:
            error = type(exc).__name__
        finally:
            # Drain callbacks before the case resets its Router callbacks.
            await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
        return events, error

    async def _exercise(self, label, outcome, files):
        """Write the first auth.json, build ONE Router on it, then one call per
        entry of `files` (each rewritten atomically before its call). Returns the
        recorded wire requests and the per-call (events, error) pairs; asserts
        the offline and fast invariants. The first file must precede the Router:
        get_llm_provider runs the authenticator at construction (case c)."""
        sent = []
        results = []
        started = time.monotonic()
        _write_auth(*files[0])
        with mock.patch.object(AsyncHTTPHandler, "post", _mock_post(sent, outcome)):
            router = self._router()
            try:
                for index, (directory, auth) in enumerate(files):
                    if index:
                        _write_auth(directory, auth)
                    results.append(await self._call(router))
            finally:
                router.reset()
        elapsed = time.monotonic() - started
        self.assertEqual(NETWORK_ATTEMPTS, [], f"{label}: no socket connection may be attempted")
        self.assertLess(elapsed, 10, f"{label}: seconds, not the device flow's minutes")
        print(json.dumps({"case": label, "upstream_attempts": len(sent),
                          "errors": [error for _, error in results], "elapsed_s": round(elapsed, 2)}))
        return sent, results

    def _assert_wire_request(self, label, entry, token, account_id):
        self.assertEqual(entry["url"], UPSTREAM_URL, label)
        self.assertTrue(entry["stream"], f"{label}: the provider streams unconditionally")
        headers = entry["headers"]
        self.assertEqual(headers.get("Authorization"), f"Bearer {token}", label)
        self.assertEqual(headers.get("ChatGPT-Account-Id"), account_id, label)
        self.assertTrue(headers.get("originator"), f"{label}: originator header must be present")
        body = entry["body"]
        self.assertEqual(set(body), EXPECTED_BODY_KEYS, f"{label}: body keys are the transform's allowlist")
        self.assertEqual(body["model"], "gpt-6-astra", label)
        self.assertEqual(body["instructions"], self.instructions,
                         f"{label}: the override is sent verbatim, without the Codex-CLI persona")
        self.assertIs(body["store"], False, label)
        self.assertIs(body["stream"], True, label)
        self.assertIn("reasoning.encrypted_content", body["include"], label)
        self.assertEqual(body["reasoning"], REQUEST["reasoning"], label)
        self.assertEqual(body["input"], INPUT, f"{label}: developer + user input forwarded as sent")
        self.assertNotIn("max_output_tokens", body, f"{label}: accepted loss, ADR 0026")
        self.assertNotIn("text", body, f"{label}: accepted loss, ADR 0026")

    async def _case_a_placeholder_file(self):
        directory = _use_token_dir("a-placeholder")
        sent, results = await self._exercise("a", "success", [(directory, PLACEHOLDER_AUTH)])
        self.assertEqual(len(sent), 1, "exactly one upstream attempt")
        self._assert_wire_request("a", sent[0], "unconfigured", "unconfigured")
        events, error = results[0]
        self.assertIsNone(error)
        completed = [event for event in events if getattr(event, "type", None) == "response.completed"]
        self.assertEqual(len(completed), 1, "the stream must surface the response.completed frame")
        self.assertEqual(completed[0].response.output[0].content[0].text, "OK")

    async def _case_b_past_jwt_exp_under_sentinel(self):
        directory = _use_token_dir("b-expired-jwt")
        expired = _jwt({"exp": 1})
        sent, results = await self._exercise(
            "b", "success", [(directory, {**PLACEHOLDER_AUTH, "access_token": expired})])
        self.assertEqual(len(sent), 1, "the file's expires_at wins over the JWT exp: no device flow")
        self._assert_wire_request("b", sent[0], expired, "unconfigured")
        self.assertIsNone(results[0][1])

    async def _case_c_device_flow(self, name, content):
        directory = _use_token_dir(f"c-{name}")
        if content is not None:
            _write_auth(directory, content)
        NETWORK_ATTEMPTS.clear()
        sent = []
        started = time.monotonic()
        error = None
        router = None
        with mock.patch.object(AsyncHTTPHandler, "post", _mock_post(sent, "success")):
            try:
                # Construction is where the 2026-09-19 probe measured the attempt
                # (get_llm_provider runs the authenticator); the first call is the
                # fallback should a future pin move it.
                router = self._router()
                _, error = await self._call(router)
            except Exception as exc:
                error = type(exc).__name__
            finally:
                if router is not None:
                    router.reset()
        elapsed = time.monotonic() - started
        self.assertIsNotNone(error, f"{name}: Router construction or the first call must raise")
        self.assertTrue(NETWORK_ATTEMPTS, f"{name}: the device flow must have reached for the network")
        self.assertTrue(all(DEVICE_FLOW_HOST in attempt for attempt in NETWORK_ATTEMPTS),
                        f"{name}: every attempt is the OAuth device flow: {NETWORK_ATTEMPTS}")
        self.assertEqual(sent, [], f"{name}: nothing may reach the upstream without a token")
        self.assertLess(elapsed, 10, f"{name}: the denied attempt fails fast")
        print(json.dumps({"case": f"c-{name}", "error": error,
                          "network_attempts": NETWORK_ATTEMPTS, "elapsed_s": round(elapsed, 2)}))
        NETWORK_ATTEMPTS.clear()

    async def _case_d_upstream_rejection(self, status, error_name, attempts):
        directory = _use_token_dir(f"d-{status}")
        sent, results = await self._exercise(f"d-{status}", status, [(directory, PLACEHOLDER_AUTH)])
        self.assertEqual(results[0][1], error_name)
        self.assertEqual(len(sent), attempts, f"{status}: upstream attempts under production router_settings")
        for index, entry in enumerate(sent):
            self._assert_wire_request(f"d-{status}[{index}]", entry, "unconfigured", "unconfigured")

    async def _case_e_rotation_without_restart(self):
        directory = _use_token_dir("e-rotation")
        token_a = _jwt({"exp": SENTINEL_EXPIRES_AT,
                        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-a"}})
        token_b = _jwt({"exp": SENTINEL_EXPIRES_AT,
                        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-b"}})
        files = [
            PLACEHOLDER_AUTH,
            {"access_token": token_a, "account_id": "acct-a", "expires_at": SENTINEL_EXPIRES_AT},
            {"access_token": token_b, "account_id": "acct-b", "expires_at": SENTINEL_EXPIRES_AT},
        ]
        sent, results = await self._exercise("e", "success", [(directory, auth) for auth in files])
        self.assertEqual(len(sent), len(files), "one upstream attempt per call")
        for index, auth in enumerate(files):
            self.assertIsNone(results[index][1], f"e[{index}]")
            self._assert_wire_request(f"e[{index}]", sent[index], auth["access_token"], auth["account_id"])
        self.assertEqual(len({entry["headers"]["Authorization"] for entry in sent}), len(files),
                         "each call carried the token of the file current at call time")

    def test_chatgpt_route_contract(self):
        async def run_cases():
            try:
                with self.subTest(case="a"):
                    await self._case_a_placeholder_file()
                with self.subTest(case="b"):
                    await self._case_b_past_jwt_exp_under_sentinel()
                for name, content in (("missing", None),
                                      ("empty-token", {**PLACEHOLDER_AUTH, "access_token": ""}),
                                      ("malformed", "not json")):
                    with self.subTest(case=f"c-{name}"):
                        await self._case_c_device_flow(name, content)
                # Measured 2026-09-19 in the pinned image with the production
                # router_settings (num_retries: 2) and a single deployment: a 401
                # is NOT retried (1 attempt, 0.05 s); a 429 IS retried num_retries
                # times (3 attempts, 4.6 s -- a retry-after of 0 is outside the
                # header's honoured (0, 60] range, so the Router's exponential
                # backoff applies: INITIAL_RETRY_DELAY 0.5 * 2^n + JITTER <= 0.75
                # per sleep). Both stay under the 10 s bound _exercise enforces;
                # neither approaches the device flow's minutes.
                retries = CONFIG["router_settings"]["num_retries"]
                for status, error_name, attempts in (("401", "AuthenticationError", 1),
                                                     ("429", "RateLimitError", 1 + retries)):
                    with self.subTest(case=f"d-{status}"):
                        await self._case_d_upstream_rejection(status, error_name, attempts)
                with self.subTest(case="e"):
                    await self._case_e_rotation_without_restart()
            finally:
                await asyncio.wait_for(GLOBAL_LOGGING_WORKER.flush(), timeout=5)
                await GLOBAL_LOGGING_WORKER.stop()
        # LiteLLM's logging worker is process-wide; all cases share its loop.
        asyncio.run(run_cases())


if __name__ == "__main__":
    package = pathlib.Path(litellm.__file__).parent
    sources = ("router.py", "llms/chatgpt/authenticator.py", "llms/chatgpt/common_utils.py",
               "llms/chatgpt/responses/transformation.py")
    print(json.dumps({
        "python": sys.version,
        "packages": {name: importlib.metadata.version(name)
                     for name in ("litellm", "openai", "httpx", "pydantic")},
        "manifest_sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
        "adapter_source_sha256": {name: hashlib.sha256((package / name).read_bytes()).hexdigest()
                                  for name in sources},
    }))
    unittest.main(verbosity=2)
