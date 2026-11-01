"""Images ride along with a request; the history stays textual."""

from __future__ import annotations

import pytest

from samurai_ai.agents.anthropic import build_messages_payload
from samurai_ai.agents.gemini import build_generate_content_payload
from samurai_ai.agents.ollama import build_chat_payload
from samurai_ai.agents.openai_compat import build_completion_payload
from samurai_ai.agents.provider import ChatRequest, ImagePart, Message

IMAGE = ImagePart(mime="image/png", data="QUJD")


def _request(**kwargs: object) -> ChatRequest:
    return ChatRequest(
        model_id="m",
        messages=(Message(role="user", content="what is this"),),
        images=(IMAGE,),
        **kwargs,  # type: ignore[arg-type]
    )


def test_openai_attaches_a_data_uri_part() -> None:
    payload = build_completion_payload("m", _request())

    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_anthropic_attaches_a_base64_source_block() -> None:
    payload = build_messages_payload("m", _request())

    block = payload["messages"][0]["content"][1]
    assert block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_gemini_attaches_inline_data() -> None:
    payload = build_generate_content_payload(_request())

    part = payload["contents"][0]["parts"][1]
    assert part == {"inline_data": {"mime_type": "image/png", "data": "QUJD"}}


def test_ollama_puts_the_image_on_the_message() -> None:
    payload = build_chat_payload("m", _request())

    assert payload["messages"][0]["images"] == ["QUJD"]


def test_without_images_the_payload_stays_textual() -> None:
    request = ChatRequest(model_id="m", messages=(Message(role="user", content="hi"),))

    assert build_completion_payload("m", request)["messages"][0]["content"] == "hi"
    assert "images" not in build_chat_payload("m", request)["messages"][0]
    assert build_generate_content_payload(request)["contents"][0]["parts"] == [{"text": "hi"}]


def test_images_attach_to_the_last_user_turn() -> None:
    request = ChatRequest(
        model_id="m",
        messages=(
            Message(role="user", content="first"),
            Message(role="assistant", content="ok"),
            Message(role="user", content="second"),
        ),
        images=(IMAGE,),
    )

    payload = build_completion_payload("m", request)

    assert isinstance(payload["messages"][0]["content"], str)
    assert isinstance(payload["messages"][2]["content"], list)


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp", "image/gif"])
def test_every_allowed_mime_round_trips(mime: str) -> None:
    request = ChatRequest(
        model_id="m",
        messages=(Message(role="user", content="x"),),
        images=(ImagePart(mime=mime, data="QUJD"),),
    )

    assert f"data:{mime}" in build_completion_payload("m", request)["messages"][0]["content"][1][
        "image_url"
    ]["url"]
