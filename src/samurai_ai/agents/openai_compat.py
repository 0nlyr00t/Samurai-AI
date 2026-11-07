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
from typing import Any

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
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.http import HttpEgressBroker

DEFAULT_TIMEOUT = 180.0
_MAX_ERROR_SNIPPET = 200


def broker_for_base_url(base_url: str, verify_tls: bool = True) -> HttpEgressBroker:
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname
    if not host:
        raise ModelError(f"provider base_url must contain a host: {base_url!r}")
    return HttpEgressBroker(ScopeContract(includes=(host,)), verify_tls=verify_tls)


def _attach_images(messages: list[dict[str, Any]], images: tuple[ImagePart, ...]) -> None:
    """Fold images into the last user turn as content parts."""
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
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
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
                id=str(entry.get("id") or ""),
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
        ),
        model_id=model_id,
        provider_id=provider_id,
        latency_ms=latency_ms,
    )


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
    for index, match in enumerate(_DSML_INVOKE.finditer(text)):
        body = match.group(2)
        arguments = {
            name: value.strip()
            for name, value in _DSML_PARAM.findall(body)
        }
        calls.append(
            ToolCall(
                id=f"dsml-{index}",
                # keep the name as written: chat handlers use shell_exec & co
                name=match.group(1),
                arguments=arguments,
            )
        )
    cleaned = _DSML_CALLS.sub("", text)
    # a stray open tag without its close leaves nothing useful behind
    cleaned = cleaned.split("<｜DSML｜", 1)[0]
    return cleaned.strip(), calls
