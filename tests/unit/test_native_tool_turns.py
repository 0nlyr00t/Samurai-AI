"""Tool traffic replays as native call/result pairs on every API family."""

from __future__ import annotations

from samurai_ai.agents.anthropic import build_messages_payload
from samurai_ai.agents.gemini import (
    build_generate_content_payload,
    parse_generate_content_response,
)
from samurai_ai.agents.ollama import build_chat_payload
from samurai_ai.agents.openai_compat import build_completion_payload
from samurai_ai.agents.provider import (
    ChatRequest,
    ImagePart,
    Message,
    ToolCall,
    ToolSpec,
    pair_safe_start,
    repair_tool_pairs,
    tool_result,
    wire_messages,
)
from samurai_ai.services.chat import ChatHistoryService

SPEC = (ToolSpec(name="shell_exec", parameters={"type": "object"}),)
CALL_A = ToolCall(id="call_a", name="shell_exec", arguments={"command": "id"})
CALL_B = ToolCall(id="call_b", name="shell_exec", arguments={"command": "pwd"})


def _history() -> tuple[Message, ...]:
    return (
        Message(role="system", content="sys"),
        Message(role="user", content="go"),
        Message(role="assistant", content="checking", tool_calls=(CALL_A, CALL_B)),
        tool_result(CALL_A, "[tool result shell_exec exit=0]\nuid=0"),
        tool_result(CALL_B, "[tool result shell_exec exit=0]\n/root"),
        Message(role="user", content="<reviewer-note>ok</reviewer-note>"),
    )


def _request(messages: tuple[Message, ...], tools: tuple[ToolSpec, ...] = SPEC) -> ChatRequest:
    return ChatRequest(model_id="x/y@v1", messages=messages, tools=tools)


def test_repair_moves_results_behind_calls_and_fills_gaps() -> None:
    note = Message(role="user", content="note mid-round")
    messages = (
        Message(role="assistant", content="", tool_calls=(CALL_A, CALL_B)),
        note,
        tool_result(CALL_B, "b"),
        tool_result(ToolCall(id="ghost", name="shell_exec"), "orphan"),
    )
    repaired = repair_tool_pairs(messages)
    assert [m.role for m in repaired] == ["assistant", "tool", "tool", "user", "user"]
    assert repaired[1].tool_call_id == "call_a"
    assert "no result" in repaired[1].content
    assert repaired[2].content == "b"
    assert repaired[3] == note
    assert repaired[4].content == "orphan"


def test_request_without_tools_flattens_to_text() -> None:
    flat = wire_messages(_request(_history(), tools=()))
    assert all(m.role != "tool" and not m.tool_calls for m in flat)
    assert '[call shell_exec] {"command": "id"}' in flat[2].content
    assert flat[3].role == "user"


def test_pair_safe_start_skips_lone_results() -> None:
    history = _history()
    assert pair_safe_start(history, 3) == 5
    assert pair_safe_start(history, 2) == 2


def test_openai_payload_pairs_calls_and_results() -> None:
    messages = build_completion_payload("m", _request(_history()))["messages"]
    assistant = messages[2]
    assert assistant["tool_calls"][0]["id"] == "call_a"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"command": "id"}'
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call_a",
        "content": "[tool result shell_exec exit=0]\nuid=0",
    }
    assert messages[4]["tool_call_id"] == "call_b"


def test_openai_images_after_a_result_get_their_own_turn() -> None:
    history = _history()[:5]
    request = ChatRequest(
        model_id="x/y@v1",
        messages=history,
        tools=SPEC,
        images=(ImagePart(mime="image/png", data="AAAA"),),
    )
    messages = build_completion_payload("m", request)["messages"]
    assert messages[-2]["role"] == "tool"
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][1]["type"] == "image_url"


