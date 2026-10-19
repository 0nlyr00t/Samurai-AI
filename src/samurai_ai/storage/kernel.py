from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from samurai_ai.domain.kernel import (
    Approval,
    ApprovalStatus,
    Artifact,
    Finding,
    FindingStatus,
    OperationPlan,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.storage.database import Database


class KernelConflictError(RuntimeError):
    pass


class KernelRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    @property
    def artifact_root(self) -> Path:
        return self.database.path.parent / "artifacts"

    def add_plan(self, plan: OperationPlan, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            self._insert_plan(connection, plan)
            self._append_event(connection, event)

    def get_plan(self, plan_id: str) -> OperationPlan | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return OperationPlan.model_validate_json(row["data"])

    def get_current_plan(self, operation_id: str) -> OperationPlan | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT data FROM plans
                WHERE operation_id = ? AND is_current = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return OperationPlan.model_validate_json(row["data"])

    def list_plans(self, operation_id: str) -> list[OperationPlan]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM plans WHERE operation_id = ? ORDER BY created_at, id",
                (operation_id,),
            ).fetchall()
        return [OperationPlan.model_validate_json(row["data"]) for row in rows]

    def save_plan(self, plan: OperationPlan, expected_version: int, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE plans
                SET title = ?, approved = ?, is_current = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    plan.title,
                    int(plan.approved),
                    int(plan.is_current),
                    plan.model_dump_json(),
                    plan.version,
                    plan.updated_at.isoformat(),
                    plan.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise KernelConflictError(
                    f"plan {plan.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def replace_current_plan(self, plan: OperationPlan, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id, data, version FROM plans WHERE operation_id = ? AND is_current = 1",
                (plan.operation_id,),
            ).fetchall()
            for row in rows:
                superseded = OperationPlan.model_validate_json(row["data"])
                superseded.is_current = False
                superseded.version += 1
                connection.execute(
                    """
                    UPDATE plans
                    SET is_current = 0, data = ?, version = ?, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        superseded.model_dump_json(),
                        superseded.version,
                        superseded.updated_at.isoformat(),
                        superseded.id,
                        row["version"],
                    ),
                )
            self._insert_plan(connection, plan)
            self._append_event(connection, event)

    def add_approval(self, approval: Approval, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO approvals(
                    id, operation_id, task_id, risk_class, status,
                    data, version, created_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.operation_id,
                    approval.task_id,
                    approval.risk_class.value,
                    approval.status.value,
                    approval.model_dump_json(),
                    approval.version,
                    approval.created_at.isoformat(),
                    approval.decided_at.isoformat() if approval.decided_at else None,
                ),
            )
            self._append_event(connection, event)

    def get_approval(self, approval_id: str) -> Approval | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        return Approval.model_validate_json(row["data"])

    def list_approvals(
        self, operation_id: str, status: ApprovalStatus | None = None
    ) -> list[Approval]:
        query = "SELECT data FROM approvals WHERE operation_id = ?"
        parameters: tuple[object, ...] = (operation_id,)
        if status is not None:
            query += " AND status = ?"
            parameters = (operation_id, status.value)
        query += " ORDER BY created_at, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Approval.model_validate_json(row["data"]) for row in rows]

    def save_approval(
        self, approval: Approval, expected_version: int, event: OperationEvent
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals
                SET status = ?, data = ?, version = ?, decided_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    approval.status.value,
                    approval.model_dump_json(),
                    approval.version,
                    approval.decided_at.isoformat() if approval.decided_at else None,
                    approval.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise KernelConflictError(
                    f"approval {approval.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def add_artifact(self, artifact: Artifact, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, operation_id, kind, requirement, task_id, gate_id, finding_id,
                    sha256, size_bytes, path, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.operation_id,
                    artifact.kind.value,
                    artifact.requirement,
                    artifact.task_id,
                    artifact.gate_id,
                    artifact.finding_id,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.path,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return Artifact.model_validate_json(row["data"])

    def list_artifacts(self, operation_id: str) -> list[Artifact]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM artifacts WHERE operation_id = ? ORDER BY created_at, id",
                (operation_id,),
            ).fetchall()
        return [Artifact.model_validate_json(row["data"]) for row in rows]

    def artifact_exists(
        self,
        operation_id: str,
        requirement: str,
        *,
        task_id: str | None = None,
        gate_id: str | None = None,
    ) -> bool:
        query = "SELECT 1 FROM artifacts WHERE operation_id = ? AND requirement = ?"
        parameters: tuple[object, ...] = (operation_id, requirement)
        if task_id is not None:
            query += " AND task_id = ?"
            parameters = (*parameters, task_id)
        if gate_id is not None:
            query += " AND gate_id = ?"
            parameters = (*parameters, gate_id)
        query += " LIMIT 1"
        with self.database.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return row is not None

    def add_finding(self, finding: Finding, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO findings(
                    id, operation_id, title, severity, status, duplicate_of,
                    data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding.id,
                    finding.operation_id,
                    finding.title,
                    finding.severity.value,
                    finding.status.value,
                    finding.duplicate_of,
                    finding.model_dump_json(),
                    finding.version,
                    finding.created_at.isoformat(),
                    finding.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_finding(self, finding_id: str) -> Finding | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM findings WHERE id = ?",
                (finding_id,),
            ).fetchone()
        if row is None:
            return None
        return Finding.model_validate_json(row["data"])

    def list_findings(
        self, operation_id: str, status: FindingStatus | None = None
    ) -> list[Finding]:
        query = "SELECT data FROM findings WHERE operation_id = ?"
        parameters: tuple[object, ...] = (operation_id,)
        if status is not None:
            query += " AND status = ?"
            parameters = (operation_id, status.value)
        query += " ORDER BY created_at, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Finding.model_validate_json(row["data"]) for row in rows]

    def save_finding(
        self, finding: Finding, expected_version: int, event: OperationEvent
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE findings
                SET status = ?, severity = ?, duplicate_of = ?, data = ?, version = ?,
                    updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    finding.status.value,
                    finding.severity.value,
                    finding.duplicate_of,
                    finding.model_dump_json(),
                    finding.version,
                    finding.updated_at.isoformat(),
                    finding.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise KernelConflictError(
                    f"finding {finding.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    @staticmethod
    def _insert_plan(connection: sqlite3.Connection, plan: OperationPlan) -> None:
        connection.execute(
            """
            INSERT INTO plans(
                id, operation_id, title, approved, is_current,
                data, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.id,
                plan.operation_id,
                plan.title,
                int(plan.approved),
                int(plan.is_current),
                plan.model_dump_json(),
                plan.version,
                plan.created_at.isoformat(),
                plan.updated_at.isoformat(),
            ),
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
