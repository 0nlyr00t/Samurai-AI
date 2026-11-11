from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from samurai_ai.domain.operations import Operation, OperationEvent
from samurai_ai.storage.database import Database

_log = logging.getLogger(__name__)


class OperationConflictError(RuntimeError):
    pass


#: re-armed after a delete; the event log stays append-only
_EVENTS_NO_UPDATE = """
CREATE TRIGGER IF NOT EXISTS operation_events_no_update
BEFORE UPDATE ON operation_events
BEGIN
    SELECT RAISE(ABORT, 'operation events are append-only');
END
"""
_EVENTS_NO_DELETE = """
CREATE TRIGGER IF NOT EXISTS operation_events_no_delete
BEFORE DELETE ON operation_events
BEGIN
    SELECT RAISE(ABORT, 'operation events are append-only');
END
"""


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
            rows = connection.execute(
                "SELECT id, data FROM operations ORDER BY created_at DESC, id DESC"
            ).fetchall()
        # a single unreadable row must not brick startup: the welcome screen and
        # the operations strip both call this on mount, so skip past a corrupt
        # record to the newest one that still parses
        for row in rows:
            operation = self._parse_row(row)
            if operation is not None:
                return operation
        return None

    def list_all(self) -> list[Operation]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, data FROM operations ORDER BY created_at, id"
            ).fetchall()
        parsed = [self._parse_row(row) for row in rows]
        return [operation for operation in parsed if operation is not None]

    @staticmethod
    def _parse_row(row: sqlite3.Row) -> Operation | None:
        """Validate a stored operation, tolerating a corrupt ``data`` blob.

        A row whose JSON no longer matches the schema (a half-written record, a
        value clobbered by an older build or outside tampering) is logged and
        skipped rather than raised, so one bad operation cannot take the whole
        app down on load.
        """
        try:
            return Operation.model_validate_json(row["data"])
        except ValidationError as error:
            _log.warning("skipping unreadable operation %s: %s", row["id"], error)
            return None

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

    def events_since(
        self, operation_id: str, after_id: int, limit: int
    ) -> list[tuple[int, str, dict[str, Any]]]:
        """Forward page of the journal: the event feed's only buffer."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, payload
                FROM operation_events
                WHERE operation_id = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (operation_id, after_id, limit),
            ).fetchall()
        return [(row["id"], row["event_type"], json.loads(row["payload"])) for row in rows]

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
    def delete(self, operation_id: str) -> bool:
        """Drop the operation and everything hanging off it.

        Every owned table carries ON DELETE CASCADE, so the operation row takes
        its events, runs, artifacts and findings with it. Two holdsouts: vault
        entries scoped to the operation (no FK, and meaningless once the
        operation is gone) are removed by hand, and the row itself last, so a
        failure leaves an operation that is still readable.
        """
        with self.database.transaction() as connection:
            # the event log refuses UPDATE and DELETE by trigger, so the cascade
            # from `operations` would abort; the triggers step aside and come back
            # inside the same transaction (SQLite rolls DDL back with the rest)
            connection.execute("DROP TRIGGER IF EXISTS operation_events_no_delete")
            connection.execute("DROP TRIGGER IF EXISTS operation_events_no_update")
            connection.execute(
                "DELETE FROM secret_entries WHERE operation_id = ?", (operation_id,)
            )
            cursor = connection.execute("DELETE FROM operations WHERE id = ?", (operation_id,))
            connection.execute(_EVENTS_NO_UPDATE)
            connection.execute(_EVENTS_NO_DELETE)
        return cursor.rowcount > 0
