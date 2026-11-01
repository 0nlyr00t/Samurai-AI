import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import AgentSpecialization, SwarmRun, SwarmStatus
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.swarm import SwarmRunConflictError, SwarmRunRepository


def make_operation(database_path: Path) -> str:
    service = OperationService(OperationRepository(Database(database_path)))
    operation = service.create(
        "Lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )
    return operation.id


def test_swarm_run_roundtrip_and_finish_guard(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = SwarmRunRepository(Database(database_path))
    swarm = SwarmRun(
        operation_id=operation_id,
        workers=2,
        task_ids=("recon", "testing"),
        model_route=("fake/agent@v1",),
    )
    repository.add(swarm)

    restored = SwarmRunRepository(Database(database_path)).get(swarm.id)
    assert restored is not None
    assert restored.operation_id == operation_id
    assert restored.workers == 2
    assert restored.task_ids == ("recon", "testing")
    assert restored.model_route == ("fake/agent@v1",)
    assert restored.status is SwarmStatus.RUNNING

    restored.finish(SwarmStatus.COMPLETED)
    repository.finish(restored)
    finished = repository.get(swarm.id)
    assert finished is not None
    assert finished.status is SwarmStatus.COMPLETED
    assert finished.finished_at is not None

    with pytest.raises(SwarmRunConflictError):
        repository.finish(finished)


def test_swarm_listing_ordered(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = SwarmRunRepository(Database(database_path))
    first = SwarmRun(
        operation_id=operation_id, workers=1, task_ids=("t1",), model_route=("m@v1",)
    )
    second = SwarmRun(
        operation_id=operation_id, workers=2, task_ids=("t2", "t3"), model_route=("m@v1",)
    )
    repository.add(first)
    repository.add(second)
    assert [swarm.id for swarm in repository.list_swarms(operation_id)] == [
        first.id,
        second.id,
    ]
    assert repository.get("swarm_missing") is None


def test_agent_run_swarm_columns_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = AgentRunRepository(Database(database_path))
    member = AgentRun(
        operation_id=operation_id,
        task_id="recon",
        model_route=("fake/agent@v1",),
        swarm_id="swarm_x1",
        specialization=AgentSpecialization.RECON,
    )
    plain = AgentRun(
        operation_id=operation_id,
        task_id="solo",
        model_route=("fake/agent@v1",),
    )
    repository.add(member)
    repository.add(plain)

    restored = repository.get(member.id)
    assert restored is not None
    assert restored.swarm_id == "swarm_x1"
    assert restored.specialization is AgentSpecialization.RECON

    restored_plain = repository.get(plain.id)
    assert restored_plain is not None
    assert restored_plain.swarm_id is None
    assert restored_plain.specialization is AgentSpecialization.GENERAL

    members = repository.list_swarm_members("swarm_x1")
    assert [run.id for run in members] == [member.id]


def test_migration_13_applies(tmp_path: Path) -> None:
    database = Database(tmp_path / "samurai.db")
    database.initialize()
    with database.connect() as connection:
        versions = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_runs)")
        }
    assert versions == set(range(1, len(_MIGRATIONS) + 1))
    assert {"swarm_id", "specialization"} <= columns


def test_migration_13_from_v12_database(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    database = Database(database_path)
    database.initialize()
    with database.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version >= 13")
        connection.execute("DROP TABLE swarm_runs")
        connection.execute("DROP TABLE IF EXISTS custom_models")
        # v12 не знает поздних таблиц: без их удаления CREATE ... IF NOT EXISTS
        # переживает откат, а ALTER из v25 упирается в уже добавленную колонку
        connection.execute("DROP TABLE IF EXISTS finding_retests")
        connection.execute("DROP TABLE IF EXISTS finding_traffic")
        connection.execute("DROP TABLE IF EXISTS traffic_pairs")
        connection.execute("ALTER TABLE model_endpoints DROP COLUMN verify_tls")
        connection.execute(
            """
            CREATE TABLE agent_runs_v8 AS
            SELECT id, operation_id, task_id, parent_run_id, depth, status,
                   model_route, created_at, created_by, finished_at
            FROM agent_runs
            """
        )
        connection.execute("DROP TABLE agent_runs")
        connection.execute("ALTER TABLE agent_runs_v8 RENAME TO agent_runs")
    database.initialize()
    with database.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(agent_runs)")
        }
        swarm_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='swarm_runs'"
        ).fetchall()
    assert {"swarm_id", "specialization", "workspace", "skills"} <= columns
    assert len(swarm_tables) == 1
    # существующие строки сохраняют дефолты
    operation_id = make_operation(database_path)
    legacy = AgentRun(operation_id=operation_id, task_id="t1", model_route=("m@v1",))
    AgentRunRepository(Database(database_path)).add(legacy)
    with sqlite3.connect(database_path) as raw:
        raw.execute("PRAGMA foreign_keys = ON")
        row = raw.execute(
            "SELECT swarm_id, specialization, workspace, skills "
            "FROM agent_runs WHERE id = ?",
            (legacy.id,),
        ).fetchone()
    assert row == (None, "general", None, "[]")
