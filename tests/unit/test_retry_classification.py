"""Rate limits say when to come back; streamed errors keep their class."""

from __future__ import annotations

import asyncio

import pytest

from samurai_ai.agents.openai_compat import OpenAICompatProvider
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Message, retry_after_seconds
from samurai_ai.agents.router import ModelRouter
from samurai_ai.domain.models import ApiFamily, ProviderDefinition, ProviderUnavailableError
from samurai_ai.execution.http import ProviderStreamError


def test_retry_after_reads_the_header_then_the_body() -> None:
    assert retry_after_seconds("12") == 12.0
    assert retry_after_seconds("", "Rate limit reached. Please try again in 7.5s.") == 7.5
    assert retry_after_seconds("", "please retry after 300ms") == 0.3
    assert retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT", "") is None
    assert retry_after_seconds("", "bad request") is None


def _ok(model_id: str) -> ChatResponse:
    return ChatResponse(text=f"ok:{model_id}", model_id=model_id, provider_id="p")


def test_router_waits_as_long_as_asked_then_retries() -> None:
    waits: list[float] = []
    calls: list[str] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)

    async def invoke(model_id: str) -> ChatResponse:
        calls.append(model_id)
        if len(calls) == 1:
            raise ProviderUnavailableError("429", status=429, retry_after=4.0)
        return _ok(model_id)

    router = ModelRouter(sleeper=sleeper)
    response = asyncio.run(router.complete(["a/x@v1"], invoke, lambda *_: None))
    assert response.text == "ok:a/x@v1"
    assert waits == [4.0]


def test_router_falls_back_at_once_on_a_quota_wall() -> None:
    waits: list[float] = []
    calls: list[str] = []
    fallbacks: list[tuple[str, str, str]] = []

    async def sleeper(delay: float) -> None:
        waits.append(delay)

    async def invoke(model_id: str) -> ChatResponse:
        calls.append(model_id)
        if model_id == "a/x@v1":
            raise ProviderUnavailableError("429", status=429, retry_after=3600.0)
        return _ok(model_id)

    router = ModelRouter(sleeper=sleeper)
    response = asyncio.run(
        router.complete(["a/x@v1", "b/y@v1"], invoke, lambda *f: fallbacks.append(f))
    )
    assert response.text == "ok:b/y@v1"
    assert calls == ["a/x@v1", "b/y@v1"]
    assert waits == []
    assert fallbacks == [("a/x@v1", "b/y@v1", "unavailable")]


class _FailingStream:
    def __init__(self, status: int, body: bytes, retry_after: str = "") -> None:
        self.error = ProviderStreamError(status, body, retry_after)

    def fetch_stream(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise self.error
        yield b""  # pragma: no cover - makes this a generator


def _stream(broker: object) -> None:
    provider = OpenAICompatProvider(
        ProviderDefinition(
            id="test", api_family=ApiFamily.OPENAI_COMPATIBLE, base_url="http://127.0.0.1:9/v1"
        ),
        None,
        broker=broker,  # type: ignore[arg-type]
    )
    request = ChatRequest(model_id="test/m@v1", messages=(Message(role="user", content="hi"),))
    asyncio.run(provider.complete_stream(request, lambda _t: None))


def test_a_streamed_rate_limit_is_transient_with_its_wait() -> None:
    with pytest.raises(ProviderUnavailableError) as caught:
        _stream(_FailingStream(429, b'{"error":"slow down"}', "9"))
    assert caught.value.status == 429
    assert caught.value.transient
    assert caught.value.retry_after == 9.0


def test_a_streamed_overflow_keeps_its_text_and_is_not_transient() -> None:
    with pytest.raises(ProviderUnavailableError) as caught:
        _stream(_FailingStream(400, b'{"error":{"code":"context_length_exceeded"}}'))
    assert not caught.value.transient
    assert "context_length_exceeded" in str(caught.value)
