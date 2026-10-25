from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from samurai_ai.domain.swarm import SwarmRun, SwarmStatus
from samurai_ai.storage.database import Database


class SwarmRunConflictError(RuntimeError):
    pass


class SwarmRunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, swarm: SwarmRun) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO swarm_runs(
                    id, operation_id, status, workers, task_ids, model_route,
                    created_by, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    swarm.id,
                    swarm.operation_id,
                    swarm.status.value,
                    swarm.workers,
                    json.dumps(list(swarm.task_ids)),
                    json.dumps(list(swarm.model_route)),
                    swarm.created_by,
                    swarm.created_at.isoformat(),
                    None,
                ),
            )

    def get(self, swarm_id: str) -> SwarmRun | None:
        with self.database.connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM swarm_runs WHERE id = ?", (swarm_id,)
            ).fetchone()
        return self._to_swarm(row) if row is not None else None

    def finish(self, swarm: SwarmRun) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE swarm_runs SET status = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    swarm.status.value,
                    swarm.finished_at.isoformat() if swarm.finished_at else None,
                    swarm.id,
                    SwarmStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise SwarmRunConflictError(
                    f"swarm {swarm.id} changed concurrently or is already finished"
                )

    def list_swarms(self, operation_id: str) -> list[SwarmRun]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM swarm_runs
                WHERE operation_id = ?
                ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [self._to_swarm(row) for row in rows]

    @staticmethod
    def _to_swarm(row: sqlite3.Row) -> SwarmRun:
        return SwarmRun(
            id=row["id"],
            operation_id=row["operation_id"],
            status=SwarmStatus(row["status"]),
            workers=row["workers"],
            task_ids=tuple(json.loads(row["task_ids"])),
            model_route=tuple(json.loads(row["model_route"])),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
        )
