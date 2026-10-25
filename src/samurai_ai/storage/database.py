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

_MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS operation_snapshots (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS operation_snapshots_operation_id
ON operation_snapshots(operation_id, created_at, id);

CREATE TRIGGER IF NOT EXISTS operation_snapshots_no_update
BEFORE UPDATE ON operation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'operation snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS operation_snapshots_no_delete
BEFORE DELETE ON operation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'operation snapshots are immutable');
END;

CREATE TABLE IF NOT EXISTS recovery_actions (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    task_id TEXT,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS recovery_actions_operation_id
ON recovery_actions(operation_id, id);

CREATE INDEX IF NOT EXISTS recovery_actions_operation_status
ON recovery_actions(operation_id, status, id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS tool_runs (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    task_id TEXT,
    tool_id TEXT NOT NULL,
    tool_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    recovery_action_id TEXT,
    approval_id TEXT,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS tool_runs_operation_id
ON tool_runs(operation_id, created_at, id);

CREATE INDEX IF NOT EXISTS tool_runs_operation_status
ON tool_runs(operation_id, status, id);

CREATE INDEX IF NOT EXISTS tool_runs_tool
ON tool_runs(tool_id, tool_version);

CREATE INDEX IF NOT EXISTS tool_runs_recovery_action
ON tool_runs(recovery_action_id);
"""

_MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS secret_entries (
    id TEXT PRIMARY KEY,
    operation_id TEXT,
    name TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    nonce BLOB NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(operation_id, name)
);

CREATE INDEX IF NOT EXISTS secret_entries_operation_id
ON secret_entries(operation_id, name);
"""

_MIGRATION_6 = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    transport TEXT NOT NULL,
    command TEXT,
    url TEXT,
    trust_level TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
"""

_MIGRATION_7 = """
CREATE TABLE IF NOT EXISTS model_endpoints (
    id TEXT PRIMARY KEY,
    api_family TEXT NOT NULL,
    base_url TEXT NOT NULL,
    secret_name TEXT,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
"""

_MIGRATION_8 = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    parent_run_id TEXT,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL,
    model_route TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS agent_runs_operation_id
ON agent_runs(operation_id, created_at, id);
"""

_MIGRATION_9 = """
CREATE TABLE IF NOT EXISTS program_rules (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_ledger_entries (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(operation_id, kind, value)
);

CREATE TRIGGER IF NOT EXISTS asset_ledger_entries_no_update
BEFORE UPDATE ON asset_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'asset ledger entries are immutable');
END;

CREATE TRIGGER IF NOT EXISTS asset_ledger_entries_no_delete
BEFORE DELETE ON asset_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'asset ledger entries are immutable');
END;

CREATE TABLE IF NOT EXISTS asset_retirements (
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, kind, value)
);

CREATE TRIGGER IF NOT EXISTS asset_retirements_no_update
BEFORE UPDATE ON asset_retirements
BEGIN
    SELECT RAISE(ABORT, 'asset retirements are immutable');
END;

CREATE TRIGGER IF NOT EXISTS asset_retirements_no_delete
BEFORE DELETE ON asset_retirements
BEGIN
    SELECT RAISE(ABORT, 'asset retirements are immutable');
END;

CREATE TABLE IF NOT EXISTS finding_fingerprints (
    finding_id TEXT PRIMARY KEY REFERENCES findings(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    asset TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS finding_fingerprints_lookup
ON finding_fingerprints(operation_id, fingerprint);

CREATE TRIGGER IF NOT EXISTS finding_fingerprints_no_update
BEFORE UPDATE ON finding_fingerprints
BEGIN
    SELECT RAISE(ABORT, 'finding fingerprints are immutable');
END;

CREATE TRIGGER IF NOT EXISTS finding_fingerprints_no_delete
BEFORE DELETE ON finding_fingerprints
BEGIN
    SELECT RAISE(ABORT, 'finding fingerprints are immutable');
END;

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS reports_no_update
BEFORE UPDATE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports are immutable');
END;

CREATE TRIGGER IF NOT EXISTS reports_no_delete
BEFORE DELETE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports are immutable');
END;
"""

_MIGRATION_10 = """
CREATE TABLE IF NOT EXISTS ctf_challenges (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS hypotheses_operation
ON hypotheses(operation_id, status);

CREATE TABLE IF NOT EXISTS flag_ledger (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(operation_id, name)
);

CREATE TRIGGER IF NOT EXISTS flag_ledger_no_update
BEFORE UPDATE ON flag_ledger
BEGIN
    SELECT RAISE(ABORT, 'flag ledger entries are immutable');
END;

CREATE TRIGGER IF NOT EXISTS flag_ledger_no_delete
BEFORE DELETE ON flag_ledger
BEGIN
    SELECT RAISE(ABORT, 'flag ledger entries are immutable');
END;

CREATE TABLE IF NOT EXISTS ctf_writeups (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS ctf_writeups_no_update
BEFORE UPDATE ON ctf_writeups
BEGIN
    SELECT RAISE(ABORT, 'ctf writeups are immutable');
END;

CREATE TRIGGER IF NOT EXISTS ctf_writeups_no_delete
BEFORE DELETE ON ctf_writeups
BEGIN
    SELECT RAISE(ABORT, 'ctf writeups are immutable');
END;
"""

_MIGRATION_11 = """
CREATE TABLE IF NOT EXISTS red_team_roe (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attack_steps (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS attack_steps_operation
ON attack_steps(operation_id, status);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debriefs (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS debriefs_no_update
BEFORE UPDATE ON debriefs
BEGIN
    SELECT RAISE(ABORT, 'debriefs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS debriefs_no_delete
BEFORE DELETE ON debriefs
BEGIN
    SELECT RAISE(ABORT, 'debriefs are immutable');
END;
"""

_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
    (3, _MIGRATION_3),
    (4, _MIGRATION_4),
    (5, _MIGRATION_5),
    (6, _MIGRATION_6),
    (7, _MIGRATION_7),
    (8, _MIGRATION_8),
    (9, _MIGRATION_9),
    (10, _MIGRATION_10),
    (11, _MIGRATION_11),
)


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
