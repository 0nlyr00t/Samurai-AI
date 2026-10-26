from samurai_ai.agents.ollama import build_chat_payload
from samurai_ai.agents.openai_compat import build_completion_payload
from samurai_ai.agents.provider import ChatRequest, Message


def _request(reasoning: str | None) -> ChatRequest:
    return ChatRequest(
        model_id="p/m@v1",
        messages=(Message(role="user", content="hi"),),
        reasoning_effort=reasoning,
    )


def test_openai_payload_reasoning_effort() -> None:
    assert "reasoning_effort" not in build_completion_payload("m", _request(None))
    assert "reasoning_effort" not in build_completion_payload("m", _request("off"))
    payload = build_completion_payload("m", _request("high"))
    assert payload["reasoning_effort"] == "high"


def test_ollama_payload_think_mapping() -> None:
    assert "think" not in build_chat_payload("m", _request(None))
    assert build_chat_payload("m", _request("off"))["think"] is False
    assert build_chat_payload("m", _request("low"))["think"] == "low"
    assert build_chat_payload("m", _request("high"))["think"] == "high"
