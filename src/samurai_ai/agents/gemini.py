"""Google Gemini provider client (POST /v1beta/models/{model}:generateContent).

The API key travels in the ``x-goog-api-key`` header — never in the URL —
so it cannot leak into logs or events. System messages map to
``systemInstruction``, content becomes ``contents`` with text parts, tool
specs map to ``functionDeclarations`` and functionCall parts map back to
our ToolCall shape.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from samurai_ai.agents.openai_compat import broker_for_base_url
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

DEFAULT_TIMEOUT = 180.0
_MAX_ERROR_SNIPPET = 200


def _wire_content(message: Message) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        parts: list[dict[str, Any]] = [{"text": message.content}] if message.content else []
        for call in message.tool_calls:
            part: dict[str, Any] = {"functionCall": {"name": call.name, "args": call.arguments}}
            if call.signature:
                part["thoughtSignature"] = call.signature
            parts.append(part)
        return {"role": "model", "parts": parts}
    if message.role == "tool":
        return {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": message.name,
                        "response": {"content": message.content},
                    }
                }
            ],
        }
    role = "model" if message.role == "assistant" else "user"
    return {"role": role, "parts": [{"text": message.content}]}


def _is_response(content: dict[str, Any]) -> bool:
    return any("functionResponse" in part for part in content["parts"])


def _append_merged(contents: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    """All responses of one round go back in one turn, matching the calls."""
    if contents and _is_response(contents[-1]) and _is_response(entry):
        contents[-1]["parts"] += entry["parts"]
        return
    contents.append(entry)


def _attach_images(contents: list[dict[str, Any]], images: tuple[ImagePart, ...]) -> None:
    """Fold images into the last user turn as inline data parts."""
    if contents and _is_response(contents[-1]):
        # a function-response turn carries responses only
        contents.append({"role": "user", "parts": [{"text": IMAGE_CARRIER}]})
    for content in reversed(contents):
        if content["role"] != "user":
            continue
        content["parts"] += [
            {"inline_data": {"mime_type": image.mime, "data": image.data}} for image in images
        ]
        return


def build_generate_content_payload(request: ChatRequest) -> dict[str, Any]:
    system_parts, conversation = split_system(wire_messages(request))
    contents: list[dict[str, Any]] = []
    for message in conversation:
        _append_merged(contents, _wire_content(message))
    if request.images:
        _attach_images(contents, request.images)
    payload: dict[str, Any] = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_parts)}]
        }
    generation_config: dict[str, Any] = {}
    if request.max_tokens is not None:
        generation_config["maxOutputTokens"] = request.max_tokens
    if request.temperature is not None:
        generation_config["temperature"] = request.temperature
    if generation_config:
        payload["generationConfig"] = generation_config
    if request.tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in request.tools
                ]
            }
        ]
    return payload


def parse_generate_content_response(
    model_id: str, provider_id: str, payload: dict[str, Any], latency_ms: int
) -> ChatResponse:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ModelError("gemini response has no candidates")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "text" in part:
            texts.append(str(part.get("text") or ""))
        elif isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            arguments = call.get("args")
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or new_call_id("gemini-")),
                    name=str(call.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                    # Gemini 3 rejects a replayed call that lost its signature
                    signature=str(part.get("thoughtSignature") or ""),
                )
            )
    metadata = payload.get("usageMetadata") or {}
    return ChatResponse(
        text="\n".join(texts),
        tool_calls=tuple(tool_calls),
        usage=Usage(
            prompt_tokens=int(metadata.get("promptTokenCount") or 0),
            completion_tokens=int(metadata.get("candidatesTokenCount") or 0),
            # implicit caching: already included in promptTokenCount
            cached_tokens=int(metadata.get("cachedContentTokenCount") or 0),
        ),
        model_id=model_id,
        provider_id=provider_id,
        latency_ms=latency_ms,
        stop_reason=str(candidates[0].get("finishReason") or ""),
    )


def read_generate_stream(
    lines: Iterable[bytes],
    on_token: Callable[[str], None],
    should_stop: Callable[[], bool] | None = None,
    on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
) -> tuple[str, tuple[ToolCall, ...], Usage, str]:
    """Fold ``streamGenerateContent?alt=sse`` chunks into one answer.

    Each chunk is a partial GenerateContentResponse: text parts arrive in
    pieces, a functionCall arrives whole (with its thoughtSignature), and
    the last chunk carries usage and the finish reason.
    """
    texts: list[str] = []
    calls: list[ToolCall] = []
    usage = Usage()
    finish = ""
    for raw in lines:
        if should_stop is not None and should_stop():
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            chunk = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        metadata = chunk.get("usageMetadata")
        if isinstance(metadata, dict):
            usage = Usage(
                prompt_tokens=int(metadata.get("promptTokenCount") or 0),
                completion_tokens=int(metadata.get("candidatesTokenCount") or 0),
                cached_tokens=int(metadata.get("cachedContentTokenCount") or 0),
            )
        candidates = chunk.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            continue
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        reason = candidate.get("finishReason")
        if isinstance(reason, str) and reason:
            finish = reason
        for part in (candidate.get("content") or {}).get("parts") or []:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                piece = str(part.get("text") or "")
                if piece:
                    texts.append(piece)
                    on_token(piece)
            elif isinstance(part.get("functionCall"), dict):
                call = part["functionCall"]
                arguments = call.get("args")
                tool_call = ToolCall(
                    id=str(call.get("id") or new_call_id("gemini-")),
                    name=str(call.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                    signature=str(part.get("thoughtSignature") or ""),
                )
                if on_tool_delta is not None:
                    on_tool_delta(
                        ToolCallDelta(
                            index=len(calls),
                            id=tool_call.id,
                            name=tool_call.name,
                            arguments=json.dumps(tool_call.arguments, ensure_ascii=False),
                        )
                    )
                calls.append(tool_call)
    return "".join(texts), tuple(calls), usage, finish


class GeminiProvider:
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
            payload = await asyncio.to_thread(self._get_json, "/v1beta/models")
        except ProviderUnavailableError:
            return False
        return bool(payload)

    async def list_models(self) -> list[str]:
        payload = await asyncio.to_thread(self._get_json, "/v1beta/models")
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ModelError(f"{self.provider.id} /models returned no models array")
        names = []
        for entry in models:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"].removeprefix("models/"))
        return sorted(names)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        _, model_name = parse_model_id(request.model_id)
        payload = build_generate_content_payload(request)
        started = time.monotonic()
        response = await asyncio.to_thread(
            self._post_json, f"/v1beta/models/{model_name}:generateContent", payload
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        return parse_generate_content_response(
            request.model_id, self.provider.id, response, latency_ms
        )

    async def complete_stream(
        self,
        request: ChatRequest,
        on_token: Callable[[str], None],
        should_stop: Callable[[], bool] | None = None,
        on_tool_delta: Callable[[ToolCallDelta], None] | None = None,
    ) -> ChatResponse:
        """The same call over SSE; a gateway that does not stream answers plainly."""
        _, model_name = parse_model_id(request.model_id)
        payload = build_generate_content_payload(request)
        started = time.monotonic()
        sent: list[str] = []

        def emit(token: str) -> None:
            sent.append(token)
            on_token(token)

        def read() -> tuple[str, tuple[ToolCall, ...], Usage, str]:
            return read_generate_stream(
                self.broker.fetch_stream(
                    f"{self.provider.base_url}/v1beta/models/{model_name}"
                    ":streamGenerateContent?alt=sse",
                    body=json.dumps(payload).encode("utf-8"),
                    headers=self._headers(),
                    timeout=self.timeout,
                ),
                emit,
                should_stop,
                on_tool_delta,
            )

        try:
            text, calls, usage, finish = await asyncio.to_thread(read)
        except ProviderStreamError as exc:
            self._decode(exc.status, exc.body, exc.retry_after)
            raise
        except Exception:
            if sent:
                on_token(STREAM_RESET)
            return await self.complete(request)
        return ChatResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            model_id=request.model_id,
            provider_id=self.provider.id,
            latency_ms=int((time.monotonic() - started) * 1000),
            stop_reason=finish,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.provider.default_headers}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers

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
