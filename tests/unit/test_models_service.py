"""Model service settings: the request timeout."""

from __future__ import annotations

from pathlib import Path

from samurai_ai.cli import _make_services


def test_the_model_timeout_comes_from_settings_and_is_bounded(tmp_path: Path) -> None:
    """60 s ended real runs on slow gateways; the operator can raise it."""
    services = _make_services(tmp_path / "t.db")
    models = services[7]
    assert models.request_timeout() == 180.0
    assert models.settings is not None
    models.settings.set_setting("model.timeout_seconds", "420")
    assert models.request_timeout() == 420.0
    models.settings.set_setting("model.timeout_seconds", "99999")
    assert models.request_timeout() == 600.0
    models.settings.set_setting("model.timeout_seconds", "2")
    assert models.request_timeout() == 10.0


def test_a_handwritten_prlctl_command_is_requoted(tmp_path: Path) -> None:
    """`prlctl exec <uuid> curl ...` typed by the model must not lose its tail."""
    from samurai_ai.tui.chat import ChatController

    # the helper is a pure function of the command; call it unbound
    normalize = ChatController._normalize_prlctl
    out = normalize(None, "prlctl exec {1381d6d4-31a6-4aee-ab87-4193b12e98ae} curl -s http://10.0.0.5/")
    assert out.startswith("prlctl exec ")
    assert "curl" in out and "http://10.0.0.5/" in out
    # already-quoted or unrelated commands pass through unchanged
    plain = normalize(None, "ls -la /tmp")
    assert plain == "ls -la /tmp"


def test_streaming_completion_arrives_token_by_token(tmp_path: Path) -> None:
    """The SSE path yields text as it lands and still returns a full response."""
    import asyncio

    from samurai_ai.agents.provider import ChatRequest, Message

    # a fake broker that drips SSE lines
    class _StreamBroker:
        def fetch_stream(self, url, *, method="POST", body=None, headers=None, timeout=60.0):
            lines = [
                b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"shell_exec","arguments":"{\\"command\\":\\"echo hi\\"}"}}]}}]}\n',
                b'data: [DONE]\n',
            ]
            yield from lines

    from samurai_ai.agents.openai_compat import OpenAICompatProvider
    from samurai_ai.domain.models import ModelDefinition, ProviderDefinition

    provider_def = ProviderDefinition(
        id="fake", name="fake", base_url="http://fake", api_family="openai-compatible"
    )
    provider = OpenAICompatProvider(provider_def, None, broker=_StreamBroker())  # type: ignore[arg-type]

    seen: list[str] = []
    request = ChatRequest(
        model_id="fake/m@v1",
        messages=(Message(role="user", content="hi"),),
        tools=(),
    )
    response = asyncio.run(provider.complete_stream(request, seen.append))
    assert seen == ["Hel", "lo"]
    assert response.text == "Hello"
    assert response.tool_calls[0].arguments["command"] == "echo hi"


def test_streaming_falls_back_to_a_plain_call_without_sse(tmp_path: Path) -> None:
    """A gateway that rejects stream=true must still answer."""
    import asyncio

    from samurai_ai.agents.provider import ChatRequest, ChatResponse, Message, Usage
    from samurai_ai.agents.openai_compat import OpenAICompatProvider
    from samurai_ai.domain.models import ProviderDefinition

    class _NoStreamBroker:
        def fetch(self, url, **kwargs):
            from samurai_ai.execution.http import HttpResponse
            return HttpResponse(
                url=url, status=200, reason="OK",
                body=b'{"choices":[{"message":{"role":"assistant","content":"plain"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}',
                truncated=False, redirects=(),
            )
        def fetch_stream(self, url, **kwargs):
            from samurai_ai.domain.execution import ToolExecutionError
            raise ToolExecutionError("no stream")

    provider_def = ProviderDefinition(
        id="fake", name="fake", base_url="http://fake", api_family="openai-compatible"
    )
    provider = OpenAICompatProvider(provider_def, None, broker=_NoStreamBroker())  # type: ignore[arg-type]
    response = asyncio.run(provider.complete_stream(
        ChatRequest(model_id="fake/m@v1", messages=(Message(role="user", content="hi"),), tools=()),
        lambda _t: None,
    ))
    assert response.text == "plain"
