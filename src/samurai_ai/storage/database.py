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

_MIGRATION_12 = """
CREATE TABLE IF NOT EXISTS plan_scenarios (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_cases (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS test_cases_operation
ON test_cases(operation_id, status);

CREATE TABLE IF NOT EXISTS runbooks (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS runbooks_no_update
BEFORE UPDATE ON runbooks
BEGIN
    SELECT RAISE(ABORT, 'runbooks are immutable');
END;

CREATE TRIGGER IF NOT EXISTS runbooks_no_delete
BEFORE DELETE ON runbooks
BEGIN
    SELECT RAISE(ABORT, 'runbooks are immutable');
END;
"""

_MIGRATION_13 = """
CREATE TABLE IF NOT EXISTS swarm_runs (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    workers INTEGER NOT NULL,
    task_ids TEXT NOT NULL,
    model_route TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS swarm_runs_operation_id
ON swarm_runs(operation_id, created_at, id);

ALTER TABLE agent_runs ADD COLUMN swarm_id TEXT;

ALTER TABLE agent_runs ADD COLUMN specialization TEXT NOT NULL DEFAULT 'general';

CREATE INDEX IF NOT EXISTS agent_runs_swarm_id
ON agent_runs(swarm_id);
"""

_MIGRATION_14 = """
CREATE TABLE IF NOT EXISTS campaign_memory (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT NOT NULL,
    retention_days INTEGER,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS campaign_memory_workspace
ON campaign_memory(workspace, updated_at, id);

CREATE TABLE IF NOT EXISTS custom_playbooks (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    data TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS custom_playbooks_no_update
BEFORE UPDATE ON custom_playbooks
BEGIN
    SELECT RAISE(ABORT, 'custom playbooks are immutable');
END;

CREATE TRIGGER IF NOT EXISTS custom_playbooks_no_delete
BEFORE DELETE ON custom_playbooks
BEGIN
    SELECT RAISE(ABORT, 'custom playbooks are immutable');
END;

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    trusted INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TRIGGER IF NOT EXISTS skills_no_update
BEFORE UPDATE ON skills
BEGIN
    SELECT RAISE(ABORT, 'skills are immutable');
END;

CREATE TRIGGER IF NOT EXISTS skills_no_delete
BEFORE DELETE ON skills
BEGIN
    SELECT RAISE(ABORT, 'skills are immutable');
END;

ALTER TABLE agent_runs ADD COLUMN workspace TEXT;

ALTER TABLE agent_runs ADD COLUMN skills TEXT NOT NULL DEFAULT '[]';
"""

_MIGRATION_15 = """
CREATE TABLE IF NOT EXISTS custom_models (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    context_limit INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    capabilities TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS custom_models_provider_id
ON custom_models(provider_id, id);
"""

_MIGRATION_16 = """
ALTER TABLE model_endpoints ADD COLUMN verify_tls INTEGER NOT NULL DEFAULT 1;
"""

_MIGRATION_17 = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS goals_operation_state
ON goals(operation_id, state, created_at);
"""

_MIGRATION_18 = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS chat_messages_operation_id
ON chat_messages(operation_id, id);
"""

_MIGRATION_19 = """
CREATE TABLE IF NOT EXISTS exploration_nodes (
    id             TEXT PRIMARY KEY,
    operation_id   TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    task_id        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    state          TEXT NOT NULL,
    payload        TEXT NOT NULL DEFAULT '{}',
    priority       INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    delete_reason  TEXT,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    completed_at   TEXT
);

CREATE INDEX IF NOT EXISTS exploration_nodes_task
ON exploration_nodes(task_id, kind, state);

CREATE INDEX IF NOT EXISTS exploration_nodes_operation
ON exploration_nodes(operation_id, created_at);

CREATE TABLE IF NOT EXISTS exploration_edges (
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    src_id       TEXT NOT NULL REFERENCES exploration_nodes(id) ON DELETE CASCADE,
    dst_id       TEXT NOT NULL REFERENCES exploration_nodes(id) ON DELETE CASCADE,
    rel          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (operation_id, src_id, rel, dst_id),
    CHECK (src_id <> dst_id)
);

CREATE INDEX IF NOT EXISTS exploration_edges_src ON exploration_edges(src_id, rel);
CREATE INDEX IF NOT EXISTS exploration_edges_dst ON exploration_edges(dst_id, rel);
"""

_MIGRATION_20 = """
CREATE TABLE IF NOT EXISTS lessons (
    id         TEXT PRIMARY KEY,
    workspace  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    cue        TEXT NOT NULL,
    action     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS lessons_identity ON lessons(workspace, kind, cue);

CREATE TABLE IF NOT EXISTS lesson_evidence (
    lesson_id   TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (lesson_id, source_kind, source_id)
);
"""

_MIGRATION_21 = """
CREATE TABLE IF NOT EXISTS task_constraints (
    id           TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('allow', 'deny')),
    text         TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS task_constraints_task ON task_constraints(task_id, kind);
"""

_MIGRATION_22 = """
CREATE TABLE IF NOT EXISTS skill_gaps (
    skill        TEXT NOT NULL,
    workspace    TEXT NOT NULL,
    misses       INTEGER NOT NULL DEFAULT 0,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (skill, workspace)
);
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
    (12, _MIGRATION_12),
    (13, _MIGRATION_13),
    (14, _MIGRATION_14),
    (15, _MIGRATION_15),
    (16, _MIGRATION_16),
    (17, _MIGRATION_17),
    (18, _MIGRATION_18),
    (19, _MIGRATION_19),
    (20, _MIGRATION_20),
    (21, _MIGRATION_21),
    (22, _MIGRATION_22),
)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            target_connection = sqlite3.connect(target)
            try:
                source.backup(target_connection)
            finally:
                target_connection.close()
        return target

    def pre_migration_backup(self, target_version: int) -> Path:
        """Keep a copy of the current database before migrating it forward.

        The copy is written beside the database (not into the data directory)
        so a restore is a plain file rename by the operator.
        """
        destination = self.path.with_name(
            f"{self.path.name}.pre-v{target_version}.bak"
        )
        return self.backup(destination)

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
            pending = [
                (version, script)
                for version, script in _MIGRATIONS
                if version not in applied
            ]
            if not pending:
                return
            if applied and self.path.exists():
                self.pre_migration_backup(pending[0][0])
            for version, script in pending:
                self._apply_migration(connection, version, script)

    @staticmethod
    def _apply_migration(
        connection: sqlite3.Connection, version: int, script: str
    ) -> None:
        """Run one migration and record it in the same transaction.

        ``executescript`` implicitly commits a pending transaction, so the
        BEGIN/COMMIT and the version row travel inside the script text: a
        failure halfway leaves neither schema changes nor a version row.
        """
        statement = (
            "BEGIN IMMEDIATE;\n"
            f"{script}\n"
            f"INSERT INTO schema_migrations(version) VALUES ({version});\n"
            "COMMIT;"
        )
        try:
            connection.executescript(statement)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

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
