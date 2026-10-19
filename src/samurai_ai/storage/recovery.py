from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.recovery import (
    OperationSnapshot,
    RecoveryAction,
    RecoveryActionStatus,
)
from samurai_ai.storage.database import Database


class RecoveryConflictError(RuntimeError):
    pass


class RecoveryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add_snapshot(self, snapshot: OperationSnapshot, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operation_snapshots(id, operation_id, label, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.operation_id,
                    snapshot.label,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_snapshot(self, snapshot_id: str) -> OperationSnapshot | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM operation_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return OperationSnapshot.model_validate_json(row["data"])

    def list_snapshots(self, operation_id: str) -> list[OperationSnapshot]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM operation_snapshots
                WHERE operation_id = ?
                ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [OperationSnapshot.model_validate_json(row["data"]) for row in rows]

    def add_action(self, action: RecoveryAction, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO recovery_actions(
                    id, operation_id, task_id, action_type, status,
                    data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.id,
                    action.operation_id,
                    action.task_id,
                    action.action_type,
                    action.status.value,
                    action.model_dump_json(),
                    action.version,
                    action.created_at.isoformat(),
                    action.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_action(self, action_id: str) -> RecoveryAction | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM recovery_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        return RecoveryAction.model_validate_json(row["data"])

    def save_action(
        self, action: RecoveryAction, expected_version: int, event: OperationEvent
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE recovery_actions
                SET status = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    action.status.value,
                    action.model_dump_json(),
                    action.version,
                    action.updated_at.isoformat(),
                    action.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RecoveryConflictError(
                    f"recovery action {action.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def list_actions(
        self, operation_id: str, status: RecoveryActionStatus | None = None
    ) -> list[RecoveryAction]:
        query = "SELECT data FROM recovery_actions WHERE operation_id = ?"
        parameters: tuple[object, ...] = (operation_id,)
        if status is not None:
            query += " AND status = ?"
            parameters = (operation_id, status.value)
        query += " ORDER BY created_at, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [RecoveryAction.model_validate_json(row["data"]) for row in rows]

    def mark_in_flight_unknown(self, operation_id: str, actor: str) -> list[str]:
        marked: list[str] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, data, version FROM recovery_actions
                WHERE operation_id = ? AND status = ?
                ORDER BY created_at, id
                """,
                (operation_id, RecoveryActionStatus.IN_PROGRESS.value),
            ).fetchall()
            for row in rows:
                action = RecoveryAction.model_validate_json(row["data"])
                action.mark_unknown()
                cursor = connection.execute(
                    """
                    UPDATE recovery_actions
                    SET status = ?, data = ?, version = ?, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        action.status.value,
                        action.model_dump_json(),
                        action.version,
                        action.updated_at.isoformat(),
                        action.id,
                        row["version"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RecoveryConflictError(
                        f"recovery action {action.id} changed concurrently"
                    )
                marked.append(action.id)
            event = OperationEvent(
                operation_id=operation_id,
                event_type="recovery.reconciled",
                payload={"actor": actor, "actions_marked_unknown": marked},
            )
            self._append_event(connection, event)
        return marked

    def get_setting(self, key: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, datetime.now(UTC).isoformat()),
            )

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
