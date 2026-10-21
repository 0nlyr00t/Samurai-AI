from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.reports import BountyReport
from samurai_ai.storage.database import Database


class ReportRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    @property
    def report_root(self) -> Path:
        return self.database.path.parent / "reports"

    def add_report(self, report: BountyReport, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO reports(
                    id, operation_id, format, sha256, size_bytes, path, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.id,
                    report.operation_id,
                    report.format.value,
                    report.sha256,
                    report.size_bytes,
                    report.path,
                    report.model_dump_json(),
                    report.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_report(self, report_id: str) -> BountyReport | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return BountyReport.model_validate_json(row["data"])

    def list_reports(self, operation_id: str) -> list[BountyReport]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM reports WHERE operation_id = ? ORDER BY created_at, id",
                (operation_id,),
            ).fetchall()
        return [BountyReport.model_validate_json(row["data"]) for row in rows]

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
