from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from samurai_ai.domain.memory import MemoryEntry, MemoryKind
from samurai_ai.storage.database import Database


class MemoryConflictError(RuntimeError):
    pass


class MemoryRepository:
    """Workspace-scoped campaign memory.

    Deliberately mutable and deletable: memory is curated operator knowledge,
    not operation evidence, so no append-only triggers apply. Updates use
    optimistic version checks.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add(self, entry: MemoryEntry) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO campaign_memory(
                    id, workspace, kind, title, body, tags, retention_days,
                    version, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.workspace,
                    entry.kind.value,
                    entry.title,
                    entry.body,
                    json.dumps(list(entry.tags)),
                    entry.retention_days,
                    entry.version,
                    entry.created_by,
                    entry.created_at.isoformat(),
                    entry.updated_at.isoformat(),
                ),
            )

    def get(self, entry_id: str) -> MemoryEntry | None:
        with self.database.connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                "SELECT * FROM campaign_memory WHERE id = ?", (entry_id,)
            ).fetchone()
        return self._to_entry(row) if row is not None else None

    def update(self, entry: MemoryEntry, expected_version: int) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE campaign_memory
                SET kind = ?, title = ?, body = ?, tags = ?, retention_days = ?,
                    version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    entry.kind.value,
                    entry.title,
                    entry.body,
                    json.dumps(list(entry.tags)),
                    entry.retention_days,
                    entry.version,
                    entry.updated_at.isoformat(),
                    entry.id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryConflictError(
                    f"memory entry {entry.id} changed concurrently or does not exist"
                )

    def delete(self, entry_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM campaign_memory WHERE id = ?", (entry_id,)
            )
            return cursor.rowcount == 1

    def list_entries(self, workspace: str | None = None) -> list[MemoryEntry]:
        query = "SELECT * FROM campaign_memory"
        params: tuple[str, ...] = ()
        if workspace is not None:
            query += " WHERE workspace = ?"
            params = (workspace,)
        query += " ORDER BY updated_at DESC, id"
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_entry(row) for row in rows]

    def purge_expired(self, now: datetime | None = None) -> list[str]:
        moment = now or datetime.now(UTC)
        expired = [entry.id for entry in self.list_entries() if entry.is_expired(moment)]
        if not expired:
            return []
        with self.database.transaction() as connection:
            connection.executemany(
                "DELETE FROM campaign_memory WHERE id = ?", [(entry_id,) for entry_id in expired]
            )
        return expired

    def list_workspaces(self) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT workspace FROM campaign_memory ORDER BY workspace"
            ).fetchall()
        return [str(row["workspace"]) for row in rows]

    @staticmethod
    def _to_entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            workspace=row["workspace"],
            kind=MemoryKind(row["kind"]),
            title=row["title"],
            body=row["body"],
            tags=tuple(json.loads(row["tags"])),
            retention_days=row["retention_days"],
            version=row["version"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class MemorySearchRepository:
    """FTS5 over campaign memory: find the past, not read it all."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()
        with self.database.connect() as connection:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(entry_id UNINDEXED, title, body, tags)
                """
            )

    def index(self, entry: MemoryEntry) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM memory_fts WHERE entry_id = ?", (entry.id,)
            )
            connection.execute(
                "INSERT INTO memory_fts (entry_id, title, body, tags) VALUES (?, ?, ?, ?)",
                (entry.id, entry.title, entry.body, " ".join(entry.tags)),
            )

    def search(self, query: str, limit: int = 8) -> list[str]:
        """Entry ids whose title/body/tags match, best first."""
        with self.database.connect() as connection:
            # a bare user query is not FTS5 syntax: quote each token
            terms = [w.replace('"', '""') for w in query.split() if w.strip()]
            if not terms:
                return []
            safe = " OR ".join(f'"{w}"' for w in terms)
            rows = connection.execute(
                "SELECT entry_id FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (safe, limit),
            ).fetchall()
        return [row["entry_id"] for row in rows]
