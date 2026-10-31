"""The conversation, remembered across restarts.

Kept deliberately boring: the transcript is a row per message, redacted on the
way in, and restored as a bounded tail. Tool traffic is the part that would
otherwise swamp both the database and the model's window, so those messages
keep their header line and lose the bodies.
"""

from __future__ import annotations

from samurai_ai.agents.provider import Message
from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.storage.chat import ChatHistoryRepository, StoredMessage

#: A restored turn costs context; anything past this is left in the database.
TAIL_MESSAGES = 40
TAIL_CHARS = 30_000
#: Tool messages keep their `[tool result …]` header and this much of nothing else.
TOOL_HEADER_ONLY = True
#: Hard caps so one runaway answer cannot own the whole budget.
MESSAGE_LIMIT = 8_000

MARKER = "[restored] the operator reopened this operation; the turns below are its earlier history"


def marker() -> Message:
    """A user-role note telling the model the turns below came from disk."""
    return Message(role="user", content=MARKER)


class ChatHistoryService:
    def __init__(self, repository: ChatHistoryRepository, redactor: SecretRedactor) -> None:
        self.repository = repository
        self.redactor = redactor

    def remember(self, operation_id: str, message: Message) -> None:
        """Persist one message, redacted; tool bodies collapse to their header."""
        if not operation_id:
            return
        content = self.redactor.redact(message.content).text
        truncated = False
        if _is_tool(message) and TOOL_HEADER_ONLY:
            header = content.split("\n", 1)[0]
            truncated = header != content
            content = header
        elif len(content) > MESSAGE_LIMIT:
            content = content[:MESSAGE_LIMIT] + "\n…"
            truncated = True
        self.repository.append(operation_id, Message(role=message.role, content=content), truncated)

    def tail(self, operation_id: str) -> tuple[list[Message], int, bool]:
        """Restored messages, the total stored, and whether older turns were left out."""
        if not operation_id:
            return [], 0, False
        stored: list[StoredMessage] = self.repository.tail(operation_id, TAIL_MESSAGES, TAIL_CHARS)
        if not stored:
            return [], 0, False
        total = self.repository.count(operation_id)
        return [entry.message for entry in stored], total, total > len(stored)

    def drop_last(self, operation_id: str, count: int) -> None:
        """Remove the newest ``count`` rows — the other half of `/undo`."""
        if operation_id and count > 0:
            self.repository.delete_last(operation_id, count)

    def clear(self, operation_id: str) -> int:
        return self.repository.clear(operation_id) if operation_id else 0


def _is_tool(message: Message) -> bool:
    return str(message.content).startswith("[tool ")
