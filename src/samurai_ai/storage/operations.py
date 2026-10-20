from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from samurai_ai.domain.operations import Operation, OperationEvent
from samurai_ai.storage.database import Database


class OperationConflictError(RuntimeError):
    pass


class OperationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, operation: Operation, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    id, name, mode, status, data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation.id,
                    operation.name,
                    operation.mode.value,
                    operation.status.value,
                    operation.model_dump_json(),
                    operation.version,
                    operation.created_at.isoformat(),
                    operation.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get(self, operation_id: str) -> Operation | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return Operation.model_validate_json(row["data"])

    def latest(self) -> Operation | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM operations ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return Operation.model_validate_json(row["data"])

    def list_all(self) -> list[Operation]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM operations ORDER BY created_at, id"
            ).fetchall()
        return [Operation.model_validate_json(row["data"]) for row in rows]

    def update(self, operation: Operation, expected_version: int, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE operations
                SET name = ?, mode = ?, status = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    operation.name,
                    operation.mode.value,
                    operation.status.value,
                    operation.model_dump_json(),
                    operation.version,
                    operation.updated_at.isoformat(),
                    operation.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise OperationConflictError(
                    f"operation {operation.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def append_event(self, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            self._append_event(connection, event)

    def events(self, operation_id: str) -> list[OperationEvent]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, operation_id, event_type, payload, created_at
                FROM operation_events
                WHERE operation_id = ?
                ORDER BY id
                """,
                (operation_id,),
            ).fetchall()
        return [
            OperationEvent(
                id=row["id"],
                operation_id=row["operation_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

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
