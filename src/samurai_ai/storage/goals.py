from __future__ import annotations

import sqlite3

from samurai_ai.domain.goal import Goal, GoalState
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.storage.database import Database


class GoalConflictError(RuntimeError):
    pass


class GoalRepository:
    """Operation-scoped goals.

    Goals are operator intent, not evidence: mutable, with optimistic version
    checks, and the state changes are journalled as operation events.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, goal: Goal, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO goals(
                    id, operation_id, state, data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal.id,
                    goal.operation_id,
                    goal.state.value,
                    goal.model_dump_json(),
                    goal.version,
                    goal.created_at.isoformat(),
                    goal.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get(self, goal_id: str) -> Goal | None:
        with self.database.connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT data FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()
        return Goal.model_validate_json(row["data"]) if row is not None else None

    def save(self, goal: Goal, expected_version: int, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE goals
                SET state = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    goal.state.value,
                    goal.model_dump_json(),
                    goal.version,
                    goal.updated_at.isoformat(),
                    goal.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise GoalConflictError(
                    f"goal {goal.id} changed underneath (expected v{expected_version})"
                )
            self._append_event(connection, event)

    def list(self, operation_id: str, state: GoalState | None = None) -> list[Goal]:
        query = "SELECT data FROM goals WHERE operation_id = ?"
        parameters: tuple[object, ...] = (operation_id,)
        if state is not None:
            query += " AND state = ?"
            parameters = (operation_id, state.value)
        query += " ORDER BY created_at, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Goal.model_validate_json(row["data"]) for row in rows]

    def current(self, operation_id: str) -> Goal | None:
        """The single active, paused or blocked goal, if any."""
        for state in (GoalState.ACTIVE, GoalState.PAUSED, GoalState.BLOCKED):
            goals = self.list(operation_id, state)
            if goals:
                return goals[0]
        return None

    def next_queued(self, operation_id: str) -> Goal | None:
        queued = self.list(operation_id, GoalState.QUEUED)
        return queued[0] if queued else None

    @staticmethod
    def _append_event(connection: sqlite3.Connection, event: OperationEvent) -> None:
        import json as _json

        connection.execute(
            """
            INSERT INTO operation_events(operation_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                event.operation_id,
                event.event_type,
                _json.dumps(event.payload),
                event.created_at.isoformat(),
            ),
        )
