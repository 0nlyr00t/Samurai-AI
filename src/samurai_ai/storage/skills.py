from __future__ import annotations

import sqlite3
from datetime import datetime

from samurai_ai.domain.skills import Skill
from samurai_ai.storage.database import Database


class SkillRepository:
    """Immutable, versioned skill storage (append-only per name)."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, skill: Skill) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO skills(
                    id, name, version, body, sha256, trusted, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill.id,
                    skill.name,
                    skill.version,
                    skill.body,
                    skill.sha256,
                    int(skill.trusted),
                    skill.created_by,
                    skill.created_at.isoformat(),
                ),
            )

    def latest(self, name: str) -> Skill | None:
        with self.database.connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                """
                SELECT * FROM skills WHERE name = ?
                ORDER BY version DESC LIMIT 1
                """,
                (name,),
            ).fetchone()
        return self._to_skill(row) if row is not None else None

    def next_version(self, name: str) -> int:
        latest = self.latest(name)
        return 1 if latest is None else latest.version + 1

    def list_latest(self) -> list[Skill]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.* FROM skills s
                JOIN (
                    SELECT name, MAX(version) AS max_version
                    FROM skills GROUP BY name
                ) latest
                ON s.name = latest.name AND s.version = latest.max_version
                ORDER BY s.name
                """
            ).fetchall()
        return [self._to_skill(row) for row in rows]

    def list_versions(self, name: str) -> list[Skill]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM skills WHERE name = ? ORDER BY version",
                (name,),
            ).fetchall()
        return [self._to_skill(row) for row in rows]

    @staticmethod
    def _to_skill(row: sqlite3.Row) -> Skill:
        return Skill(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            body=row["body"],
            sha256=row["sha256"],
            trusted=bool(row["trusted"]),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
