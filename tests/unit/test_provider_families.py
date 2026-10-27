import asyncio

import pytest

from samurai_ai.agents.anthropic import (
    build_messages_payload,
    parse_messages_response,
)
from samurai_ai.agents.budgets import (
    BudgetExceededError,
    ModelBudget,
    check_budget,
    usage_from_events,
)
from samurai_ai.agents.gemini import (
    build_generate_content_payload,
    parse_generate_content_response,
)
from samurai_ai.agents.provider import ChatRequest, ChatResponse, Message, ToolSpec, Usage
from samurai_ai.agents.router import ModelRouter
from samurai_ai.domain.models import (
    CapabilityError,
    ModelNotFoundError,
    ProviderUnavailableError,
)
from samurai_ai.domain.operations import OperationEvent


def make_request(tools: bool = False) -> ChatRequest:
    return ChatRequest(
        model_id="x/y@v1",
        messages=(
            Message(role="system", content="be terse"),
            Message(role="system", content="be precise"),
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi there"),
            Message(role="user", content="how are you"),
        ),
        tools=(
            ToolSpec(
                name="search",
                description="Search",
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            ),
        )
        if tools
        else (),
        max_tokens=128,
        temperature=0.1,
    )


def test_anthropic_payload_extracts_system() -> None:
    payload = build_messages_payload("claude-sonnet-4-20250514", make_request(tools=True))
    assert payload["model"] == "claude-sonnet-4-20250514"
    assert payload["system"] == "be terse\n\nbe precise"
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert payload["max_tokens"] == 128
    assert payload["temperature"] == 0.1
    assert payload["tools"][0]["input_schema"]["properties"]["q"]["type"] == "string"


def test_anthropic_payload_defaults_max_tokens() -> None:
    request = ChatRequest(model_id="x/y@v1", messages=(Message(role="user", content="hi"),))
    payload = build_messages_payload("claude", request)
    assert payload["max_tokens"] == 4096
    assert "system" not in payload


def test_anthropic_response_mapping_with_tool_use() -> None:
    response = parse_messages_response(
        "anthropic/claude-sonnet-4-20250514@v1",
        "anthropic",
        {
            "content": [
                {"type": "text", "text": "let me search"},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 21, "output_tokens": 9},
        },
        33,
    )
    assert response.text == "let me search"
    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].arguments == {"q": "x"}
    assert response.usage.prompt_tokens == 21
    assert response.usage.completion_tokens == 9
    assert response.provider_id == "anthropic"
    assert response.latency_ms == 33


def test_gemini_payload_system_instruction() -> None:
    payload = build_generate_content_payload(make_request(tools=True))
    assert payload["systemInstruction"]["parts"][0]["text"] == "be terse\n\nbe precise"
    assert [content["role"] for content in payload["contents"]] == [
        "user",
        "model",
        "user",
    ]
    assert payload["generationConfig"]["maxOutputTokens"] == 128
    declarations = payload["tools"][0]["functionDeclarations"]
    assert declarations[0]["name"] == "search"


def test_gemini_response_mapping_with_function_call() -> None:
    response = parse_generate_content_response(
        "gemini/gemini-2.5-pro@v1",
        "gemini",
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "calling search"},
                            {"functionCall": {"name": "search", "args": {"q": "y"}}},
                        ],
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 11},
        },
        21,
    )
    assert response.text == "calling search"
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"q": "y"}
    assert response.tool_calls[0].id.startswith("gemini-")
    assert response.usage.prompt_tokens == 30
    assert response.usage.completion_tokens == 11


def test_ollama_payload_includes_tools() -> None:
    from samurai_ai.agents.ollama import build_chat_payload

    payload = build_chat_payload("qwen3:8b", make_request(tools=True))
    assert payload["tools"][0]["function"]["name"] == "search"
    assert payload["tools"][0]["function"]["parameters"]["properties"]["q"] == {
        "type": "string"
    }


def test_ollama_response_maps_tool_calls() -> None:
    from samurai_ai.agents.ollama import parse_chat_response

    response = parse_chat_response(
        "ollama/qwen3:8b@v1",
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "shell_exec", "arguments": {"command": "id"}}}
                ],
            },
            "prompt_eval_count": 10,
            "eval_count": 4,
        },
        12,
    )
    assert response.tool_calls[0].name == "shell_exec"
    assert response.tool_calls[0].arguments == {"command": "id"}
    assert response.usage.prompt_tokens == 10


def make_response(model_id: str) -> ChatResponse:
    return ChatResponse(
        text=f"ok:{model_id}",
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        model_id=model_id,
        provider_id=model_id.split("/", 1)[0],
    )


def no_sleep(delay: float) -> asyncio.Future:
    future: asyncio.Future = asyncio.Future()
    future.set_result(None)
    return future


def test_router_returns_first_success() -> None:
    router = ModelRouter(sleeper=no_sleep)
    fallbacks: list[tuple[str, str, str]] = []

    async def invoke(model_id: str) -> ChatResponse:
        return make_response(model_id)

    response = asyncio.run(
        router.complete(
            ["a/x@v1", "b/y@v1"],
            invoke,
            lambda f, t, r: fallbacks.append((f, t, r)),
        )
    )
    assert response.text == "ok:a/x@v1"
    assert fallbacks == []


