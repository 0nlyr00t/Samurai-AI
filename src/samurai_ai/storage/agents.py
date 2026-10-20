from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.storage.database import Database


class AgentRunConflictError(RuntimeError):
    pass


class AgentRunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, run: AgentRun) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, operation_id, task_id, parent_run_id, depth, status,
                    model_route, created_at, created_by, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.operation_id,
                    run.task_id,
                    run.parent_run_id,
                    run.depth,
                    run.status.value,
                    json.dumps(list(run.model_route)),
                    run.created_at.isoformat(),
                    run.created_by,
                    None,
                ),
            )

    def get(self, run_id: str) -> AgentRun | None:
        with self.database.connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._to_run(row) if row is not None else None

    def finish(self, run: AgentRun) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs SET status = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    run.status.value,
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.id,
                    AgentRunStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentRunConflictError(
                    f"agent run {run.id} changed concurrently or is already finished"
                )

    def list_runs(self, operation_id: str) -> list[AgentRun]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE operation_id = ?
                ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [self._to_run(row) for row in rows]

    @staticmethod
    def _to_run(row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            id=row["id"],
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            parent_run_id=row["parent_run_id"],
            depth=row["depth"],
            status=AgentRunStatus(row["status"]),
            model_route=tuple(json.loads(row["model_route"])),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
        )
