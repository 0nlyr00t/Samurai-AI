from __future__ import annotations

import json
import sqlite3

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.planning import PlannedTest, PlanScenario, Runbook
from samurai_ai.storage.database import Database


class PlanningConflictError(RuntimeError):
    pass


class PlanningRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def get_scenario(self, operation_id: str) -> PlanScenario | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM plan_scenarios WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return PlanScenario.model_validate_json(row["data"])

    def save_scenario(
        self,
        scenario: PlanScenario,
        expected_version: int | None,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            if expected_version is None:
                connection.execute(
                    """
                    INSERT INTO plan_scenarios(operation_id, data, version, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        scenario.operation_id,
                        scenario.model_dump_json(),
                        scenario.version,
                        scenario.updated_at.isoformat(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE plan_scenarios SET data = ?, version = ?, updated_at = ?
                    WHERE operation_id = ? AND version = ?
                    """,
                    (
                        scenario.model_dump_json(),
                        scenario.version,
                        scenario.updated_at.isoformat(),
                        scenario.operation_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PlanningConflictError(
                        f"scenario for {scenario.operation_id} changed concurrently "
                        "or does not exist"
                    )
            self._append_event(connection, event)

    def add_test_case(self, test_case: PlannedTest, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO test_cases(
                    id, operation_id, status, data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    test_case.id,
                    test_case.operation_id,
                    test_case.status.value,
                    test_case.model_dump_json(),
                    test_case.version,
                    test_case.created_at.isoformat(),
                    test_case.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def update_test_case(
        self,
        test_case: PlannedTest,
        expected_version: int,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE test_cases SET status = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    test_case.status.value,
                    test_case.model_dump_json(),
                    test_case.version,
                    test_case.updated_at.isoformat(),
                    test_case.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise PlanningConflictError(
                    f"test case {test_case.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def get_test_case(self, test_case_id: str) -> PlannedTest | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM test_cases WHERE id = ?",
                (test_case_id,),
            ).fetchone()
        if row is None:
            return None
        return PlannedTest.model_validate_json(row["data"])

    def list_test_cases(self, operation_id: str) -> list[PlannedTest]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM test_cases
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [PlannedTest.model_validate_json(row["data"]) for row in rows]

    def add_runbook(self, runbook: Runbook, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runbooks(
                    id, operation_id, format, sha256, size_bytes, path, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runbook.id,
                    runbook.operation_id,
                    runbook.format.value,
                    runbook.sha256,
                    runbook.size_bytes,
                    runbook.path,
                    runbook.model_dump_json(),
                    runbook.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_runbook(self, runbook_id: str) -> Runbook | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM runbooks WHERE id = ?",
                (runbook_id,),
            ).fetchone()
        if row is None:
            return None
        return Runbook.model_validate_json(row["data"])

    def list_runbooks(self, operation_id: str) -> list[Runbook]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM runbooks
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [Runbook.model_validate_json(row["data"]) for row in rows]

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
