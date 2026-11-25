"""Streamed answers keep everything a whole answer has: calls, usage, stop reason."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from samurai_ai.agents.anthropic import AnthropicProvider, read_message_stream
from samurai_ai.agents.openai_compat import OpenAICompatProvider
from samurai_ai.agents.provider import ChatRequest, Message, ToolCallDelta
from samurai_ai.domain.models import ApiFamily, ProviderDefinition, ProviderUnavailableError
from samurai_ai.execution.http import ProviderStreamError

REQUEST = ChatRequest(
    model_id="anthropic/claude@v1", messages=(Message(role="user", content="hi"),)
)


def _sse(*events: dict[str, Any]) -> list[bytes]:
    lines: list[bytes] = []
    for event in events:
        lines.append(f"event: {event['type']}\n".encode())
        lines.append(f"data: {json.dumps(event)}\n".encode())
        lines.append(b"\n")
    return lines


def _text(piece: str) -> dict[str, str]:
    return {"type": "text_delta", "text": piece}


ANSWER = _sse(
    {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 12,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 88,
                "output_tokens": 1,
            }
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": _text("Checking ")},
    {"type": "content_block_delta", "index": 0, "delta": _text("the host")},
    {"type": "ping"},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "shell_exec", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"command": "i'},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": 'd"}'},
    },
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
    {"type": "message_stop"},
)


def test_the_stream_folds_into_text_calls_usage_and_stop_reason() -> None:
    tokens: list[str] = []
    deltas: list[ToolCallDelta] = []
    message = read_message_stream(ANSWER, tokens.append, on_tool_delta=deltas.append)
    assert tokens == ["Checking ", "the host"]
    assert message.text == "Checking the host"
    assert [(c.id, c.name, c.arguments) for c in message.tool_calls] == [
        ("toolu_1", "shell_exec", {"command": "id"})
    ]
    # the guard sees the cumulative raw arguments while they are written
    assert [d.arguments for d in deltas] == ['{"command": "i', '{"command": "id"}']
    assert message.usage.prompt_tokens == 1000
    assert message.usage.cached_tokens == 900
    assert message.usage.completion_tokens == 30
    assert message.stop_reason == "tool_use"


def test_a_stop_request_ends_the_read() -> None:
    tokens: list[str] = []
    message = read_message_stream(ANSWER, tokens.append, should_stop=lambda: bool(tokens))
    assert message.text == "Checking "


def test_an_overloaded_event_is_transient() -> None:
    lines = _sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    with pytest.raises(ProviderUnavailableError) as caught:
        read_message_stream(lines, lambda _t: None)
    assert caught.value.transient


class _Broker:
    def __init__(self, lines: list[bytes] | None = None, error: Exception | None = None) -> None:
        self.lines = lines or []
        self.error = error
        self.urls: list[str] = []

    def fetch_stream(self, url: str, **_kwargs: Any) -> Any:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        yield from self.lines


def _anthropic(broker: _Broker) -> AnthropicProvider:
    return AnthropicProvider(
        ProviderDefinition(
            id="anthropic", api_family=ApiFamily.ANTHROPIC, base_url="http://127.0.0.1:9/v1"
        ),
        "k",
        broker=broker,  # type: ignore[arg-type]
    )


def test_anthropic_streams_through_the_messages_endpoint() -> None:
    broker = _Broker(ANSWER)
    tokens: list[str] = []
    response = asyncio.run(_anthropic(broker).complete_stream(REQUEST, tokens.append))
    assert broker.urls == ["http://127.0.0.1:9/v1/messages"]
    assert tokens == ["Checking ", "the host"]
    assert response.tool_calls[0].arguments == {"command": "id"}
    assert response.stop_reason == "tool_use"
    assert response.usage.cached_tokens == 900


def test_anthropic_stream_error_status_is_classified() -> None:
    broker = _Broker(error=ProviderStreamError(429, b'{"error":"rate"}', "6"))
    with pytest.raises(ProviderUnavailableError) as caught:
        asyncio.run(_anthropic(broker).complete_stream(REQUEST, lambda _t: None))
    assert caught.value.transient and caught.value.retry_after == 6.0


class _OpenAIBroker:
    def fetch_stream(self, *_args: Any, **_kwargs: Any) -> Any:
        chunk = {"choices": [{"delta": {"content": "par"}, "finish_reason": None}]}
        yield f"data: {json.dumps(chunk)}\n".encode()
        last = {"choices": [{"delta": {}, "finish_reason": "length"}]}
        yield f"data: {json.dumps(last)}\n".encode()
        yield b"data: [DONE]\n"


def test_openai_stream_keeps_the_finish_reason() -> None:
    provider = OpenAICompatProvider(
        ProviderDefinition(
            id="gpt", api_family=ApiFamily.OPENAI_COMPATIBLE, base_url="http://127.0.0.1:9/v1"
        ),
        None,
        broker=_OpenAIBroker(),  # type: ignore[arg-type]
    )
    response = asyncio.run(provider.complete_stream(REQUEST, lambda _t: None))
    assert response.text == "par"
    assert response.stop_reason == "length"
