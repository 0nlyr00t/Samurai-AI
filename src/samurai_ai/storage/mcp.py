from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from samurai_ai.domain.mcp import McpServer, McpToolInfo
from samurai_ai.storage.database import Database


class McpServerConflictError(RuntimeError):
    pass


class McpServerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, server: McpServer) -> None:
        with self.database.transaction() as connection:
            if self._find_row(connection, server.name) is not None:
                raise McpServerConflictError(f"MCP server already exists: {server.name}")
            connection.execute(
                """
                INSERT INTO mcp_servers(
                    id, name, transport, command, url, trust_level, enabled,
                    created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server.id,
                    server.name,
                    server.transport,
                    json.dumps(list(server.command)) if server.command else None,
                    server.url,
                    server.trust_level,
                    1 if server.enabled else 0,
                    server.created_at.isoformat(),
                    server.created_by,
                ),
            )

    def get(self, name: str) -> McpServer | None:
        with self.database.connect() as connection:
            row = self._find_row(connection, name)
        return self._to_server(row) if row is not None else None

    def list_all(self) -> list[McpServer]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mcp_servers ORDER BY name"
            ).fetchall()
        return [self._to_server(row) for row in rows]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE mcp_servers SET enabled = ? WHERE name = ?",
                (1 if enabled else 0, name),
            )
            return cursor.rowcount == 1

    def delete(self, name: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM mcp_servers WHERE name = ?", (name,)
            )
            return cursor.rowcount == 1

    def get_tools_cache(self, server_name: str) -> list[McpToolInfo]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (self._tools_key(server_name),),
            ).fetchone()
        if row is None:
            return []
        payload = json.loads(row["value"])
        return [McpToolInfo.model_validate(entry) for entry in payload]

    def set_tools_cache(self, server_name: str, tools: list[McpToolInfo]) -> None:
        payload = json.dumps(
            [tool.model_dump(mode="json") for tool in tools],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (self._tools_key(server_name), payload, datetime.now(UTC).isoformat()),
            )

    def delete_tools_cache(self, server_name: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM app_settings WHERE key = ?", (self._tools_key(server_name),)
            )

    @staticmethod
    def _tools_key(server_name: str) -> str:
        return f"mcp.tools.{server_name}"

    @staticmethod
    def _find_row(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM mcp_servers WHERE name = ?", (name,)
        ).fetchone()
        return row

    @staticmethod
    def _to_server(row: sqlite3.Row) -> McpServer:
        command = json.loads(row["command"]) if row["command"] else None
        return McpServer(
            id=row["id"],
            name=row["name"],
            transport=row["transport"],
            command=tuple(command) if command else None,
            url=row["url"],
            trust_level=row["trust_level"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
        )
