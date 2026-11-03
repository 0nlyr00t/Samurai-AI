"""Pending operator questions: durable, so a late answer is still deliverable."""

from __future__ import annotations

from dataclasses import dataclass

from samurai_ai.storage.database import Database


@dataclass(frozen=True)
class QuestionRow:
    id: str
    operation_id: str
    run_id: str
    call_id: str
    payload: str
    status: str
    answer: str


class QuestionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self, question_id: str, operation_id: str, run_id: str, call_id: str, payload: str
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO agent_questions (id, operation_id, run_id, call_id, payload,"
                " asked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (question_id, operation_id, run_id, call_id, payload, _now()),
            )

    def get(self, question_id: str) -> QuestionRow | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, operation_id, run_id, call_id, payload, status, answer"
                " FROM agent_questions WHERE id = ?",
                (question_id,),
            ).fetchone()
        return None if row is None else QuestionRow(**dict(row))

    def by_call(self, call_id: str) -> list[QuestionRow]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, operation_id, run_id, call_id, payload, status, answer"
                " FROM agent_questions WHERE call_id = ?",
                (call_id,),
            ).fetchall()
        return [QuestionRow(**dict(row)) for row in rows]

    def pending(self, operation_id: str) -> list[QuestionRow]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, operation_id, run_id, call_id, payload, status, answer"
                " FROM agent_questions WHERE operation_id = ?"
                " AND status IN ('pending', 'expired')"
                " ORDER BY asked_at",
                (operation_id,),
            ).fetchall()
        return [QuestionRow(**dict(row)) for row in rows]

    def set_status(
        self, question_id: str, status: str, answer: str = "", answered_by: str = ""
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE agent_questions SET status = ?, answer = ?, answered_at = ?,"
                " answered_by = ? WHERE id = ?",
                (status, answer, _now() if status != "pending" else "", answered_by, question_id),
            )


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["QuestionRepository", "QuestionRow"]
