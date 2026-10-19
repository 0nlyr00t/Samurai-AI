import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS operation_events_operation_id
ON operation_events(operation_id, id);

CREATE TRIGGER IF NOT EXISTS operation_events_no_update
BEFORE UPDATE ON operation_events
BEGIN
    SELECT RAISE(ABORT, 'operation events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS operation_events_no_delete
BEFORE DELETE ON operation_events
BEGIN
    SELECT RAISE(ABORT, 'operation events are append-only');
END;
"""

_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    approved INTEGER NOT NULL,
    is_current INTEGER NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS plans_operation_id
ON plans(operation_id, created_at, id);

CREATE UNIQUE INDEX IF NOT EXISTS plans_single_current
ON plans(operation_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    task_id TEXT,
    risk_class TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS approvals_operation_id
ON approvals(operation_id, status, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    requirement TEXT,
    task_id TEXT,
    gate_id TEXT,
    finding_id TEXT,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS artifacts_operation_id
ON artifacts(operation_id, id);

CREATE INDEX IF NOT EXISTS artifacts_task_requirement
ON artifacts(operation_id, task_id, requirement);

CREATE INDEX IF NOT EXISTS artifacts_gate_requirement
ON artifacts(operation_id, gate_id, requirement);

CREATE INDEX IF NOT EXISTS artifacts_finding_id
ON artifacts(finding_id);

CREATE TRIGGER IF NOT EXISTS artifacts_no_update
BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifacts_no_delete
BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;

CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    duplicate_of TEXT,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS findings_operation_id
ON findings(operation_id, status, id);
"""

_MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _MIGRATION_1), (2, _MIGRATION_2))


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, script in _MIGRATIONS:
                if version not in applied:
                    connection.executescript(script)
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                        (version,),
                    )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
