from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from samurai_ai.domain.execution import ToolRun, ToolRunStatus
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.storage.database import Database


class ToolRunConflictError(RuntimeError):
    pass


class ToolRunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add_run(self, run: ToolRun, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tool_runs(
                    id, operation_id, task_id, tool_id, tool_version, status,
                    recovery_action_id, approval_id, data, version,
                    created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.operation_id,
                    run.task_id,
                    run.tool_id,
                    run.tool_version,
                    run.status.value,
                    run.recovery_action_id,
                    run.approval_id,
                    run.model_dump_json(),
                    run.version,
                    run.created_at.isoformat(),
                    run.started_at.isoformat() if run.started_at else None,
                    run.finished_at.isoformat() if run.finished_at else None,
                ),
            )
            self._append_event(connection, event)

    def get_run(self, run_id: str) -> ToolRun | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM tool_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return ToolRun.model_validate_json(row["data"])

    def save_run(self, run: ToolRun, expected_version: int, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tool_runs
                SET status = ?, recovery_action_id = ?, approval_id = ?, data = ?,
                    version = ?, started_at = ?, finished_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    run.status.value,
                    run.recovery_action_id,
                    run.approval_id,
                    run.model_dump_json(),
                    run.version,
                    run.started_at.isoformat() if run.started_at else None,
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolRunConflictError(
                    f"tool run {run.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def list_runs(
        self, operation_id: str, status: ToolRunStatus | None = None
    ) -> list[ToolRun]:
        query = "SELECT data FROM tool_runs WHERE operation_id = ?"
        parameters: tuple[object, ...] = (operation_id,)
        if status is not None:
            query += " AND status = ?"
            parameters = (operation_id, status.value)
        query += " ORDER BY created_at, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [ToolRun.model_validate_json(row["data"]) for row in rows]

    def list_runs_by_tool(self, tool_id: str) -> list[ToolRun]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM tool_runs
                WHERE tool_id = ?
                ORDER BY created_at, id
                """,
                (tool_id,),
            ).fetchall()
        return [ToolRun.model_validate_json(row["data"]) for row in rows]

    def mark_in_flight_failed(self, operation_id: str, actor: str) -> list[str]:
        """Mark queued/running runs as failed after a crash; never retried."""
        marked: list[str] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, data, version FROM tool_runs
                WHERE operation_id = ? AND status IN (?, ?)
                ORDER BY created_at, id
                """,
                (operation_id, ToolRunStatus.QUEUED.value, ToolRunStatus.RUNNING.value),
            ).fetchall()
            for row in rows:
                run = ToolRun.model_validate_json(row["data"])
                run.status = ToolRunStatus.FAILED
                run.finished_at = datetime.now(UTC)
                run.version += 1
                cursor = connection.execute(
                    """
                    UPDATE tool_runs
                    SET status = ?, data = ?, version = ?, finished_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        run.status.value,
                        run.model_dump_json(),
                        run.version,
                        run.finished_at.isoformat() if run.finished_at else None,
                        run.id,
                        row["version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ToolRunConflictError(f"tool run {run.id} changed concurrently")
                marked.append(run.id)
            event = OperationEvent(
                operation_id=operation_id,
                event_type="tool.runs.reconciled",
                payload={"actor": actor, "runs_marked_failed": marked},
            )
            self._append_event(connection, event)
        return marked

    @staticmethod
    def _append_event(connection: sqlite3.Connection, event: OperationEvent) -> None:
        connection.execute(
            """
            INSERT INTO operation_events(operation_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event.operation_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                event.created_at.isoformat(),
            ),
        )
