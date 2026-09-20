"""chatgpt-chat: LiteLLM custom provider that serves Chat Completions from the ChatGPT subscription.

WHY THIS EXISTS (measured 2026-09-20 in the pinned LiteLLM 1.101.0 image, ADR 0027). LiteLLM's own
`chatgpt/` provider (litellm/llms/chatgpt) is Responses-native against chatgpt.com/backend-api/codex
and is what serves gpt-6-astra-realjaynesage (ADR 0026). Two things stop a plain `chatgpt/<model>`
deployment from serving the Strive document-table workflow, whose adapter is NON-streaming Chat
Completions with a strict `response_format` json_schema:
  1. Non-streaming chat-completions through `chatgpt/` 500 ("Unknown items in responses API
     response: []"): the provider forces `stream: true` upstream, chatgpt.com's `response.completed`
     event carries `output: []` (the message arrives only in `response.output_item.done`), and the
     chat->Responses bridge's empty-output recovery does not recover this SSE shape. Streaming works.
  2. The provider's Responses allow-list (ChatGPTResponsesAPIConfig.transform_responses_api_request)
     drops `text`, so a caller's `response_format` never reaches the backend and the model answers in
     free text. The backend DOES accept `text.format` (strict json_schema, enforced). It REJECTS
     `max_output_tokens` (400 "Unsupported parameter"), so that drop stays.

WHAT IT DOES. `chatgpt-chat/<model>` = one inner `chatgpt/<model>` call, always streamed, with inner
retries disabled, under ONE deadline (the proxy's request timeout) that covers the call and the drain;
the upstream stream is closed in `finally` so a client disconnect or the deadline stops the upstream
read instead of letting it run on against the subscription. A non-streaming caller gets the chunks
rebuilt into a normal ModelResponse (`litellm.stream_chunk_builder` over the bridge's own chunks, capped at MAX_CONTENT_BYTES of content,
usage taken from the upstream's response.completed block and refused when there is none -- LiteLLM's
stream wrapper would otherwise substitute a token-count estimate);
a streaming caller gets the chunks re-yielded unchanged. Around the inner call a ContextVar
is set, and a wrapper on the provider's request transform re-adds `text` ONLY while that var is set --
every other `chatgpt/` call on this proxy (dsh's gpt-6-astra-realjaynesage) sends the byte-identical
request it sends today. The inner call is `no-log` so the proxy's spend/metrics logging records the
request once, under the outer model name.

WHAT IT DOES NOT DO. It does not touch auth: the inner call reads $CHATGPT_TOKEN_DIR/auth.json per
request exactly as the existing route does (litellm-chatgpt-eso.yaml renders it). It adds no retries
or fallbacks. It does not pass `max_output_tokens` (backend 400): a caller's `max_completion_tokens`
is dropped at the provider, as on the existing route, so the backend's own output ceiling is the only
generation bound and MAX_CONTENT_BYTES the only memory bound. A caller's `reasoning_effort` reaches
this handler only because the route carries `allowed_openai_params: [reasoning_effort]` -- custom
providers map params through OpenAILikeChatConfig, whose list lacks it, and `drop_params: true`
would otherwise drop it silently before this code runs.

LOADED BY the proxy from `litellm_settings.custom_provider_map` (litellm.yaml). The file is merged into
the litellm-config ConfigMap (kustomization.yaml, `behavior: merge`), so it sits beside config.yaml at
/etc/litellm and `get_instance_fn` loads it from the config directory; a pod that restarts reads both
from the same ConfigMap, so it can never see a custom_provider_map entry without the module it names.
The `checksum/chatgpt-chat` pod-template annotation (scripts/check-inline-hashes.py) is what rolls the
Deployment on an edit here. The import-time assertions below make a pin bump that moves the upstream
seam FAIL THE POD START (the old ReplicaSet keeps serving under maxUnavailable: 0) rather than silently
serve without the schema; scripts/tests/integration/test_litellm_chatgpt_chat_handler_contract.py runs
this module in the pinned image in CI so that failure shows at PR time.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
from collections.abc import AsyncIterator
from typing import Any

import litellm
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.types.utils import ModelResponse

PROVIDER = "chatgpt-chat"
# `chatgpt/responses/<model>`, NOT `chatgpt/<model>`: LiteLLM sends a chat-completions call through its
# chat->Responses bridge only when the model's info says `mode: responses`. For `chatgpt/gpt-5.6-sol`
# that mode comes from the REMOTE model-cost map the pod fetches at start; the bundled map (a pod that
# starts without network, CI) has no such entry, and the call then falls into the OpenAI-SDK chat path
# against chatgpt.com/backend-api/codex/chat/completions, which the Codex backend does not serve.
# The `responses/` prefix is LiteLLM's documented way to force the bridge regardless of the map
# (measured 2026-09-20: responses_api_bridge_check("responses/gpt-5.6-sol", "chatgpt") -> mode
# responses under the bundled map; the same call without the prefix -> {}).
INNER_PROVIDER = "chatgpt"
INNER_MODEL_PREFIX = f"{INNER_PROVIDER}/responses/"
# Bounds on what a non-streaming call may retain before it is rebuilt: the platform adapter's own
# MAX_RESPONSE_BYTES over EVERY delta payload (text, refusal, reasoning, tool-call JSON), and a chunk
# count, because chatgpt.com streams a few characters per event and a stream of empty events would
# otherwise escape the byte count. 32 K output tokens arrive in ~45 K events measured; 262 144 is
# ~6x that and still a few hundred MB of retained chunks at most on the shared 6 GiB proxy.
MAX_CONTENT_BYTES = 4 * 1024**2
MAX_CHUNKS = 262_144
# Set only for the duration of THIS provider's inner call; read by the transform wrapper below.
_PASS_TEXT: contextvars.ContextVar[bool] = contextvars.ContextVar("chatgpt_chat_pass_text", default=False)
# Chat-completion kwargs the handler owns (or the Router injects) and must not forward into the inner call.
_OWNED_PARAMS = frozenset({"stream", "stream_options", "no-log", "max_retries", "num_retries"})

# ---- the upstream seam, pinned ---------------------------------------------------------------
_UPSTREAM_TRANSFORM = ChatGPTResponsesAPIConfig.transform_responses_api_request
_EXPECTED_SIGNATURE = ("self", "model", "input", "response_api_optional_request_params", "litellm_params", "headers")
_actual_signature = tuple(inspect.signature(_UPSTREAM_TRANSFORM).parameters)
if _actual_signature != _EXPECTED_SIGNATURE:  # a pin bump moved the seam: refuse to start
    raise ImportError(
        f"{PROVIDER}: ChatGPTResponsesAPIConfig.transform_responses_api_request signature changed to "
        f"{_actual_signature}; re-verify the text pass-through before serving (ADR 0027)"
    )


def _transform_with_scoped_text(self, model, input, response_api_optional_request_params, litellm_params, headers):
    request = _UPSTREAM_TRANSFORM(self, model, input, response_api_optional_request_params, litellm_params, headers)
    if _PASS_TEXT.get():
        text = response_api_optional_request_params.get("text")
        if text is not None and "text" not in request:
            request["text"] = text
    return request


ChatGPTResponsesAPIConfig.transform_responses_api_request = _transform_with_scoped_text  # type: ignore[method-assign]


def _inner_kwargs(model: str, messages: list, optional_params: dict | None, timeout: Any) -> dict:
    forwarded = {k: v for k, v in (optional_params or {}).items() if k not in _OWNED_PARAMS}
    kwargs: dict = {
        "model": f"{INNER_MODEL_PREFIX}{model}",
        "messages": messages,
        "stream": True,
        # NO stream_options.include_usage on the inner call: it makes the inner wrapper synthesise a
        # usage chunk from a token count whether or not the upstream reported usage, which erases the
        # signal _upstream_reported_usage relies on. A streaming caller's own include_usage is served
        # by the OUTER wrapper as for any provider.
        # One attempt, explicitly: the outer route's num_retries does not reach a nested call.
        "num_retries": 0,
        "no-log": True,
        **forwarded,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


def _deadline_seconds(timeout: Any) -> float | None:
    """The one deadline for call + drain: a number as given, an httpx.Timeout by its read value, else none."""
    if isinstance(timeout, (int, float)):
        return float(timeout)
    read = getattr(timeout, "read", None)
    return float(read) if isinstance(read, (int, float)) else None


_REGISTERED: set[str] = set()
# What the inner id's model info MUST say for the transport to be right, whatever the map says:
# the chat->Responses bridge (mode) and native streaming (should_fake_stream reads
# supports_native_streaming; False or absent -> a plain non-streaming POST the Codex backend does
# not serve). Pinned over any existing entry, not only added when one is missing.
_INNER_PINS = {"litellm_provider": "chatgpt", "mode": "responses", "supports_native_streaming": True}


def _ensure_model_info(model: str) -> None:
    """Pin the inner id's model info so the transport does not depend on which cost map the pod
    loaded. The remote map the pod fetches at start lists chatgpt/gpt-5.6-sol (mode responses);
    the bundled fallback map does not, and an entry could also arrive with
    supports_native_streaming false. The Router does a similar registration for a deployment's own
    model_info (that is why gpt-6-astra-realjaynesage streams natively); this handler's deployment
    is the OUTER id, so the inner id is pinned here, merged over whatever entry exists. Zero cost on
    both ids where no price is known: the subscription has no per-token price, and it keeps cost
    logging from warning on every request."""
    if model in _REGISTERED:
        return
    inner = f"{INNER_PROVIDER}/{model}"
    try:
        existing = dict(litellm.get_model_info(inner))
    except Exception:  # noqa: BLE001 - no entry
        existing = {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
    entries = {inner: {**existing, **_INNER_PINS}}
    outer = f"{PROVIDER}/{model}"
    try:
        litellm.get_model_info(outer)
    except Exception:  # noqa: BLE001
        entries[outer] = {"litellm_provider": PROVIDER, "mode": "chat", "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
    litellm.register_model(entries)
    _REGISTERED.add(model)


async def _open_inner_stream(model: str, messages: list, optional_params: dict | None, timeout: Any):
    _ensure_model_info(model)
    token = _PASS_TEXT.set(True)
    try:
        return await litellm.acompletion(**_inner_kwargs(model, messages, optional_params, timeout))
    finally:
        _PASS_TEXT.reset(token)


def _delta_bytes(chunk: Any) -> int:
    """Bytes a chunk would retain: every delta payload, not only text."""
    if not chunk.choices:
        return 0
    delta = chunk.choices[0].delta
    if delta is None:
        return 0
    total = 0
    for attr in ("content", "refusal", "reasoning_content"):
        value = getattr(delta, attr, None)
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
    tool_calls = getattr(delta, "tool_calls", None)
    if tool_calls:
        total += len(json.dumps([tc.model_dump() if hasattr(tc, "model_dump") else tc for tc in tool_calls], default=str))
    return total


async def _close(stream: Any) -> None:
    """Close the inner wrapper AND the httpx response under it. The wrapper's aclose() reaches the
    bridge iterator, whose aclose() only closes an `http_response` the bridge never attaches, so an
    abandoned stream (deadline, client disconnect) would otherwise keep its upstream connection open
    until garbage collection (measured with a stalling transport)."""
    response = getattr(getattr(getattr(stream, "completion_stream", None), "streaming_response", None), "response", None)
    for target in (stream, response):
        aclose = getattr(target, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - closing is best-effort; the caller's outcome is already decided
                pass


class ChatGPTChat(CustomLLM):
    """See the module docstring. Only the async pair is implemented: the proxy never calls the sync one."""

    def completion(self, *args, **kwargs):
        raise CustomLLMError(status_code=501, message=f"{PROVIDER}: synchronous completion is not served; use the async proxy path")

    def streaming(self, *args, **kwargs):
        raise CustomLLMError(status_code=501, message=f"{PROVIDER}: synchronous streaming is not served; use the async proxy path")

    async def acompletion(  # noqa: PLR0913 - LiteLLM's CustomLLM signature
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers=None,
        timeout=None,
        client=None,
    ) -> ModelResponse:
        chunks: list = []
        content_bytes = 0
        stream = None
        try:
            async with asyncio.timeout(_deadline_seconds(timeout)):
                stream = await _open_inner_stream(model, messages, optional_params, timeout)
                # Drain the bridge's own iterator (CustomStreamWrapper.completion_stream), NOT the
                # wrapper: the wrapper replaces a missing upstream usage with a token-count ESTIMATE
                # in the same slot it stores a real one, so nothing downstream of it can tell the two
                # apart. The bridge's terminal chunk carries `usage` iff response.completed did.
                raw = getattr(stream, "completion_stream", None)
                if raw is None or not hasattr(raw, "__aiter__"):
                    raise CustomLLMError(status_code=502, message=f"{PROVIDER}: unexpected inner stream shape {type(stream).__name__}")
                async for chunk in raw:
                    chunks.append(chunk)
                    if len(chunks) > MAX_CHUNKS:
                        raise CustomLLMError(status_code=502, message=f"{PROVIDER}: upstream stream exceeded {MAX_CHUNKS} chunks")
                    content_bytes += _delta_bytes(chunk)
                    if content_bytes > MAX_CONTENT_BYTES:
                        raise CustomLLMError(status_code=502, message=f"{PROVIDER}: upstream content exceeded {MAX_CONTENT_BYTES} bytes")
        except TimeoutError as exc:
            raise litellm.Timeout(message=f"{PROVIDER}: deadline reached before the upstream stream completed",
                                  model=model, llm_provider=PROVIDER) from exc
        finally:
            if stream is not None:
                await _close(stream)
        # The consumer records usage as billed truth and fails closed on a missing block, so an
        # estimate must never pass as one: no upstream usage -> an error, not a completion.
        # KNOWN LOSS (measured 2026-09-20): the bridge in 1.101.0 carries no refusal text on its
        # chunks, so a refusal item comes back as an EMPTY content with refusal None; the consumer
        # still fails closed on it (empty content is not its JSON), just not through its refusal
        # guard. ADR 0027.
        usage = next((chunk.usage for chunk in reversed(chunks) if getattr(chunk, "usage", None)), None)
        if usage is None:
            raise CustomLLMError(status_code=502, message=f"{PROVIDER}: the upstream stream reported no usage")
        built = litellm.stream_chunk_builder(chunks, messages=messages)
        if built is None or not getattr(built, "choices", None):
            raise CustomLLMError(status_code=502, message=f"{PROVIDER}: the upstream stream carried no completion")
        built.usage = usage  # the upstream's block, not the builder's count
        built.model = model
        return built

    async def astreaming(  # noqa: PLR0913 - LiteLLM's CustomLLM signature
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers=None,
        timeout=None,
        client=None,
    ) -> AsyncIterator[Any]:
        # The proxy's CustomStreamWrapper accepts ModelResponseStream chunks from a custom provider
        # as-is (streaming_handler: the `_custom_providers` branch), so every delta the chatgpt/
        # route emits -- role-only, usage-only, tool-call, refusal -- passes through unchanged.
        #
        # The deadline is ABSOLUTE and scoped to the upstream awaits only: an asyncio.timeout held
        # open across `yield` would cancel the CONSUMER's task while this generator sits suspended
        # (e.g. in a slow downstream send) instead of this read. Each upstream read gets the time
        # left; a consumer that stops iterating (client disconnect -> the outer wrapper's aclose)
        # closes this generator, and the finally closes the upstream. (codex impl-review round 2)
        stream = None
        loop = asyncio.get_running_loop()
        budget = _deadline_seconds(timeout)
        expires = None if budget is None else loop.time() + budget
        try:
            async with asyncio.timeout(budget):
                stream = await _open_inner_stream(model, messages, optional_params, timeout)
            raw = getattr(stream, "completion_stream", None)
            if raw is None or not hasattr(raw, "__aiter__"):
                raise CustomLLMError(status_code=502, message=f"{PROVIDER}: unexpected inner stream shape {type(stream).__name__}")
            iterator = raw.__aiter__()  # the bridge iterator initialises itself here, not in __anext__
            first = True
            while True:
                remaining = None if expires is None else expires - loop.time()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                try:
                    async with asyncio.timeout(remaining):
                        chunk = await iterator.__anext__()  # the bridge's chunks: the terminal one carries the upstream usage
                except StopAsyncIteration:
                    break
                chunk.model = model
                if first and chunk.choices and chunk.choices[0].delta is not None:
                    # The wrapper this bypasses is what stamps `role` on the first chunk; keep
                    # the OpenAI shape streaming clients expect.
                    chunk.choices[0].delta.role = "assistant"
                    first = False
                yield chunk  # outside every timeout context
        except TimeoutError as exc:
            raise litellm.Timeout(message=f"{PROVIDER}: deadline reached before the upstream stream completed",
                                  model=model, llm_provider=PROVIDER) from exc
        finally:
            if stream is not None:
                await _close(stream)


handler = ChatGPTChat()
