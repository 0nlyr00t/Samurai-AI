from __future__ import annotations

from samurai_ai.domain.lessons import Lesson, LessonKind, LessonStatus
from samurai_ai.storage.database import Database


class LessonRepository:
    """Lessons and their evidence.

    Evidence dedup is the database's job (`PRIMARY KEY (lesson_id, source_kind,
    source_id)`), so a re-run of extraction cannot inflate support.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def upsert(self, workspace: str, kind: LessonKind, cue: str, action: str, now: str) -> Lesson:
        """The lesson for this identity, created when missing; status is never reset."""
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO lessons(
                    id, workspace, kind, cue, action, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace, kind, cue) DO UPDATE SET action = excluded.action,
                    updated_at = excluded.updated_at
                """,
                (
                    Lesson(workspace=workspace, kind=kind, cue=cue, action=action).id,
                    workspace,
                    kind.value,
                    cue,
                    action,
                    LessonStatus.CANDIDATE.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM lessons WHERE workspace = ? AND kind = ? AND cue = ?",
                (workspace, kind.value, cue),
            ).fetchone()
        assert row is not None
        return _lesson(row)

    def add_evidence(self, lesson_id: str, source_kind: str, source_id: str, now: str) -> bool:
        """True when this evidence row is new."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO lesson_evidence(lesson_id, source_kind, source_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (lesson_id, source_kind, source_id, now),
            )
            return (cursor.rowcount or 0) > 0

    def support(self, lesson_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM lesson_evidence WHERE lesson_id = ?", (lesson_id,)
            ).fetchone()
        return int(row["total"]) if row else 0

    def evidence(self, lesson_id: str) -> list[tuple[str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT source_kind, source_id FROM lesson_evidence WHERE lesson_id = ? "
                "ORDER BY created_at",
                (lesson_id,),
            ).fetchall()
        return [(str(row["source_kind"]), str(row["source_id"])) for row in rows]

    def get(self, lesson_id: str) -> Lesson | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        return _lesson(row) if row else None

    def list(self, workspace: str | None = None) -> list[Lesson]:
        query = "SELECT * FROM lessons"
        params: tuple[str, ...] = ()
        if workspace is not None:
            query += " WHERE workspace = ?"
            params = (workspace,)
        query += " ORDER BY updated_at DESC, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_lesson(row) for row in rows]

    def set_status(self, lesson_id: str, status: LessonStatus, now: str) -> Lesson | None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE lessons SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, lesson_id),
            )
            if (cursor.rowcount or 0) == 0:
                return None
            row = connection.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        return _lesson(row) if row else None


def _lesson(row: object) -> Lesson:
    data = dict(row)  # type: ignore[call-overload]
    return Lesson(
        id=data["id"],
        workspace=data["workspace"],
        kind=LessonKind(data["kind"]),
        cue=data["cue"],
        action=data["action"],
        status=LessonStatus(data["status"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
