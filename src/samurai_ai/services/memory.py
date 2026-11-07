"""Campaign memory service: operator-curated, workspace-scoped knowledge.

Memory is not operation evidence: entries are mutable, deletable and
retention-bounded by design (FR-17). Agents get read-only access through
bounded, redacted digests — memory can never be written by a model, which
keeps prompt injection from poisoning long-term knowledge.
"""

from __future__ import annotations

from collections.abc import Sequence

from samurai_ai.domain.execution import SecretRedactor
from samurai_ai.domain.memory import (
    DIGEST_MAX_BODY_CHARS,
    DIGEST_MAX_ENTRIES,
    MemoryEntry,
    MemoryKind,
    MemoryNotFoundError,
)
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.memory import MemoryRepository, MemorySearchRepository


class CampaignMemoryService:
    def __init__(
        self,
        memory: MemoryRepository,
        vault: VaultService | None = None,
        search: MemorySearchRepository | None = None,
    ) -> None:
        self.memory = memory
        self.vault = vault
        self.search = search

    def add(
        self,
        workspace: str,
        title: str,
        body: str = "",
        *,
        kind: MemoryKind = MemoryKind.NOTE,
        tags: Sequence[str] = (),
        retention_days: int | None = None,
        created_by: str = "operator",
    ) -> MemoryEntry:
        entry = MemoryEntry(
            workspace=workspace,
            kind=kind,
            title=title,
            body=body,
            tags=tuple(tags),
            retention_days=retention_days,
            created_by=created_by,
        )
        self.memory.add(entry)
        if self.search is not None:
            self.search.index(entry)
        return entry

    def get(self, entry_id: str) -> MemoryEntry:
        entry = self.memory.get(entry_id)
        if entry is None:
            raise MemoryNotFoundError(f"memory entry not found: {entry_id}")
        return entry

    def list_entries(self, workspace: str | None = None) -> list[MemoryEntry]:
        return self.memory.list_entries(workspace)

    def list_workspaces(self) -> list[str]:
        return self.memory.list_workspaces()

    def update(
        self,
        entry_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        kind: MemoryKind | None = None,
        tags: Sequence[str] | None = None,
        retention_days: int | None = None,
        clear_retention: bool = False,
    ) -> MemoryEntry:
        entry = self.get(entry_id)
        expected_version = entry.version
        if title is not None:
            entry.title = title
        if body is not None:
            entry.body = body
        if kind is not None:
            entry.kind = kind
        if tags is not None:
            entry.tags = tuple(tags)
        if clear_retention:
            entry.retention_days = None
        elif retention_days is not None:
            entry.retention_days = retention_days
        entry.touch()
        self.memory.update(entry, expected_version)
        return entry

    def delete(self, entry_id: str) -> None:
        if not self.memory.delete(entry_id):
            raise MemoryNotFoundError(f"memory entry not found: {entry_id}")

    def purge_expired(self) -> list[str]:
        return self.memory.purge_expired()

    def recall(self, query: str, limit: int = 8) -> list[MemoryEntry]:
        """Past entries that answer the query, best first; empty without a match."""
        if self.search is None:
            return []
        entries = []
        for entry_id in self.search.search(query, limit):
            entry = self.memory.get(entry_id)
            if entry is not None:
                entries.append(entry)
        return entries

    def digest(self, workspace: str) -> tuple[str, tuple[str, ...]]:
        """Bounded, redacted read-only view for agent prompts.

        Returns the digest text and the ids of the entries included (for the
        run-start audit record).
        """
        entries = [
            entry
            for entry in self.memory.list_entries(workspace)
            if not entry.is_expired()
        ][:DIGEST_MAX_ENTRIES]
        if not entries:
            return "", ()
        redactor = self._redactor()
        lines = []
        for entry in entries:
            body = entry.body
            if len(body) > DIGEST_MAX_BODY_CHARS:
                body = body[:DIGEST_MAX_BODY_CHARS] + "…"
            line = f"- [{entry.kind.value}] {entry.title}"
            if body:
                line += f": {body}"
            lines.append(redactor.redact(line).text)
        return "\n".join(lines), tuple(entry.id for entry in entries)

    def _redactor(self) -> SecretRedactor:
        redactor = SecretRedactor()
        if self.vault is not None:
            for metadata in self.vault.list_metadata(None):
                redactor.register(self.vault.get(metadata.name))
        return redactor