def test_router_fallback_on_transient_after_retries() -> None:
    attempts: list[str] = []

    async def invoke(model_id: str) -> ChatResponse:
        attempts.append(model_id)
        if model_id == "a/x@v1":
            raise ProviderUnavailableError("HTTP 500", status=500, transient=True)
        return make_response(model_id)

    router = ModelRouter(sleeper=no_sleep)
    fallbacks: list[tuple[str, str, str]] = []
    response = asyncio.run(
        router.complete(
            ["a/x@v1", "b/y@v1"],
            invoke,
            lambda f, t, r: fallbacks.append((f, t, r)),
        )
    )
    assert response.text == "ok:b/y@v1"
    assert attempts == ["a/x@v1", "a/x@v1", "a/x@v1", "b/y@v1"]
    assert fallbacks == [("a/x@v1", "b/y@v1", "unavailable")]


def test_router_no_retry_on_auth_and_capability() -> None:
    attempts: list[str] = []

    async def invoke(model_id: str) -> ChatResponse:
        attempts.append(model_id)
        if model_id == "a/x@v1":
            raise ProviderUnavailableError("HTTP 401", status=401, auth=True, transient=False)
        if model_id == "b/y@v1":
            raise CapabilityError("b/y@v1", ("tools",))
        return make_response(model_id)

    router = ModelRouter(sleeper=no_sleep)
    fallbacks: list[tuple[str, str, str]] = []
    response = asyncio.run(
        router.complete(
            ["a/x@v1", "b/y@v1", "c/z@v1"],
            invoke,
            lambda f, t, r: fallbacks.append((f, t, r)),
        )
    )
    assert response.text == "ok:c/z@v1"
    assert attempts == ["a/x@v1", "b/y@v1", "c/z@v1"]
    assert fallbacks == [("a/x@v1", "b/y@v1", "auth"), ("b/y@v1", "c/z@v1", "capability")]


def test_router_non_transient_error_aborts_chain() -> None:
    async def invoke(model_id: str) -> ChatResponse:
        raise ProviderUnavailableError("HTTP 400", status=400, transient=False)

    router = ModelRouter(sleeper=no_sleep)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(router.complete(["a/x@v1", "b/y@v1"], invoke, lambda f, t, r: None))


def test_router_unknown_model_skips_without_retry() -> None:
    attempts: list[str] = []

    async def invoke(model_id: str) -> ChatResponse:
        attempts.append(model_id)
        if model_id == "a/x@v1":
            raise ModelNotFoundError("a/x@v1")
        return make_response(model_id)

    router = ModelRouter(sleeper=no_sleep)
    response = asyncio.run(router.complete(["a/x@v1", "b/y@v1"], invoke, lambda f, t, r: None))
    assert attempts == ["a/x@v1", "b/y@v1"]
    assert response.text == "ok:b/y@v1"


def test_router_raises_last_error_when_all_fail() -> None:
    async def invoke(model_id: str) -> ChatResponse:
        raise ModelNotFoundError(model_id)

    router = ModelRouter(sleeper=no_sleep)
    with pytest.raises(ModelNotFoundError):
        asyncio.run(router.complete(["a/x@v1"], invoke, lambda f, t, r: None))


def make_call_event(
    model_id: str, prompt: int, completion: int, cost: float
) -> OperationEvent:
    return OperationEvent(
        operation_id="op_1",
        event_type="model.call",
        payload={
            "model_id": model_id,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "estimated_cost_usd": cost,
        },
    )


def test_usage_from_events_aggregates() -> None:
    events = [
        make_call_event("a/x@v1", 10, 5, 0.01),
        make_call_event("a/x@v1", 20, 5, 0.02),
        make_call_event("b/y@v1", 30, 10, 0.03),
        OperationEvent(operation_id="op_1", event_type="tool.run.completed", payload={}),
    ]
    summary = usage_from_events(events)
    assert summary.calls == 3
    assert summary.prompt_tokens == 60
    assert summary.completion_tokens == 20
    assert summary.cost_usd == 0.06
    assert summary.by_model["a/x@v1"]["calls"] == 2
    assert summary.by_model["b/y@v1"]["cost_usd"] == 0.03


def test_check_budget_limits() -> None:
    events = [make_call_event("a/x@v1", 100, 50, 0.5)]
    summary = usage_from_events(events)
    check_budget(ModelBudget(), summary)
    check_budget(ModelBudget(max_calls=2, max_tokens_total=200, max_cost_usd=1.0), summary)
    with pytest.raises(BudgetExceededError, match="calls"):
        check_budget(ModelBudget(max_calls=1), summary)
    with pytest.raises(BudgetExceededError, match="tokens"):
        check_budget(ModelBudget(max_tokens_total=150), summary)
    with pytest.raises(BudgetExceededError, match="cost"):
        check_budget(ModelBudget(max_cost_usd=0.5), summary)
