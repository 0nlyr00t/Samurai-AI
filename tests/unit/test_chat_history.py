"""The transcript: redacted on the way in, bounded on the way out."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.agents.provider import Message
from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.services.chat import ChatHistoryService
from samurai_ai.storage.chat import ChatHistoryRepository
from samurai_ai.storage.database import Database


@pytest.fixture
def service(tmp_path: Path) -> ChatHistoryService:
    database = Database(tmp_path / "chat.db")
    return ChatHistoryService(ChatHistoryRepository(database), SecretRedactor())


@pytest.fixture
def operation_id(tmp_path: Path) -> str:
    from samurai_ai.domain.operations import OperationMode
    from samurai_ai.domain.scope import ScopeContract
    from samurai_ai.services.operations import OperationService
    from samurai_ai.storage.operations import OperationRepository

    service = OperationService(OperationRepository(Database(tmp_path / "chat.db")))
    operation = service.create("gate", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    return operation.id


def test_messages_come_back_in_order(service: ChatHistoryService, operation_id: str) -> None:
    service.remember(operation_id, Message(role="user", content="first"))
    service.remember(operation_id, Message(role="assistant", content="second"))

    messages, total, truncated = service.tail(operation_id)

    assert [(m.role, m.content) for m in messages] == [("user", "first"), ("assistant", "second")]
    assert total == 2
    assert truncated is False


def test_tool_bodies_collapse_to_the_header(service: ChatHistoryService, operation_id: str) -> None:
    service.remember(
        operation_id,
        Message(role="user", content="[tool result shell_exec exit=0]\nstdout:\n" + "x" * 5000),
    )

    messages, _total, _truncated = service.tail(operation_id)

    assert messages[0].content == "[tool result shell_exec exit=0]"


def test_secrets_are_redacted_on_the_way_in(
    service: ChatHistoryService, operation_id: str
) -> None:
    service.remember(
        operation_id, Message(role="user", content="export TOKEN=ghp_" + "a" * 30)
    )

    messages, _total, _truncated = service.tail(operation_id)

    assert "ghp_" not in messages[0].content
    assert SecretRedactor.PLACEHOLDER in messages[0].content


def test_long_answers_are_capped(service: ChatHistoryService, operation_id: str) -> None:
    from samurai_ai.services.chat import MESSAGE_LIMIT

    service.remember(operation_id, Message(role="assistant", content="y" * (MESSAGE_LIMIT + 500)))

    messages, _total, _truncated = service.tail(operation_id)

    assert messages[0].content.endswith("…")
    assert len(messages[0].content) == MESSAGE_LIMIT + 2


def test_tail_stops_at_the_message_limit(
    service: ChatHistoryService, operation_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("samurai_ai.services.chat.TAIL_MESSAGES", 3)
    for index in range(6):
        service.remember(operation_id, Message(role="user", content=f"m{index}"))

    messages, total, truncated = service.tail(operation_id)

    assert [m.content for m in messages] == ["m3", "m4", "m5"]
    assert total == 6
    assert truncated is True


def test_tail_stops_at_the_char_budget(
    service: ChatHistoryService, operation_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("samurai_ai.services.chat.TAIL_CHARS", 10)
    service.remember(operation_id, Message(role="user", content="a" * 8))
    service.remember(operation_id, Message(role="user", content="b" * 8))

    messages, _total, _truncated = service.tail(operation_id)

    assert [m.content for m in messages] == ["b" * 8]


def test_drop_last_removes_the_newest_rows(
    service: ChatHistoryService, operation_id: str
) -> None:
    for index in range(4):
        service.remember(operation_id, Message(role="user", content=f"m{index}"))

    service.drop_last(operation_id, 2)

    messages, _total, _truncated = service.tail(operation_id)
    assert [m.content for m in messages] == ["m0", "m1"]


def test_clear_wipes_the_operation(service: ChatHistoryService, operation_id: str) -> None:
    service.remember(operation_id, Message(role="user", content="m0"))

    assert service.clear(operation_id) == 1
    assert service.tail(operation_id) == ([], 0, False)


def test_nothing_is_stored_without_an_operation(service: ChatHistoryService) -> None:
    service.remember("", Message(role="user", content="m0"))

    assert service.tail("") == ([], 0, False)
