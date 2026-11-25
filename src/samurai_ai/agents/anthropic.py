"""Anthropic provider client (POST /v1/messages, anthropic-version 2023-06-01).

System messages are lifted into the top-level ``system`` field; tool specs
map to ``tools`` with ``input_schema`` and tool_use content blocks map back
to our ToolCall shape. The API key travels in the ``x-api-key`` header and
is scrubbed from error text.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from samurai_ai.agents.openai_compat import broker_for_base_url
from samurai_ai.agents.provider import (
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
    split_system,
    wire_messages,
)
from samurai_ai.domain.execution import SecretRedactor, ToolExecutionError
from samurai_ai.domain.models import (
    ModelError,
    ProviderDefinition,
    ProviderUnavailableError,
    parse_model_id,
)
from samurai_ai.execution.http import HttpEgressBroker, ProviderStreamError

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 180.0
_MAX_ERROR_SNIPPET = 200


_TOOL_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _tool_id(raw: str) -> str:
    """Anthropic accepts only ``[A-Za-z0-9_-]`` ids; foreign ids are mapped."""
    return _TOOL_ID_UNSAFE.sub("_", raw) or "call"


def _blocks(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content}] if content else []


def _wire_message(message: Message) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        blocks = _blocks(message.content)
        blocks += [
            {
                "type": "tool_use",
                "id": _tool_id(call.id),
                "name": call.name,
                "input": call.arguments,
            }
            for call in message.tool_calls
        ]
        return {"role": "assistant", "content": blocks}
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": _tool_id(message.tool_call_id),
                    "content": message.content or "(empty)",
                }
            ],
        }
    return {"role": message.role, "content": message.content}


def _append_merged(messages: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """Results of one round share a single user turn, as the API requires.

    Only turns carrying blocks merge; plain text turns keep their old shape.
    """
    if messages:
        last = messages[-1]
        if last["role"] == entry["role"] and (
            isinstance(last["content"], list) or isinstance(entry["content"], list)
        ):
            last["content"] = _blocks(last["content"]) + _blocks(entry["content"])
            return
    messages.append(entry)


def _attach_images(messages: list[dict[str, Any]], images: tuple[ImagePart, ...]) -> None:
    """Fold images into the last user turn as base64 source blocks."""
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        blocks: list[dict[str, Any]] = _blocks(message["content"])
        blocks += [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image.mime, "data": image.data},
            }
            for image in images
        ]
        message["content"] = blocks
        return


#: a prompt-cache breakpoint; Anthropic caches nothing without one
_CACHE = {"type": "ephemeral"}


def _mark_cached(message: dict[str, Any]) -> None:
    blocks = _blocks(message["content"])
    if not blocks:
        return
    blocks[-1] = {**blocks[-1], "cache_control": _CACHE}
    message["content"] = blocks


def build_messages_payload(model_name: str, request: ChatRequest) -> dict[str, Any]:
    """The request, with cache breakpoints on tools, system and the newest turn.

    Tools and the system prompt are the stable prefix; breakpoints on the last
    two messages let the next round read everything before them from cache, so
    a tool loop pays full price only for the turns it just added. That is four
    breakpoints, the API's maximum.
    """
    system_parts, conversation = split_system(wire_messages(request))
    messages: list[dict[str, Any]] = []
    for message in conversation:
        _append_merged(messages, _wire_message(message))
    if request.images:
        _attach_images(messages, request.images)
    # the newest turn and the one before it: when the newest is per-request
    # context (an operation snapshot), the next round still finds the history
    # up to the previous breakpoint in cache
    for entry in messages[-2:]:
        _mark_cached(entry)
    payload: dict[str, Any] = {
        "model": model_name,
        "max_tokens": request.max_tokens or 4096,
        "messages": messages,
    }
    if system_parts:
        payload["system"] = [
            {"type": "text", "text": "\n\n".join(system_parts), "cache_control": _CACHE}
        ]
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.tools:
        tools: list[dict[str, Any]] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in request.tools
        ]
        tools[-1]["cache_control"] = _CACHE
        payload["tools"] = tools
    return payload


def parse_messages_response(
    model_id: str, provider_id: str, payload: dict[str, Any], latency_ms: int
) -> ChatResponse:
    content = payload.get("content")
    if not isinstance(content, list):
        raise ModelError("anthropic response has no content array")
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            arguments = block.get("input")
            tool_calls.append(
                ToolCall(
                    id=str(block.get("id") or new_call_id()),
                    name=str(block.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
    usage = payload.get("usage") or {}
    return ChatResponse(
        text="\n".join(texts),
        tool_calls=tuple(tool_calls),
        usage=Usage(
            # input_tokens counts only the uncached tail; the window holds all three
            prompt_tokens=int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        ),
        model_id=model_id,
        provider_id=provider_id,
        latency_ms=latency_ms,
        stop_reason=str(payload.get("stop_reason") or ""),
    )


@dataclass
class _Block:
    kind: str
    id: str = ""
    name: str = ""
    text: str = ""


@dataclass
class StreamedMessage:
    """What one SSE stream of /v1/messages added up to."""

    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""


def read_message_stream(
    lines: Iterable[bytes],
    on_token: Callable[[str], None],
    should_stop: Callable[[], bool] | None = None,
    on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
) -> StreamedMessage:
    """Fold the Messages SSE events into one answer, emitting text as it lands.

    Tool input arrives as JSON fragments; each fragment is reported as the
    cumulative raw arguments (what the stream guard matches against). A stop
    request ends the read: what arrived is the answer. An ``error`` event is
    raised, transient when Anthropic says it is overloaded.
    """
    blocks: dict[int, _Block] = {}
    texts: list[str] = []
    reasoning: list[str] = []
    prompt = cache_read = cache_write = output = 0
    stop_reason = ""
    for raw in lines:
        if should_stop is not None and should_stop():
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            prompt = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            output = int(usage.get("output_tokens") or 0)
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            index = int(event.get("index") or 0)
            blocks[index] = _Block(
                kind=str(block.get("type") or ""),
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
            )
        elif kind == "content_block_delta":
            index = int(event.get("index") or 0)
            entry = blocks.setdefault(index, _Block(kind="text"))
            delta = event.get("delta") or {}
            delta_kind = delta.get("type")
            if delta_kind == "text_delta":
                piece = str(delta.get("text") or "")
                if piece:
                    texts.append(piece)
                    on_token(piece)
            elif delta_kind == "thinking_delta":
                reasoning.append(str(delta.get("thinking") or ""))
            elif delta_kind == "input_json_delta":
                entry.text += str(delta.get("partial_json") or "")
                if on_tool_delta is not None:
                    on_tool_delta(
                        ToolCallDelta(
                            index=index, id=entry.id, name=entry.name, arguments=entry.text
                        )
                    )
        elif kind == "message_delta":
            reason = (event.get("delta") or {}).get("stop_reason")
            if isinstance(reason, str) and reason:
                stop_reason = reason
            usage = event.get("usage") or {}
            output = int(usage.get("output_tokens") or output)
        elif kind == "error":
            error = event.get("error") or {}
            error_type = str(error.get("type") or "error")
            raise ProviderUnavailableError(
                f"anthropic stream error {error_type}: {str(error.get('message') or '')[:200]}",
                transient=error_type in ("overloaded_error", "api_error"),
            )
        elif kind == "message_stop":
            break
    calls: list[ToolCall] = []
    for _index, block in sorted(blocks.items()):
        if block.kind != "tool_use":
            continue
        try:
            arguments = json.loads(block.text) if block.text.strip() else {}
        except json.JSONDecodeError:
            arguments = {}
        calls.append(
            ToolCall(
                id=block.id or new_call_id(),
                name=block.name,
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return StreamedMessage(
        text="".join(texts),
        reasoning="".join(reasoning),
        tool_calls=tuple(calls),
        usage=Usage(
            prompt_tokens=prompt + cache_read + cache_write,
            completion_tokens=output,
            cached_tokens=cache_read,
        ),
        stop_reason=stop_reason,
    )


class AnthropicProvider:
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
            response = await asyncio.to_thread(self._post_probe)
        except ProviderUnavailableError:
            return False
        return response < 500

    async def list_models(self) -> list[str]:
        payload = await asyncio.to_thread(self._get_json, "/models")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ModelError(f"{self.provider.id} /models returned no data array")
        return sorted(str(entry.get("id")) for entry in data if isinstance(entry, dict))

    async def complete(self, request: ChatRequest) -> ChatResponse:
        _, model_name = parse_model_id(request.model_id)
        payload = build_messages_payload(model_name, request)
        started = time.monotonic()
        response = await asyncio.to_thread(self._post_json, "/messages", payload)
        latency_ms = int((time.monotonic() - started) * 1000)
        return parse_messages_response(
            request.model_id, self.provider.id, response, latency_ms
        )

    async def complete_stream(
        self,
        request: ChatRequest,
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
        on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
    ) -> ChatResponse:
        """The same call over SSE: text reaches the screen as it is written.

        Streaming also lets the stream guard see a tool call while it is being
        written, which a whole-answer call cannot. A gateway that does not
        stream gets the plain call instead; an error status is classified like
        any other answer.
        """
        _, model_name = parse_model_id(request.model_id)
        payload = build_messages_payload(model_name, request)
        payload["stream"] = True
        started = time.monotonic()
        sent: list[str] = []

        def emit(token: str) -> None:
            sent.append(token)
            on_token(token)

        def read() -> StreamedMessage:
            return read_message_stream(
                self.broker.fetch_stream(
                    f"{self.provider.base_url}/messages",
                    body=json.dumps(payload).encode("utf-8"),
                    headers=self._headers(),
                    timeout=self.timeout,
                ),
                emit,
                should_stop,
                on_tool_delta,
            )

        try:
            streamed = await asyncio.to_thread(read)
        except ProviderStreamError as exc:
            self._decode(exc.status, exc.body, exc.retry_after)
            raise
        except ProviderUnavailableError:
            raise
        except Exception:
            if sent:
                on_token(STREAM_RESET)
            return await self.complete(request)
        return ChatResponse(
            text=streamed.text,
            tool_calls=streamed.tool_calls,
            usage=streamed.usage,
            model_id=request.model_id,
            provider_id=self.provider.id,
            latency_ms=int((time.monotonic() - started) * 1000),
            reasoning=streamed.reasoning,
            stop_reason=streamed.stop_reason,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            **self.provider.default_headers,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _post_probe(self) -> int:
        try:
            response = self.broker.fetch(
                f"{self.provider.base_url}/messages",
                method="POST",
                body=json.dumps(
                    {
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    }
                ).encode("utf-8"),
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
