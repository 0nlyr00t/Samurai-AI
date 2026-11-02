from __future__ import annotations

from samurai_ai.domain.teamwork import Member, NoteTarget, OperationLevel, OperationNote
from samurai_ai.storage.database import Database


class TeamworkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    # --- members ----------------------------------------------------------
    def add_member(self, member: Member) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operation_members(
                    operation_id, subject, label, level, added_by, added_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id, subject) DO UPDATE SET level = excluded.level
                """,
                (
                    member.operation_id,
                    member.subject,
                    member.label,
                    member.level.value,
                    member.added_by,
                    member.added_at.isoformat(),
                ),
            )
            return (cursor.rowcount or 0) > 0

    def get_member(self, operation_id: str, subject: str) -> Member | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_members WHERE operation_id = ? AND subject = ?",
                (operation_id, subject),
            ).fetchone()
        return _member(row) if row else None

    def list_members(self, operation_id: str) -> list[Member]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_members WHERE operation_id = ? ORDER BY added_at",
                (operation_id,),
            ).fetchall()
        return [_member(row) for row in rows]

    def remove_member(self, operation_id: str, subject: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM operation_members WHERE operation_id = ? AND subject = ?",
                (operation_id, subject),
            )
            return (cursor.rowcount or 0) > 0

    def set_owner(self, operation_id: str, subject: str, label: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE operation_members SET level = 'member' "
                "WHERE operation_id = ? AND level = 'owner'",
                (operation_id,),
            )
            connection.execute(
                """
                INSERT INTO operation_members(
                    operation_id, subject, label, level, added_by, added_at
                ) VALUES (?, ?, ?, 'owner', ?, ?)
                ON CONFLICT(operation_id, subject) DO UPDATE SET level = 'owner'
                """,
                (operation_id, subject, label, subject, _now()),
            )

    # --- notes ------------------------------------------------------------
    def add_note(self, note: OperationNote) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operation_notes(
                    id, operation_id, target_kind, target_id, body, author, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.id,
                    note.operation_id,
                    note.target_kind.value,
                    note.target_id,
                    note.body,
                    note.author,
                    note.created_at.isoformat(),
                ),
            )

    def list_notes(self, operation_id: str, limit: int) -> list[OperationNote]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM operation_notes WHERE operation_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (operation_id, limit),
            ).fetchall()
        return [_note(row) for row in reversed(rows)]


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _member(row: object) -> Member:
    data = dict(row)  # type: ignore[call-overload]
    return Member(
        operation_id=data["operation_id"],
        subject=data["subject"],
        label=data["label"],
        level=OperationLevel(data["level"]),
        added_by=data["added_by"],
        added_at=data["added_at"],
    )


def _note(row: object) -> OperationNote:
    data = dict(row)  # type: ignore[call-overload]
    return OperationNote(
        id=data["id"],
        operation_id=data["operation_id"],
        target_kind=NoteTarget(data["target_kind"]),
        target_id=data["target_id"],
        body=data["body"],
        author=data["author"],
        created_at=data["created_at"],
    )
