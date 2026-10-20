"""Ollama local provider: health, model discovery and /api/chat.

First-class local data route: models are discovered from GET /api/tags
(never static), capabilities are assigned conservatively by model-name
heuristics (tool calling only for known tool-capable families), and every
ModelDefinition carries local=True. An unreachable daemon degrades to a
clean ProviderUnavailableError.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from samurai_ai.agents.openai_compat import broker_for_base_url
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Usage
from samurai_ai.domain.execution import ToolExecutionError
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ModelError,
    ModelNotFoundError,
    ProviderDefinition,
    ProviderUnavailableError,
    parse_model_id,
)
from samurai_ai.execution.http import HttpEgressBroker

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 120.0

_TOOL_CAPABLE_FAMILIES: tuple[str, ...] = (
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "qwen2.5",
    "qwen3",
    "mistral",
    "mixtral",
    "command-r",
    "granite",
    "firefunction",
    "nemotron",
)


def infer_capabilities(model_name: str) -> frozenset[ModelCapability]:
    lowered = model_name.lower()
    if any(family in lowered for family in _TOOL_CAPABLE_FAMILIES):
        return frozenset({ModelCapability.TOOLS, ModelCapability.STREAMING})
    return frozenset({ModelCapability.STREAMING})


def model_definition_for(model_name: str) -> ModelDefinition:
    return ModelDefinition(
        id=f"ollama/{model_name}@v1",
        provider_id="ollama",
        display_name=model_name,
        api_family=ApiFamily.OLLAMA,
        context_limit=8192,
        max_output_tokens=4096,
        capabilities=infer_capabilities(model_name),
        local=True,
    )


def build_chat_payload(model_name: str, request: ChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_name,
        "stream": False,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
    }
    options: dict[str, Any] = {}
    if request.max_tokens is not None:
        options["num_predict"] = request.max_tokens
    if request.temperature is not None:
        options["temperature"] = request.temperature
    if options:
        payload["options"] = options
    return payload


def parse_chat_response(
    model_id: str, payload: dict[str, Any], latency_ms: int
) -> ChatResponse:
    message = payload.get("message") or {}
    return ChatResponse(
        text=str(message.get("content") or ""),
        tool_calls=(),
        usage=Usage(
            prompt_tokens=int(payload.get("prompt_eval_count") or 0),
            completion_tokens=int(payload.get("eval_count") or 0),
        ),
        model_id=model_id,
        provider_id="ollama",
        latency_ms=latency_ms,
    )


class OllamaProvider:
    def __init__(
        self,
        provider: ProviderDefinition,
        broker: HttpEgressBroker | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.provider = provider
        self.broker = broker or broker_for_base_url(provider.base_url)
        self.timeout = timeout

    async def health(self) -> bool:
        try:
            response = await asyncio.to_thread(self._fetch, "/api/tags", None)
        except ProviderUnavailableError:
            return False
        return bool(response.status == 200)

    async def list_models(self) -> list[ModelDefinition]:
        response = await asyncio.to_thread(self._fetch, "/api/tags", None)
        payload = self._decode(response.status, response.body)
        models = payload.get("models")
        if not isinstance(models, list):
            raise ModelError("ollama /api/tags returned no models array")
        names = sorted(
            str(entry.get("name")) for entry in models if isinstance(entry, dict)
        )
        return [model_definition_for(name) for name in names]

    async def resolve_model(self, model_id: str) -> ModelDefinition:
        _, model_name = parse_model_id(model_id)
        for model in await self.list_models():
            if model.model_name == model_name:
                return model
        raise ModelNotFoundError(
            f"ollama model not found locally: {model_name!r}; pull it with "
            f"'ollama pull {model_name}'"
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        _, model_name = parse_model_id(request.model_id)
        payload = build_chat_payload(model_name, request)
        started = time.monotonic()
        response = await asyncio.to_thread(
            self._fetch, "/api/chat", json.dumps(payload).encode("utf-8")
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        decoded = self._decode(response.status, response.body)
        return parse_chat_response(request.model_id, decoded, latency_ms)

    def _fetch(self, path: str, body: bytes | None) -> Any:
        try:
            if body is None:
                return self.broker.fetch(
                    f"{self.provider.base_url}{path}", timeout=self.timeout, max_bytes=4 << 20
                )
            return self.broker.fetch(
                f"{self.provider.base_url}{path}",
                method="POST",
                body=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
                max_bytes=8 << 20,
            )
        except ToolExecutionError as exc:
            raise ProviderUnavailableError(
                f"ollama is unavailable at {self.provider.base_url}: {exc}; "
                "start it with 'ollama serve' for the local data route"
            ) from exc

    @staticmethod
    def _decode(status: int, body: bytes) -> dict[str, Any]:
        if status >= 400:
            snippet = body.decode("utf-8", errors="replace")[:200]
            raise ProviderUnavailableError(f"ollama returned HTTP {status}: {snippet}")
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ModelError("ollama returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ModelError("ollama returned a non-object JSON")
        return decoded
