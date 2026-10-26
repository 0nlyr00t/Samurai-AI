from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from samurai_ai.domain.memory import MemoryEntry, MemoryKind
from samurai_ai.domain.skills import Skill
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.memory import MemoryConflictError, MemoryRepository
from samurai_ai.storage.skills import SkillRepository


def make_entry(workspace: str = "acme-bb", **kwargs: object) -> MemoryEntry:
    kwargs.setdefault("title", "Baseline")
    return MemoryEntry(workspace=workspace, **kwargs)  # type: ignore[arg-type]


def test_memory_crud_and_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    repository = MemoryRepository(Database(database_path))
    entry = make_entry(kind=MemoryKind.METHODOLOGY, body="enumerate first", tags=("recon",))
    repository.add(entry)

    restored = MemoryRepository(Database(database_path)).get(entry.id)
    assert restored is not None
    assert restored.workspace == "acme-bb"
    assert restored.kind is MemoryKind.METHODOLOGY
    assert restored.tags == ("recon",)
    assert restored.version == 1

    restored.body = "enumerate, then fingerprint"
    restored.touch()
    repository.update(restored, expected_version=1)
    updated = repository.get(entry.id)
    assert updated is not None
    assert updated.body == "enumerate, then fingerprint"
    assert updated.version == 2

    with pytest.raises(MemoryConflictError):
        repository.update(updated, expected_version=1)


def test_memory_delete_and_workspace_isolation(tmp_path: Path) -> None:
    repository = MemoryRepository(Database(tmp_path / "samurai.db"))
    first = make_entry(workspace="ws-a", title="A")
    second = make_entry(workspace="ws-b", title="B")
    repository.add(first)
    repository.add(second)

    assert [e.id for e in repository.list_entries("ws-a")] == [first.id]
    assert sorted(repository.list_workspaces()) == ["ws-a", "ws-b"]

    assert repository.delete(first.id)
    assert repository.get(first.id) is None
    assert not repository.delete(first.id)
    assert [e.id for e in repository.list_entries()] == [second.id]


def test_memory_purge_expired(tmp_path: Path) -> None:
    repository = MemoryRepository(Database(tmp_path / "samurai.db"))
    old = make_entry(title="Old", retention_days=7)
    old.updated_at = datetime.now(UTC) - timedelta(days=10)
    repository.add(old)
    fresh = make_entry(title="Fresh", retention_days=7)
    repository.add(fresh)
    permanent = make_entry(title="Permanent")
    repository.add(permanent)

    purged = repository.purge_expired()
    assert purged == [old.id]
    remaining = {entry.title for entry in repository.list_entries()}
    assert remaining == {"Fresh", "Permanent"}
    assert repository.purge_expired() == []


def test_skills_versioned_immutable(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    repository = SkillRepository(Database(database_path))
    v1 = Skill(name="web-recon", body="v1 body", sha256="ab" * 32, trusted=True)
    repository.add(v1)
    assert repository.next_version("web-recon") == 2
    v2 = Skill(
        name="web-recon", version=2, body="v2 body", sha256="cd" * 32, trusted=True
    )
    repository.add(v2)

    latest = repository.latest("web-recon")
    assert latest is not None
    assert latest.version == 2
    assert latest.body == "v2 body"
    assert [s.version for s in repository.list_versions("web-recon")] == [1, 2]

    other = Skill(name="reporting", body="other", sha256="ef" * 32, trusted=True)
    repository.add(other)
    names = [skill.name for skill in repository.list_latest()]
    assert names == ["reporting", "web-recon"]

    # immutability enforced by triggers
    import sqlite3

    with sqlite3.connect(database_path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE skills SET body = 'x' WHERE id = ?", (v1.id,))
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM skills WHERE id = ?", (v1.id,))


def test_migration_14_applies(tmp_path: Path) -> None:
    database = Database(tmp_path / "samurai.db")
    database.initialize()
    with database.connect() as connection:
        versions = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(agent_runs)")
        }
    assert versions == set(range(1, len(_MIGRATIONS) + 1))
    assert {"workspace", "skills"} <= columns
