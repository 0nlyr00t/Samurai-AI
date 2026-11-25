"""OpenAI-compatible provider client.

POSTs to the registered endpoint's /chat/completions through the governed
pinned-HTTP broker. LLM traffic is operator infrastructure and does NOT
follow the operation target scope; instead the client is constructed with a
scope derived from the registered base_url, so it can only ever reach the
registered endpoint host (fail-closed). The API key comes from the vault
and is scrubbed from any error text.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from samurai_ai.agents.provider import (
    IMAGE_CARRIER,
    STREAM_RESET,
    ChatRequest,
    ChatResponse,
    ImagePart,
    Message,
    ToolCall,
    ToolCallDelta,
    Usage,
    new_call_id,
    retry_after_seconds,
    wire_messages,
)
from samurai_ai.domain.execution import SecretRedactor, ToolExecutionError
from samurai_ai.domain.models import (
    ModelError,
    ProviderDefinition,
    ProviderUnavailableError,
    parse_model_id,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.http import HttpEgressBroker, ProviderStreamError

DEFAULT_TIMEOUT = 180.0
_MAX_ERROR_SNIPPET = 200


def broker_for_base_url(base_url: str, verify_tls: bool = True) -> HttpEgressBroker:
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname
    if not host:
        raise ModelError(f"provider base_url must contain a host: {base_url!r}")
    return HttpEgressBroker(ScopeContract(includes=(host,)), verify_tls=verify_tls)


def _wire_message(message: Message) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    return {"role": message.role, "content": message.content}


def _attach_images(messages: list[dict[str, Any]], images: tuple[ImagePart, ...]) -> None:
    """Fold images into the last user turn as content parts.

    A tool result cannot carry images here, so when the history ends on one the
    images ride in a user turn of their own right after it.
    """
    if messages and messages[-1]["role"] == "tool":
        messages.append({"role": "user", "content": IMAGE_CARRIER})
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": message["content"]}]
        parts += [
            {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{image.data}"}}
            for image in images
        ]
        message["content"] = parts
        return


def build_completion_payload(
    model_name: str, request: ChatRequest
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [_wire_message(message) for message in wire_messages(request)],
    }
    if request.images:
        _attach_images(payload["messages"], request.images)
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.response_format is not None:
        payload["response_format"] = request.response_format
    if request.reasoning_effort and request.reasoning_effort != "off":
        payload["reasoning_effort"] = request.reasoning_effort
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    return payload


def parse_completion(
    model_id: str, provider_id: str, payload: dict[str, Any], latency_ms: int
) -> ChatResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError("chat completion response has no choices")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    tool_calls: list[ToolCall] = []
    if "DSML" in text:
        text, salvaged = _salvage_dsml(text)
        tool_calls.extend(salvaged)
    for entry in message.get("tool_calls") or []:
        function = entry.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        tool_calls.append(
            ToolCall(
                id=str(entry.get("id") or new_call_id()),
                name=str(function.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    usage = payload.get("usage") or {}
    return ChatResponse(
        text=text,
        tool_calls=tuple(tool_calls),
        usage=Usage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=_cached_tokens(usage),
        ),
        model_id=model_id,
        provider_id=provider_id,
        latency_ms=latency_ms,
        stop_reason=str(choices[0].get("finish_reason") or ""),
    )


def _usage(prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> Usage:
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
    )


def _cached_tokens(usage: dict[str, Any]) -> int:
    """Prefix-cache hits, however the gateway spells them; 0 when it cannot say."""
    details = usage.get("prompt_tokens_details") or {}
    for value in (
        details.get("cached_tokens") if isinstance(details, dict) else None,
        usage.get("cache_read_input_tokens"),
        usage.get("prompt_cache_hit_tokens"),
    ):
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _json_dict(raw: object) -> dict[str, Any]:
    """Parse a streamed tool-call argument string into a dict, never raising."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OpenAICompatProvider:
    def __init__(
        self,
        provider: ProviderDefinition,
        api_key: str | None,
        broker: HttpEgressBroker | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.broker = broker or broker_for_base_url(
            provider.base_url, verify_tls=provider.verify_tls
        )
        self.timeout = timeout
        self.redactor = SecretRedactor()
        if api_key:
            self.redactor.register(api_key)

    async def health(self) -> bool:
        try:
            response = await asyncio.to_thread(self._get, "/models")
        except ProviderUnavailableError:
            return False
        return response < 400

    async def list_models(self) -> list[str]:
        payload = await asyncio.to_thread(self._get_json, "/models")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ModelError(f"{self.provider.id} /models returned no data array")
        return sorted(str(entry.get("id")) for entry in data if isinstance(entry, dict))

    async def complete(self, request: ChatRequest) -> ChatResponse:
        _, model_name = parse_model_id(request.model_id)
        payload = build_completion_payload(model_name, request)
        started = time.monotonic()
        response = await asyncio.to_thread(
            self._post_json, "/chat/completions", payload
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        return parse_completion(request.model_id, self.provider.id, response, latency_ms)

    async def complete_stream(
        self,
        request: ChatRequest,
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
        on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
    ) -> ChatResponse:
        """The same call with SSE streaming; text arrives through on_token.

        Falls back to a plain call when the provider does not stream — the
        answer is identical, just delivered at the end instead of live.
        """
        _, model_name = parse_model_id(request.model_id)
        payload = build_completion_payload(model_name, request)
        payload["stream"] = True
        started = time.monotonic()
        sent: list[str] = []

        def emit(token: str) -> None:
            sent.append(token)
            on_token(token)

        try:
            meta: dict[str, str] = {}
            text_parts, reasoning_parts, tool_map, usage = await asyncio.to_thread(
                self._read_stream, payload, emit, should_stop, on_tool_delta, meta
            )
        except ProviderStreamError as exc:
            # an error status before the first byte: classify it like any other
            # answer (transient, auth, overflow text, Retry-After) — raised raw,
            # the router could neither retry nor fall back
            self._decode(exc.status, exc.body, exc.retry_after)
            raise
        except Exception:
            if sent:
                # tokens already reached the screen: the retry starts over, so
                # tell the view to drop the partial answer first — otherwise the
                # fallback answer doubles the text
                on_token(STREAM_RESET)
            # a gateway without SSE must not break the turn: answer plainly
            return await self.complete(request)
        latency_ms = int((time.monotonic() - started) * 1000)
        calls = tuple(
            ToolCall(
                id=str(entry.get("id") or new_call_id()),
                name=str(entry.get("function", {}).get("name", "")),
                arguments=_json_dict(entry.get("function", {}).get("arguments")),
            )
            for _, entry in sorted(tool_map.items())
        )
        text = "".join(text_parts)
        if "DSML" in text:
            # a degrading model inlines its calls as markup; the plain path
            # salvages them, the stream must not strand them as prose
            text, salvaged = _salvage_dsml(text)
            calls = (*calls, *salvaged)
        return ChatResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            model_id=request.model_id,
            provider_id=self.provider.id,
            latency_ms=latency_ms,
            reasoning="".join(reasoning_parts),
            # `length` must survive streaming: it is how a call cut by the
            # output cap is kept from running with truncated arguments
            stop_reason=meta.get("stop_reason", ""),
        )

    def _read_stream(
        self,
        payload: dict[str, Any],
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
        on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
        meta: dict[str, str] | None = None,
    ) -> tuple[list[str], list[str], dict[int, dict[str, Any]], Usage]:
        """Drain the SSE stream into text parts, reasoning and tool calls.

        ``meta`` receives the stream's finish reason under ``stop_reason``.

        Anything malformed is skipped rather than fatal: a chunk with a junk
        index or a non-string content must not lose the answer that arrived.
        """
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_map: dict[int, dict[str, Any]] = {}
        prompt_tokens = completion_tokens = cached_tokens = 0
        for raw in self.broker.fetch_stream(
            f"{self.provider.base_url}/chat/completions",
            body=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            timeout=self.timeout,
        ):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage_payload = chunk.get("usage") or {}
            prompt_tokens = usage_payload.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage_payload.get("completion_tokens", completion_tokens)
            cached_tokens = _cached_tokens(usage_payload) or cached_tokens
            for choice in chunk.get("choices") or []:
                if should_stop is not None and should_stop():
                    # the operator stopped: what arrived is the answer
                    return text_parts, reasoning_parts, tool_map, _usage(
                        prompt_tokens, completion_tokens, cached_tokens
                    )
                if not isinstance(choice, dict):
                    continue
                finish = choice.get("finish_reason")
                if isinstance(finish, str) and finish and meta is not None:
                    meta["stop_reason"] = finish
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_parts.append(reasoning)
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    text_parts.append(piece)
                    on_token(piece)
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    try:
                        index = int(call.get("index", 0))
                    except (TypeError, ValueError):
                        index = len(tool_map)
                    entry = tool_map.setdefault(
                        index, {"id": call.get("id", ""), "function": {"name": "", "arguments": ""}}
                    )
                    if call.get("id"):
                        entry["id"] = call["id"]
                    function = call.get("function") or {}
                    if function.get("name"):
                        entry["function"]["name"] = function["name"]
                    if function.get("arguments"):
                        entry["function"]["arguments"] += function["arguments"]
                    if on_tool_delta is not None:
                        on_tool_delta(
                            ToolCallDelta(
                                index=index,
                                id=str(entry.get("id") or ""),
                                name=str(entry["function"].get("name") or ""),
                                arguments=str(entry["function"].get("arguments") or ""),
                            )
                        )
        return text_parts, reasoning_parts, tool_map, _usage(
            prompt_tokens, completion_tokens, cached_tokens
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.provider.default_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str) -> int:
        try:
            response = self.broker.fetch(
                f"{self.provider.base_url}{path}",
                headers=self._headers(),
                timeout=self.timeout,
                max_bytes=1 << 20,
            )
        except ToolExecutionError as exc:
            raise self._unavailable(exc) from exc
        return response.status

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = self.broker.fetch(
                f"{self.provider.base_url}{path}",
                headers=self._headers(),
                timeout=self.timeout,
                max_bytes=4 << 20,
            )
        except ToolExecutionError as exc:
            raise self._unavailable(exc) from exc
        return self._decode(
            response.status, response.body, getattr(response, "retry_after", "")
        )

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.broker.fetch(
                f"{self.provider.base_url}{path}",
                method="POST",
                body=json.dumps(payload).encode("utf-8"),
                headers=self._headers(),
                timeout=self.timeout,
                max_bytes=8 << 20,
            )
        except ToolExecutionError as exc:
            raise self._unavailable(exc) from exc
        return self._decode(
            response.status, response.body, getattr(response, "retry_after", "")
        )

    def _decode(self, status: int, body: bytes, retry_after: str = "") -> dict[str, Any]:
        if status >= 400:
            text = body.decode("utf-8", errors="replace")
            snippet = self.redactor.redact(text[:_MAX_ERROR_SNIPPET]).text
            raise ProviderUnavailableError(
                f"provider {self.provider.id} returned HTTP {status}: {snippet}",
                status=status,
                transient=status == 429 or status >= 500,
                auth=status in (401, 403),
                retry_after=retry_after_seconds(retry_after, text),
            )
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ModelError(
                f"provider {self.provider.id} returned invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ModelError(f"provider {self.provider.id} returned a non-object JSON")
        return decoded

    def _unavailable(self, exc: ToolExecutionError) -> ProviderUnavailableError:
        detail = self.redactor.redact(str(exc)).text
        return ProviderUnavailableError(
            f"provider {self.provider.id} is unavailable: {detail}"
        )


_DSML_CALLS = re.compile(r"<｜DSML｜\s*calls>(.*?)</｜DSML｜\s*calls>", re.S)
_DSML_INVOKE = re.compile(
    r"<｜DSML｜\s*invoke\s+name=\"([^\"]+)\"[^>]*>(.*?)(?:</｜DSML｜\s*invoke>|\Z)",
    re.S,
)
_DSML_PARAM = re.compile(
    r"<｜DSML｜\s*parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)(?:</｜DSML｜\s*parameter>|\Z)",
    re.S,
)


def _salvage_dsml(text: str) -> tuple[str, list[ToolCall]]:
    """Recover tool calls a model wrote as DSML markup into the text.

    Some backends surface DeepSeek-style calls as content instead of native
    tool_calls; a well-formed invoke is still a call, so parse it out and
    strip the markup from the visible text.
    """
    calls: list[ToolCall] = []
    for match in _DSML_INVOKE.finditer(text):
        body = match.group(2)
        arguments = {
            name: value.strip()
            for name, value in _DSML_PARAM.findall(body)
        }
        calls.append(
            ToolCall(
                id=new_call_id(),
                # keep the name as written: chat handlers use shell_exec & co
                name=match.group(1),
                arguments=arguments,
            )
        )
    cleaned = _DSML_CALLS.sub("", text)
    # a stray open tag without its close leaves nothing useful behind
    cleaned = cleaned.split("<｜DSML｜", 1)[0]
    return cleaned.strip(), calls
