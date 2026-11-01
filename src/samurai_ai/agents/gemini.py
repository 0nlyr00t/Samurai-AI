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
from typing import Any

from samurai_ai.agents.openai_compat import broker_for_base_url
from samurai_ai.agents.provider import (
    ChatRequest,
    ChatResponse,
    ImagePart,
    ToolCall,
    Usage,
)
from samurai_ai.domain.execution import SecretRedactor, ToolExecutionError
from samurai_ai.domain.models import (
    ModelError,
    ProviderDefinition,
    ProviderUnavailableError,
    parse_model_id,
)
from samurai_ai.execution.http import HttpEgressBroker

DEFAULT_TIMEOUT = 60.0
_MAX_ERROR_SNIPPET = 200


def _attach_images(contents: list[dict[str, Any]], images: tuple[ImagePart, ...]) -> None:
    """Fold images into the last user turn as inline data parts."""
    for content in reversed(contents):
        if content["role"] != "user":
            continue
        content["parts"] += [
            {"inline_data": {"mime_type": image.mime, "data": image.data}} for image in images
        ]
        return


def build_generate_content_payload(request: ChatRequest) -> dict[str, Any]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content)
        else:
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})
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
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        if "text" in part:
            texts.append(str(part.get("text") or ""))
        elif isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            arguments = call.get("args")
            tool_calls.append(
                ToolCall(
                    id=f"gemini-{index}",
                    name=str(call.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
    metadata = payload.get("usageMetadata") or {}
    return ChatResponse(
        text="\n".join(texts),
        tool_calls=tuple(tool_calls),
        usage=Usage(
            prompt_tokens=int(metadata.get("promptTokenCount") or 0),
            completion_tokens=int(metadata.get("candidatesTokenCount") or 0),
        ),
        model_id=model_id,
        provider_id=provider_id,
        latency_ms=latency_ms,
    )


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
        return self._decode(response.status, response.body)

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
        return self._decode(response.status, response.body)

    def _decode(self, status: int, body: bytes) -> dict[str, Any]:
        if status >= 400:
            snippet = self.redactor.redact(
                body.decode("utf-8", errors="replace")[:_MAX_ERROR_SNIPPET]
            ).text
            raise ProviderUnavailableError(
                f"provider {self.provider.id} returned HTTP {status}: {snippet}",
                status=status,
                transient=status == 429 or status >= 500,
                auth=status in (401, 403),
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
