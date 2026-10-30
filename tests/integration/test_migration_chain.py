"""Migration chain v1→current and backup/restore.

A hand-built v1 database (operation + journal event) must migrate through
every schema version in order with data intact, and an online backup must be
a byte-consistent, independently openable copy that restores the operation
when moved back into place.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.storage import database as database_module
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.operations import OperationRepository

CURRENT_VERSIONS = set(range(1, len(_MIGRATIONS) + 1))


def _build_v1_database(path: Path, operation: Operation) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_MIGRATIONS[0][1])
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
    connection.execute(
        """
        INSERT INTO operations(id, name, mode, status, data, version, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation.id,
            operation.name,
            operation.mode.value,
            operation.status.value,
            operation.model_dump_json(),
            operation.version,
            operation.created_at.isoformat(),
            operation.updated_at.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO operation_events(operation_id, event_type, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            operation.id,
            "operation.created",
            json.dumps({"name": operation.name}),
            operation.created_at.isoformat(),
        ),
    )
    connection.commit()
    connection.close()


def _operation() -> Operation:
    return Operation(
        name="Legacy chain operation",
        mode=OperationMode.BUG_BOUNTY,
        scope=ScopeContract(includes=("example.com",)),
    )


def test_v1_chain_migrates_to_current_with_data_intact(tmp_path: Path) -> None:
    database_path = tmp_path / "chain.db"
    operation = _operation()
    _build_v1_database(database_path, operation)

    database = Database(database_path)
    database.initialize()

    with database.connect() as connection:
        versions = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert versions == CURRENT_VERSIONS

    repository = OperationRepository(database)
    restored = repository.get(operation.id)
    assert restored is not None
    assert restored.name == operation.name
    events = repository.events(operation.id)
    assert [event.event_type for event in events] == ["operation.created"]


def test_backup_is_complete_and_restorable(tmp_path: Path) -> None:
    database_path = tmp_path / "live.db"
    operation = _operation()
    _build_v1_database(database_path, operation)
    database = Database(database_path)
    database.initialize()

    backup_path = database.backup(tmp_path / "backup" / "snapshot.db")
    assert backup_path.exists()

    backup = Database(backup_path)
    with backup.connect() as connection:
        versions = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert versions == CURRENT_VERSIONS
    repository = OperationRepository(backup)
    assert repository.get(operation.id) is not None
    assert [e.event_type for e in repository.events(operation.id)] == [
        "operation.created"
    ]

    # restore: remove the original, move the backup into place
    database_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(f"{database_path}{suffix}")
        if side.exists():
            side.unlink()
    shutil.copy(backup_path, database_path)
    restored_db = Database(database_path)
    restored_repo = OperationRepository(restored_db)
    restored = restored_repo.get(operation.id)
    assert restored is not None and restored.name == operation.name


def test_backup_after_writes_captures_latest_state(tmp_path: Path) -> None:
    database_path = tmp_path / "live.db"
    first = _operation()
    _build_v1_database(database_path, first)
    database = Database(database_path)
    repository = OperationRepository(database)
    assert repository.get(first.id) is not None

    from samurai_ai.domain.operations import OperationEvent

    second = Operation(
        name="Second operation",
        mode=OperationMode.CTF_HTB,
        scope=ScopeContract(includes=("example.org",)),
    )
    repository.add(
        second,
        OperationEvent(
            operation_id=second.id,
            event_type="operation.created",
            payload={"name": second.name},
        ),
    )
    backup_path = database.backup(tmp_path / "snapshot.db")
    backup_repo = OperationRepository(Database(backup_path))
    assert backup_repo.get(first.id) is not None
    assert backup_repo.get(second.id) is not None


def test_failed_migration_leaves_no_partial_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "partial.db"
    operation = _operation()
    _build_v1_database(database_path, operation)
    broken = (
        len(CURRENT_VERSIONS) + 1,
        """
CREATE TABLE broken_probe (id INTEGER PRIMARY KEY);
ALTER TABLE table_that_does_not_exist ADD COLUMN nope INTEGER NOT NULL;
""",
    )
    original = database_module._MIGRATIONS
    database_module._MIGRATIONS = (*original, broken)
    try:
        database = Database(database_path)
        with pytest.raises(sqlite3.OperationalError):
            database.initialize()

        with database.connect() as connection:
            versions = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            probe = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='broken_probe'"
            ).fetchone()
        # the version is not recorded and the partial DDL rolled back
        assert versions == CURRENT_VERSIONS
        assert probe is None

        # a corrected retry applies only the pending version
        database_module._MIGRATIONS = (
            *original,
            (broken[0], "CREATE TABLE broken_probe (id INTEGER PRIMARY KEY);"),
        )
        Database(database_path).initialize()
        with database.connect() as connection:
            versions = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            probe = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='broken_probe'"
            ).fetchone()
        assert versions == CURRENT_VERSIONS | {broken[0]}
        assert probe is not None
    finally:
        database_module._MIGRATIONS = original


def test_migration_keeps_a_pre_migration_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    operation = _operation()
    _build_v1_database(database_path, operation)

    Database(database_path).initialize()

    backup_path = database_path.with_name(
        f"{database_path.name}.pre-v{_MIGRATIONS[1][0]}.bak"
    )
    assert backup_path.exists()
    backup = Database(backup_path)
    with backup.connect() as connection:
        versions = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    # the copy is the pre-migration state, not a post-migration one
    assert versions == {1}
    assert OperationRepository(backup).get(operation.id) is not None


def test_initialize_is_idempotent_without_pending_migrations(tmp_path: Path) -> None:
    database_path = tmp_path / "settled.db"
    _build_v1_database(database_path, _operation())
    Database(database_path).initialize()
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1

    Database(database_path).initialize()
    assert list(tmp_path.glob("*.bak")) == backups