def test_anthropic_payload_uses_tool_use_and_merges_results() -> None:
    payload = build_messages_payload("m", _request(_history()))
    messages = payload["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assistant = messages[1]["content"]
    assert assistant[0] == {"type": "text", "text": "checking"}
    assert [b["type"] for b in assistant[1:]] == ["tool_use", "tool_use"]
    results = messages[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    assert results[0]["tool_use_id"] == "call_a"


def test_anthropic_sanitises_foreign_ids_consistently() -> None:
    call = ToolCall(id="fc.1:x", name="shell_exec")
    history = (
        Message(role="user", content="go"),
        Message(role="assistant", content="", tool_calls=(call,)),
        tool_result(call, "done"),
    )
    messages = build_messages_payload("m", _request(history))["messages"]
    assert messages[1]["content"][0]["id"] == "fc_1_x"
    assert messages[2]["content"][0]["tool_use_id"] == "fc_1_x"


def test_anthropic_plain_turns_keep_their_shape() -> None:
    history = (
        Message(role="user", content="a"),
        Message(role="assistant", content="b"),
        Message(role="user", content="c"),
    )
    messages = build_messages_payload("m", _request(history))["messages"]
    assert messages[0] == {"role": "user", "content": "a"}
    # the two newest turns become blocks, to carry the cache breakpoints
    cached = {"type": "ephemeral"}
    assert messages[1]["content"] == [{"type": "text", "text": "b", "cache_control": cached}]
    assert messages[2]["content"] == [{"type": "text", "text": "c", "cache_control": cached}]


def test_only_the_leading_system_prompt_is_lifted() -> None:
    history = (
        Message(role="system", content="core"),
        Message(role="user", content="go"),
        Message(role="system", content="memory: port 22"),
        Message(role="assistant", content="ok"),
    )
    payload = build_messages_payload("m", _request(history))
    assert payload["system"][0]["text"] == "core"
    messages = payload["messages"]
    assert messages[0] == {"role": "user", "content": "go"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0]["text"] == (
        "<system-reminder>\nmemory: port 22\n</system-reminder>"
    )
    contents = build_generate_content_payload(_request(history))
    assert contents["systemInstruction"]["parts"][0]["text"] == "core"
    assert "memory: port 22" in contents["contents"][1]["parts"][0]["text"]


def test_cache_usage_is_reported() -> None:
    from samurai_ai.agents.anthropic import parse_messages_response

    response = parse_messages_response(
        "m",
        "p",
        {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 90,
                "output_tokens": 5,
            },
        },
        0,
    )
    assert response.usage.prompt_tokens == 1000
    assert response.usage.cached_tokens == 900
    gemini = parse_generate_content_response(
        "m",
        "p",
        {
            "candidates": [{"content": {"parts": [{"text": "x"}]}}],
            "usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 60},
        },
        0,
    )
    assert gemini.usage.cached_tokens == 60


def test_gemini_payload_replays_calls_with_signatures() -> None:
    signed = CALL_A.model_copy(update={"signature": "sig-1"})
    history = (
        Message(role="user", content="go"),
        Message(role="assistant", content="", tool_calls=(signed, CALL_B)),
        tool_result(signed, "a"),
        tool_result(CALL_B, "b"),
    )
    contents = build_generate_content_payload(_request(history))["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    model_parts = contents[1]["parts"]
    assert model_parts[0]["thoughtSignature"] == "sig-1"
    assert "thoughtSignature" not in model_parts[1]
    responses = contents[2]["parts"]
    assert [p["functionResponse"]["name"] for p in responses] == ["shell_exec", "shell_exec"]


def test_gemini_parse_keeps_signature_and_unique_ids() -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"functionCall": {"name": "a", "args": {}}, "thoughtSignature": "s"},
                        {"functionCall": {"name": "b", "args": {}}},
                    ]
                }
            }
        ]
    }
    first = parse_generate_content_response("m", "p", payload, 0)
    second = parse_generate_content_response("m", "p", payload, 0)
    assert first.tool_calls[0].signature == "s"
    ids = {call.id for call in (*first.tool_calls, *second.tool_calls)}
    assert len(ids) == 4


def test_ollama_payload_pairs_calls_and_results() -> None:
    messages = build_chat_payload("m", _request(_history()))["messages"]
    assert messages[2]["tool_calls"][0]["function"] == {
        "name": "shell_exec",
        "arguments": {"command": "id"},
    }
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_name"] == "shell_exec"


class _Repo:
    def __init__(self) -> None:
        self.rows: list[tuple[Message, bool]] = []

    def append(self, operation_id: str, message: Message, truncated: bool = False) -> None:
        self.rows.append((message, truncated))


class _Redactor:
    def redact(self, text: str) -> object:
        return type("R", (), {"text": text})()


def test_persisted_transcript_stays_plain_text() -> None:
    repo = _Repo()
    service = ChatHistoryService(repo, _Redactor())  # type: ignore[arg-type]
    for message in _history()[1:5]:
        service.remember("op", message)
    roles = [m.role for m, _ in repo.rows]
    assert roles == ["user", "assistant", "user", "user"]
    assert all(not m.tool_calls for m, _ in repo.rows)
    # bodies of tool results still collapse to their header line
    assert repo.rows[2][0].content == "[tool result shell_exec exit=0]"
    service.remember("op", Message(role="assistant", content="", tool_calls=(CALL_A,)))
    assert len(repo.rows) == 4
