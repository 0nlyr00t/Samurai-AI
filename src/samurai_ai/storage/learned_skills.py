from __future__ import annotations

import json
from datetime import datetime

from samurai_ai.domain.learned_skills import (
    LearnedSkill,
    LearnedSkillFitness,
    LearnedSkillKind,
    LearnedSkillStatus,
)
from samurai_ai.storage.database import Database


class LearnedSkillRepository:
    """Mutable storage for agent-distilled skills.

    Identity is ``(workspace, sha256)`` (a unique index), so re-extracting the
    same episode updates the existing row instead of inflating the bank.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, skill: LearnedSkill) -> LearnedSkill:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO learned_skills(
                    id, workspace, kind, title, cue, body, status,
                    reuses, findings_after_reuse, median_invalid_actions,
                    source_operation_id, source_run_id, evidence, sha256,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace, sha256) DO UPDATE SET
                    title = excluded.title,
                    evidence = excluded.evidence,
                    source_run_id = excluded.source_run_id,
                    updated_at = excluded.updated_at
                """,
                (
                    skill.id,
                    skill.workspace,
                    skill.kind.value,
                    skill.title,
                    skill.cue,
                    skill.body,
                    skill.status.value,
                    skill.fitness.reuses,
                    skill.fitness.findings_after_reuse,
                    skill.fitness.median_invalid_actions,
                    skill.source_operation_id,
                    skill.source_run_id,
                    json.dumps(list(skill.evidence)),
                    skill.sha256,
                    skill.created_by,
                    skill.created_at.isoformat(),
                    skill.updated_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM learned_skills WHERE workspace = ? AND sha256 = ?",
                (skill.workspace, skill.sha256),
            ).fetchone()
        assert row is not None
        return _skill(row)

    def get(self, skill_id: str) -> LearnedSkill | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM learned_skills WHERE id = ?", (skill_id,)
            ).fetchone()
        return _skill(row) if row else None

    def find_by_sha(self, workspace: str, sha256: str) -> LearnedSkill | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM learned_skills WHERE workspace = ? AND sha256 = ?",
                (workspace, sha256),
            ).fetchone()
        return _skill(row) if row else None

    def list(
        self, workspace: str | None = None, status: LearnedSkillStatus | None = None
    ) -> list[LearnedSkill]:
        query = "SELECT * FROM learned_skills"
        clauses: list[str] = []
        params: list[str] = []
        if workspace is not None:
            clauses.append("workspace = ?")
            params.append(workspace)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [_skill(row) for row in rows]

    def set_status(
        self, skill_id: str, status: LearnedSkillStatus, now: str
    ) -> LearnedSkill | None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE learned_skills SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, skill_id),
            )
            if (cursor.rowcount or 0) == 0:
                return None
            row = connection.execute(
                "SELECT * FROM learned_skills WHERE id = ?", (skill_id,)
            ).fetchone()
        return _skill(row) if row else None

    def record_fitness(
        self, skill_id: str, fitness: LearnedSkillFitness, now: str
    ) -> LearnedSkill | None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learned_skills
                SET reuses = ?, findings_after_reuse = ?, median_invalid_actions = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    fitness.reuses,
                    fitness.findings_after_reuse,
                    fitness.median_invalid_actions,
                    now,
                    skill_id,
                ),
            )
            if (cursor.rowcount or 0) == 0:
                return None
            row = connection.execute(
                "SELECT * FROM learned_skills WHERE id = ?", (skill_id,)
            ).fetchone()
        return _skill(row) if row else None


def _skill(row: object) -> LearnedSkill:
    data = dict(row)  # type: ignore[call-overload]
    return LearnedSkill(
        id=data["id"],
        workspace=data["workspace"],
        kind=LearnedSkillKind(data["kind"]),
        title=data["title"],
        cue=data["cue"],
        body=data["body"],
        status=LearnedSkillStatus(data["status"]),
        fitness=LearnedSkillFitness(
            reuses=int(data["reuses"]),
            findings_after_reuse=int(data["findings_after_reuse"]),
            median_invalid_actions=float(data["median_invalid_actions"]),
        ),
        source_operation_id=data["source_operation_id"],
        source_run_id=data["source_run_id"],
        evidence=tuple(json.loads(data["evidence"])),
        sha256=data["sha256"],
        created_by=data["created_by"],
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
    )
