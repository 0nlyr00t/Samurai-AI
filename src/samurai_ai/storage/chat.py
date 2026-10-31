from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from samurai_ai.agents.provider import Message, MessageRole
from samurai_ai.storage.database import Database


@dataclass(frozen=True)
class StoredMessage:
    """A row of the transcript as it was persisted."""

    id: int
    message: Message
    truncated: bool


class ChatHistoryRepository:
    """The operator's conversation, operation-scoped and append-only.

    Unlike evidence, the transcript is mutable at its tail: `/undo` drops the
    last turn and `/clear` wipes the operation, because the operator is editing
    their own context, not a record.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def append(self, operation_id: str, message: Message, truncated: bool = False) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_messages(operation_id, role, content, truncated, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (operation_id, message.role, message.content, int(truncated)),
            )

    def tail(self, operation_id: str, limit: int, char_budget: int) -> list[StoredMessage]:
        """The newest ``limit`` messages, stopping early at ``char_budget`` characters."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, truncated
                FROM chat_messages
                WHERE operation_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (operation_id, limit),
            ).fetchall()
        kept: list[StoredMessage] = []
        spent = 0
        for row in rows:
            spent += len(str(row["content"]))
            if kept and spent > char_budget:
                break
            kept.append(
                StoredMessage(
                    id=int(row["id"]),
                    message=Message(
                        role=cast(MessageRole, row["role"]), content=str(row["content"])
                    ),
                    truncated=bool(row["truncated"]),
                )
            )
        kept.reverse()
        return kept

    def count(self, operation_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM chat_messages WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def delete_last(self, operation_id: str, count: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM chat_messages
                WHERE operation_id = ?
                  AND id IN (
                      SELECT id FROM chat_messages
                      WHERE operation_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (operation_id, operation_id, count),
            )

    def clear(self, operation_id: str) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM chat_messages WHERE operation_id = ?", (operation_id,)
            )
            return int(cursor.rowcount or 0)
