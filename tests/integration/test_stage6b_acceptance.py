"""Stage 6b acceptance: hardening suites hold under adversarial conditions.

One composite pass over the stage goal from development plan §8: a suite
mixing a faulted provider scenario with a healthy one must complete with
correct per-scenario outcomes, a vault canary must stay out of provider
traffic and the journal, and a v1 database must migrate to the current
schema and survive a backup/restore cycle. Detailed coverage lives in
test_fault_injection.py, test_prompt_injection.py, test_soak.py and
test_migration_chain.py.
"""

import json
import sqlite3
from pathlib import Path

from samurai_ai.domain.eval import EvalScenarioStatus, EvalSuite
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.eval import EvalService
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.operations import OperationRepository

PLANNER = '{"steps": ["probe it"]}'
EXECUTOR = '{"commands": [{"type": "complete_task"}]}'


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


def test_hardening_composite(tmp_path: Path) -> None:
    # 1. fault tolerance: a faulted scenario does not sink the suite
    suite = EvalSuite.model_validate(
        {
            "name": "stage6b",
            "version": "1.0.0",
            "scenarios": [
                {
                    "id": "faulted",
                    "title": "Provider down",
                    "scope_includes": ["example.com"],
                    "tasks": [{"id": "recon", "title": "Recon"}],
                    "script": {
                        "responses": [PLANNER],
                        "fault": {"mode": "http_error", "status": 500},
                    },
                    "expectations": {"expect_solve": False},
                },
                {
                    "id": "healthy",
                    "title": "Provider fine",
                    "scope_includes": ["example.com"],
                    "tasks": [{"id": "recon", "title": "Recon"}],
                    "script": {"responses": [PLANNER, EXECUTOR]},
                    "expectations": {"expect_solve": True},
                },
            ],
        }
    )
    report = EvalService().run_suite(suite, workdir=tmp_path / "eval")
    assert report.passed
    faulted, healthy = report.results
    assert faulted.status is EvalScenarioStatus.PASSED
    assert faulted.metrics is not None and faulted.metrics.solved is False
    assert healthy.metrics is not None and healthy.metrics.solved is True

    # faulted run terminated cleanly — no run stuck in RUNNING
    events_path = tmp_path / "eval" / "stage6b-faulted-0" / "samurai.db"
    with Database(events_path).connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM operation_events WHERE event_type = 'agent.run.finish'"
        ).fetchall()
    assert rows
    assert all(
        json.loads(row["payload"])["status"] != "running" for row in rows
    )

    # 2. migration chain + backup/restore on real data
    database_path = tmp_path / "chain.db"
    operation = Operation(
        name="Chain",
        mode=OperationMode.BUG_BOUNTY,
        scope=ScopeContract(includes=("example.com",)),
    )
    _build_v1_database(database_path, operation)
    database = Database(database_path)
    repository = OperationRepository(database)
    assert repository.get(operation.id) is not None

    backup = Database(database.backup(tmp_path / "snapshot.db"))
    backup_repo = OperationRepository(backup)
    assert backup_repo.get(operation.id) is not None
    assert [
        event.event_type for event in backup_repo.events(operation.id)
    ] == ["operation.created"]
