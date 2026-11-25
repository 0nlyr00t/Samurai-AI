import json

from samurai_ai.agents.openai_compat import OpenAICompatProvider
from samurai_ai.agents.provider import ToolCallDelta
from samurai_ai.domain.models import ApiFamily, ProviderDefinition


class _Broker:
    def __init__(self, chunks: list[dict[str, object]]) -> None:
        self.chunks = chunks

    def fetch_stream(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            yield f"data: {json.dumps(chunk)}\n".encode()
        yield b"data: [DONE]\n"


def test_stream_reports_cumulative_raw_tool_arguments() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "shell_exec",
                                    "arguments": '{"command":"ec',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": "ho id\"}"},
                            }
                        ]
                    }
                }
            ]
        },
    ]
    provider = OpenAICompatProvider(
        ProviderDefinition(
            id="test",
            api_family=ApiFamily.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:9/v1",
        ),
        None,
        broker=_Broker(chunks),  # type: ignore[arg-type]
    )
    deltas: list[ToolCallDelta] = []

    _text, _reasoning, calls, _usage = provider._read_stream(
        {"model": "test"}, lambda _token: None, on_tool_delta=deltas.append
    )

    assert [delta.arguments for delta in deltas] == [
        '{"command":"ec',
        '{"command":"echo id"}',
    ]
    assert deltas[-1].id == "call-1"
    assert deltas[-1].name == "shell_exec"
    assert calls[0]["function"]["arguments"] == '{"command":"echo id"}'
