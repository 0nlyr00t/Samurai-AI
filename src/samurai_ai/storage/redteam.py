from __future__ import annotations

import json
import sqlite3

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.redteam import AttackStep, Checkpoint, Debrief, RoeDocument
from samurai_ai.storage.database import Database


class RedTeamConflictError(RuntimeError):
    pass


class RedTeamRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def get_roe(self, operation_id: str) -> RoeDocument | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM red_team_roe WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return RoeDocument.model_validate_json(row["data"])

    def save_roe(
        self,
        roe: RoeDocument,
        expected_version: int | None,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            if expected_version is None:
                connection.execute(
                    """
                    INSERT INTO red_team_roe(operation_id, data, version, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        roe.operation_id,
                        roe.model_dump_json(),
                        roe.version,
                        roe.updated_at.isoformat(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE red_team_roe SET data = ?, version = ?, updated_at = ?
                    WHERE operation_id = ? AND version = ?
                    """,
                    (
                        roe.model_dump_json(),
                        roe.version,
                        roe.updated_at.isoformat(),
                        roe.operation_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RedTeamConflictError(
                        f"ROE for {roe.operation_id} changed concurrently "
                        "or does not exist"
                    )
            self._append_event(connection, event)

    def add_step(self, step: AttackStep, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO attack_steps(
                    id, operation_id, status, data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.id,
                    step.operation_id,
                    step.status.value,
                    step.model_dump_json(),
                    step.version,
                    step.created_at.isoformat(),
                    step.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def update_step(
        self,
        step: AttackStep,
        expected_version: int,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE attack_steps SET status = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    step.status.value,
                    step.model_dump_json(),
                    step.version,
                    step.updated_at.isoformat(),
                    step.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RedTeamConflictError(
                    f"attack step {step.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def get_step(self, step_id: str) -> AttackStep | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM attack_steps WHERE id = ?",
                (step_id,),
            ).fetchone()
        if row is None:
            return None
        return AttackStep.model_validate_json(row["data"])

    def list_steps(self, operation_id: str) -> list[AttackStep]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM attack_steps
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [AttackStep.model_validate_json(row["data"]) for row in rows]

    def add_checkpoint(self, checkpoint: Checkpoint, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(
                    id, operation_id, status, data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.id,
                    checkpoint.operation_id,
                    checkpoint.status.value,
                    checkpoint.model_dump_json(),
                    checkpoint.version,
                    checkpoint.created_at.isoformat(),
                    checkpoint.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def update_checkpoint(
        self,
        checkpoint: Checkpoint,
        expected_version: int,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE checkpoints SET status = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    checkpoint.status.value,
                    checkpoint.model_dump_json(),
                    checkpoint.version,
                    checkpoint.updated_at.isoformat(),
                    checkpoint.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RedTeamConflictError(
                    f"checkpoint {checkpoint.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint.model_validate_json(row["data"])

    def list_checkpoints(self, operation_id: str) -> list[Checkpoint]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM checkpoints
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [Checkpoint.model_validate_json(row["data"]) for row in rows]

    def add_debrief(self, debrief: Debrief, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO debriefs(
                    id, operation_id, format, sha256, size_bytes, path, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    debrief.id,
                    debrief.operation_id,
                    debrief.format.value,
                    debrief.sha256,
                    debrief.size_bytes,
                    debrief.path,
                    debrief.model_dump_json(),
                    debrief.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_debrief(self, debrief_id: str) -> Debrief | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM debriefs WHERE id = ?",
                (debrief_id,),
            ).fetchone()
        if row is None:
            return None
        return Debrief.model_validate_json(row["data"])

    def list_debriefs(self, operation_id: str) -> list[Debrief]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM debriefs
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [Debrief.model_validate_json(row["data"]) for row in rows]

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
