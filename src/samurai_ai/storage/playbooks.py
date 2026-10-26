from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.playbooks import (
    AssetKind,
    AssetLedgerEntry,
    BountyProgramRules,
    FindingFingerprintRecord,
    PlaybookTemplate,
)
from samurai_ai.storage.database import Database


class PlaybookConflictError(RuntimeError):
    pass


class PlaybookRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def get_rules(self, operation_id: str) -> BountyProgramRules | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM program_rules WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return BountyProgramRules.model_validate_json(row["data"])

    def save_rules(
        self,
        rules: BountyProgramRules,
        expected_version: int | None,
        event: OperationEvent,
    ) -> None:
        with self.database.transaction() as connection:
            if expected_version is None:
                connection.execute(
                    """
                    INSERT INTO program_rules(operation_id, data, version, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        rules.operation_id,
                        rules.model_dump_json(),
                        rules.version,
                        rules.updated_at.isoformat(),
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE program_rules SET data = ?, version = ?, updated_at = ?
                    WHERE operation_id = ? AND version = ?
                    """,
                    (
                        rules.model_dump_json(),
                        rules.version,
                        rules.updated_at.isoformat(),
                        rules.operation_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PlaybookConflictError(
                        f"program rules for {rules.operation_id} changed concurrently "
                        "or do not exist"
                    )
            self._append_event(connection, event)

    def add_asset(self, entry: AssetLedgerEntry, event: OperationEvent) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset_ledger_entries(
                    id, operation_id, kind, value, source, data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.operation_id,
                    entry.kind.value,
                    entry.value,
                    entry.source,
                    entry.model_dump_json(),
                    entry.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def get_asset(
        self, operation_id: str, kind: AssetKind, value: str
    ) -> AssetLedgerEntry | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT data FROM asset_ledger_entries
                WHERE operation_id = ? AND kind = ? AND value = ?
                """,
                (operation_id, kind.value, value),
            ).fetchone()
        if row is None:
            return None
        return AssetLedgerEntry.model_validate_json(row["data"])

    def list_assets(self, operation_id: str) -> list[AssetLedgerEntry]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT data FROM asset_ledger_entries
                WHERE operation_id = ? ORDER BY created_at, id
                """,
                (operation_id,),
            ).fetchall()
        return [AssetLedgerEntry.model_validate_json(row["data"]) for row in rows]

    def retire_asset(
        self, operation_id: str, kind: AssetKind, value: str, event: OperationEvent
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset_retirements(operation_id, kind, value, retired_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    operation_id,
                    kind.value,
                    value,
                    event.created_at.isoformat(),
                ),
            )
            self._append_event(connection, event)

    def list_retirements(
        self, operation_id: str
    ) -> list[tuple[AssetKind, str, datetime]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT kind, value, retired_at FROM asset_retirements
                WHERE operation_id = ? ORDER BY retired_at, kind, value
                """,
                (operation_id,),
            ).fetchall()
        return [
            (AssetKind(row["kind"]), row["value"], datetime.fromisoformat(row["retired_at"]))
            for row in rows
        ]

    def add_fingerprint(self, record: FindingFingerprintRecord) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO finding_fingerprints(
                    finding_id, operation_id, fingerprint, asset, category, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.finding_id,
                    record.operation_id,
                    record.fingerprint,
                    record.asset,
                    record.category,
                    record.created_at.isoformat(),
                ),
            )

    def find_fingerprint(
        self, operation_id: str, fingerprint: str
    ) -> FindingFingerprintRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT finding_id, asset, category, created_at FROM finding_fingerprints
                WHERE operation_id = ? AND fingerprint = ?
                ORDER BY created_at, finding_id LIMIT 1
                """,
                (operation_id, fingerprint),
            ).fetchone()
        if row is None:
            return None
        return FindingFingerprintRecord(
            finding_id=row["finding_id"],
            operation_id=operation_id,
            fingerprint=fingerprint,
            asset=row["asset"],
            category=row["category"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_fingerprint_for_finding(
        self, finding_id: str
    ) -> FindingFingerprintRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT operation_id, fingerprint, asset, category, created_at
                FROM finding_fingerprints WHERE finding_id = ?
                """,
                (finding_id,),
            ).fetchone()
        if row is None:
            return None
        return FindingFingerprintRecord(
            finding_id=finding_id,
            operation_id=row["operation_id"],
            fingerprint=row["fingerprint"],
            asset=row["asset"],
            category=row["category"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def add_custom_playbook(
        self, template: PlaybookTemplate, sha256: str, created_by: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO custom_playbooks(name, title, sha256, data, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    template.name,
                    template.title,
                    sha256,
                    template.model_dump_json(),
                    created_by,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_custom_playbook(self, name: str) -> PlaybookTemplate | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT data FROM custom_playbooks WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return PlaybookTemplate.model_validate_json(row["data"])

    def list_custom_playbooks(self) -> list[PlaybookTemplate]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM custom_playbooks ORDER BY name"
            ).fetchall()
        return [PlaybookTemplate.model_validate_json(row["data"]) for row in rows]

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
