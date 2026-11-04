"""Installed plugins: the package row, its files and what it brought."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from samurai_ai.storage.database import Database


@dataclass(frozen=True)
class PluginRow:
    name: str
    version: str
    description: str
    author: str
    installed_at: str


@dataclass(frozen=True)
class PluginFileRow:
    path: str
    kind: str
    sha256: str


class PluginRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        name: str,
        version: str,
        files: list[PluginFileRow],
        assets: list[tuple[str, str]],
        *,
        description: str = "",
        author: str = "",
        installed_by: str = "operator",
    ) -> PluginRow:
        row = PluginRow(
            name=name,
            version=version,
            description=description,
            author=author,
            installed_at=datetime.now(UTC).isoformat(),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO plugins
                    (name, version, description, author, installed_at, installed_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, version, description, author, row.installed_at, installed_by),
            )
            connection.executemany(
                """
                INSERT INTO plugin_files (plugin_name, path, kind, sha256)
                VALUES (?, ?, ?, ?)
                """,
                [(name, item.path, item.kind, item.sha256) for item in files],
            )
            connection.executemany(
                "INSERT INTO plugin_assets (plugin_name, kind, ident) VALUES (?, ?, ?)",
                [(name, kind, ident) for kind, ident in assets],
            )
        return row

    def get(self, name: str) -> PluginRow | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT name, version, description, author, installed_at "
                "FROM plugins WHERE name = ?",
                (name,),
            ).fetchone()
        return PluginRow(**dict(row)) if row else None

    def list_all(self) -> list[PluginRow]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT name, version, description, author, installed_at "
                "FROM plugins ORDER BY name"
            ).fetchall()
        return [PluginRow(**dict(row)) for row in rows]

    def files(self, name: str) -> list[PluginFileRow]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT path, kind, sha256 FROM plugin_files "
                "WHERE plugin_name = ? ORDER BY path",
                (name,),
            ).fetchall()
        return [PluginFileRow(**dict(row)) for row in rows]

    def assets(self, name: str) -> list[tuple[str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT kind, ident FROM plugin_assets WHERE plugin_name = ? "
                "ORDER BY kind, ident",
                (name,),
            ).fetchall()
        return [(row["kind"], row["ident"]) for row in rows]

    def remove(self, name: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM plugins WHERE name = ?", (name,))
        return cursor.rowcount > 0


__all__ = ["PluginFileRow", "PluginRepository", "PluginRow"]
