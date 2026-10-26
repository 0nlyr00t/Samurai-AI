from __future__ import annotations

import sqlite3
from datetime import datetime

from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ProviderDefinition,
)
from samurai_ai.storage.database import Database


class ModelEndpointConflictError(RuntimeError):
    pass


class CustomModelConflictError(RuntimeError):
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


class CustomModelRepository:
    """Operator-registered model names layered on top of any provider.

    Lets the operator whitelist new provider-side model names (for example a
    freshly released model, or a name containing slashes like
    ``giga-osa/deepseek-v4.1-flash-uncensored``) without a code change.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, model: ModelDefinition, created_by: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM custom_models WHERE id = ?", (model.id,)
            ).fetchone()
            if row is not None:
                raise CustomModelConflictError(
                    f"custom model already exists: {model.id}"
                )
            connection.execute(
                """
                INSERT INTO custom_models(
                    id, provider_id, display_name, context_limit,
                    max_output_tokens, capabilities, created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model.id,
                    model.provider_id,
                    model.display_name or model.id,
                    model.context_limit,
                    model.max_output_tokens,
                    ",".join(sorted(cap.value for cap in model.capabilities)),
                    datetime.now().astimezone().isoformat(),
                    created_by,
                ),
            )

    def get(self, model_id: str) -> ModelDefinition | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM custom_models WHERE id = ?", (model_id,)
            ).fetchone()
        return self._to_model(row) if row is not None else None

    def list_all(self, provider_id: str | None = None) -> list[ModelDefinition]:
        with self.database.connect() as connection:
            if provider_id is None:
                rows = connection.execute(
                    "SELECT * FROM custom_models ORDER BY id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM custom_models WHERE provider_id = ? ORDER BY id",
                    (provider_id,),
                ).fetchall()
        return [self._to_model(row) for row in rows]

    def delete(self, model_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM custom_models WHERE id = ?", (model_id,)
            )
            return cursor.rowcount == 1

    @staticmethod
    def _to_model(row: sqlite3.Row) -> ModelDefinition:
        capabilities = frozenset(
            ModelCapability(value)
            for value in row["capabilities"].split(",")
            if value
        )
        return ModelDefinition(
            id=row["id"],
            provider_id=row["provider_id"],
            display_name=row["display_name"],
            context_limit=row["context_limit"],
            max_output_tokens=row["max_output_tokens"],
            capabilities=capabilities,
        )
