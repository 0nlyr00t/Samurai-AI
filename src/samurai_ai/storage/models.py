from __future__ import annotations

import sqlite3
from datetime import datetime

from samurai_ai.domain.models import ApiFamily, ProviderDefinition
from samurai_ai.storage.database import Database


class ModelEndpointConflictError(RuntimeError):
    pass


class ModelEndpointRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, endpoint: ProviderDefinition, created_by: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM model_endpoints WHERE id = ?", (endpoint.id,)
            ).fetchone()
            if row is not None:
                raise ModelEndpointConflictError(
                    f"model endpoint already exists: {endpoint.id}"
                )
            connection.execute(
                """
                INSERT INTO model_endpoints(
                    id, api_family, base_url, secret_name, display_name, enabled,
                    created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    endpoint.id,
                    endpoint.api_family.value,
                    endpoint.base_url,
                    endpoint.secret_name,
                    endpoint.display_name or endpoint.id,
                    1 if endpoint.enabled else 0,
                    datetime.now().astimezone().isoformat(),
                    created_by,
                ),
            )

    def get(self, endpoint_id: str) -> ProviderDefinition | None:
        with self.database.connect() as connection:
            row = self._find(connection, endpoint_id)
        return self._to_provider(row) if row is not None else None

    def list_all(self) -> list[ProviderDefinition]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_endpoints ORDER BY id"
            ).fetchall()
        return [self._to_provider(row) for row in rows]

    def delete(self, endpoint_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM model_endpoints WHERE id = ?", (endpoint_id,)
            )
            return cursor.rowcount == 1

    @staticmethod
    def _find(connection: sqlite3.Connection, endpoint_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM model_endpoints WHERE id = ?", (endpoint_id,)
        ).fetchone()
        return row

    @staticmethod
    def _to_provider(row: sqlite3.Row) -> ProviderDefinition:
        return ProviderDefinition(
            id=row["id"],
            api_family=ApiFamily(row["api_family"]),
            base_url=row["base_url"],
            display_name=row["display_name"],
            secret_name=row["secret_name"],
            enabled=bool(row["enabled"]),
            local=False,
        )
