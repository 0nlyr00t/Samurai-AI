from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from samurai_ai.domain.vault import SecretMetadata, VaultError
from samurai_ai.storage.database import Database


class SecretConflictError(VaultError):
    pass


@dataclass(frozen=True)
class SecretEntry:
    metadata: SecretMetadata
    ciphertext: bytes
    nonce: bytes


class VaultRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, entry: SecretEntry) -> None:
        metadata = entry.metadata
        with self.database.transaction() as connection:
            if self._find(connection, metadata.name, metadata.operation_id) is not None:
                scope = metadata.operation_id or "global"
                raise SecretConflictError(
                    f"secret {metadata.name!r} already exists in scope {scope}"
                )
            connection.execute(
                """
                INSERT INTO secret_entries(
                    id, operation_id, name, ciphertext, nonce, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.id,
                    metadata.operation_id,
                    metadata.name,
                    entry.ciphertext,
                    entry.nonce,
                    metadata.created_at.isoformat(),
                    metadata.created_by,
                ),
            )

    def find(self, name: str, operation_id: str | None) -> SecretEntry | None:
        with self.database.connect() as connection:
            return self._find(connection, name, operation_id)

    def find_visible(self, name: str, operation_id: str | None) -> SecretEntry | None:
        with self.database.connect() as connection:
            if operation_id is not None:
                entry = self._find(connection, name, operation_id)
                if entry is not None:
                    return entry
            return self._find(connection, name, None)

    def find_any(self, name: str) -> SecretEntry | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, operation_id, name, ciphertext, nonce, created_at, created_by
                FROM secret_entries
                WHERE name = ?
                ORDER BY operation_id IS NULL, id
                LIMIT 1
                """,
                (name,),
            ).fetchone()
        if row is None:
            return None
        return SecretEntry(
            metadata=self._to_metadata(row),
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
        )

    def delete(self, name: str, operation_id: str | None) -> bool:
        with self.database.transaction() as connection:
            if operation_id is None:
                cursor = connection.execute(
                    "DELETE FROM secret_entries WHERE name = ? AND operation_id IS NULL",
                    (name,),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM secret_entries WHERE name = ? AND operation_id = ?",
                    (name, operation_id),
                )
            return cursor.rowcount == 1

    def list_metadata(self, operation_id: str | None) -> list[SecretMetadata]:
        if operation_id is None:
            query = (
                "SELECT id, operation_id, name, created_at, created_by "
                "FROM secret_entries WHERE operation_id IS NULL "
                "ORDER BY name, id"
            )
            parameters: tuple[object, ...] = ()
        else:
            query = (
                "SELECT id, operation_id, name, created_at, created_by "
                "FROM secret_entries "
                "WHERE operation_id IS NULL OR operation_id = ? "
                "ORDER BY name, id"
            )
            parameters = (operation_id,)
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._to_metadata(row) for row in rows]

    @staticmethod
    def _find(
        connection: sqlite3.Connection, name: str, operation_id: str | None
    ) -> SecretEntry | None:
        if operation_id is None:
            row = connection.execute(
                """
                SELECT id, operation_id, name, ciphertext, nonce, created_at, created_by
                FROM secret_entries
                WHERE name = ? AND operation_id IS NULL
                """,
                (name,),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT id, operation_id, name, ciphertext, nonce, created_at, created_by
                FROM secret_entries
                WHERE name = ? AND operation_id = ?
                """,
                (name, operation_id),
            ).fetchone()
        if row is None:
            return None
        return SecretEntry(
            metadata=VaultRepository._to_metadata(row),
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
        )

    @staticmethod
    def _to_metadata(row: sqlite3.Row) -> SecretMetadata:
        return SecretMetadata(
            id=row["id"],
            operation_id=row["operation_id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
        )
