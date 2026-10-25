from __future__ import annotations

import json
import sqlite3

from samurai_ai.domain.ctf import CtfChallenge, CtfWriteup, FlagRecord, Hypothesis
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.storage.database import Database


class CtfConflictError(RuntimeError):
    pass


class CtfRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def get_challenge(self, operation_id: str) -> CtfChallenge | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM ctf_challenges WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return CtfChallenge.model_validate_json(row["data"])

    def save_challenge(
        self,
        challenge: CtfChallenge,
        expected_version: int | None,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            if expected_version is None:
                connection.execute(
                    """
                    INSERT INTO ctf_challenges(operation_id, data, version, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        challenge.operation_id,
                        challenge.model_dump_json(),
                        challenge.version,
                        challenge.updated_at.isoformat(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE ctf_challenges SET data = ?, version = ?, updated_at = ?
                    WHERE operation_id = ? AND version = ?
                    """,
                    (
                        challenge.model_dump_json(),
                        challenge.version,
                        challenge.updated_at.isoformat(),
                        challenge.operation_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CtfConflictError(
                        f"challenge for {challenge.operation_id} changed concurrently "
                        "or does not exist"
                    )
            self._append_event(connection, event)

    def add_hypothesis(self, hypothesis: Hypothesis, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO hypotheses(
                    id, operation_id, status, data, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hypothesis.id,
                    hypothesis.operation_id,
                    hypothesis.status.value,
                    hypothesis.model_dump_json(),
                    hypothesis.version,
                    hypothesis.created_at.isoformat(),
                    hypothesis.updated_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def update_hypothesis(
        self,
        hypothesis: Hypothesis,
        expected_version: int,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE hypotheses SET status = ?, data = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    hypothesis.status.value,
                    hypothesis.model_dump_json(),
                    hypothesis.version,
                    hypothesis.updated_at.isoformat(),
                    hypothesis.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise CtfConflictError(
                    f"hypothesis {hypothesis.id} changed concurrently or does not exist"
                )
            self._append_event(connection, event)

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM hypotheses WHERE id = ?",
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            return None
        return Hypothesis.model_validate_json(row["data"])

    def list_hypotheses(self, operation_id: str) -> list[Hypothesis]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM hypotheses
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [Hypothesis.model_validate_json(row["data"]) for row in rows]

    def add_flag(self, record: FlagRecord, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO flag_ledger(id, operation_id, name, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.operation_id,
                    record.name,
                    record.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_flag(self, operation_id: str, name: str) -> FlagRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT data FROM flag_ledger
                WHERE operation_id = ? AND name = ?
                """,
                (operation_id, name),
            ).fetchone()
        if row is None:
            return None
        return FlagRecord.model_validate_json(row["data"])

    def list_flags(self, operation_id: str) -> list[FlagRecord]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM flag_ledger
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [FlagRecord.model_validate_json(row["data"]) for row in rows]

    def add_writeup(self, writeup: CtfWriteup, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ctf_writeups(
                    id, operation_id, format, sha256, size_bytes, path, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    writeup.id,
                    writeup.operation_id,
                    writeup.format.value,
                    writeup.sha256,
                    writeup.size_bytes,
                    writeup.path,
                    writeup.model_dump_json(),
                    writeup.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_writeup(self, writeup_id: str) -> CtfWriteup | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM ctf_writeups WHERE id = ?",
                (writeup_id,),
            ).fetchone()
        if row is None:
            return None
        return CtfWriteup.model_validate_json(row["data"])

    def list_writeups(self, operation_id: str) -> list[CtfWriteup]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM ctf_writeups
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [CtfWriteup.model_validate_json(row["data"]) for row in rows]

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
