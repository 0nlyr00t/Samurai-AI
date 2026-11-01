from __future__ import annotations

from dataclasses import dataclass

from samurai_ai.storage.database import Database


@dataclass(frozen=True)
class SkillGap:
    skill: str
    workspace: str
    misses: int
    last_seen: str


class SkillGapRepository:
    """Skills the model asked for and did not get.

    A miss is evidence about the harness, not a guess: it is written only when
    the model names a skill that does not exist, so the report is a backlog with
    provenance.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def record(self, skill: str, workspace: str, now: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO skill_gaps(skill, workspace, misses, last_seen)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(skill, workspace) DO UPDATE SET misses = misses + 1,
                    last_seen = excluded.last_seen
                """,
                (skill, workspace, now),
            )

    def list(self, workspace: str | None = None) -> list[SkillGap]:
        query = "SELECT * FROM skill_gaps"
        params: tuple[str, ...] = ()
        if workspace is not None:
            query += " WHERE workspace = ?"
            params = (workspace,)
        query += " ORDER BY misses DESC, skill"
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            SkillGap(
                skill=str(row["skill"]),
                workspace=str(row["workspace"]),
                misses=int(row["misses"]),
                last_seen=str(row["last_seen"]),
            )
            for row in rows
        ]

    def clear(self, skill: str | None = None) -> int:
        with self.database.transaction() as connection:
            if skill is None:
                cursor = connection.execute("DELETE FROM skill_gaps")
            else:
                cursor = connection.execute("DELETE FROM skill_gaps WHERE skill = ?", (skill,))
            return int(cursor.rowcount or 0)
