import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import (
    ApprovalStatus,
    ArtifactKind,
    PlanTask,
    RiskClass,
    StageGate,
    TaskStatus,
)
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.recovery import RecoveryActionStatus
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService, SnapshotNotFoundError
from samurai_ai.services.settings import InvalidLocaleError, SettingsService
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository


def make_stack(
    database_path: Path,
) -> tuple[OperationService, KernelService, SettingsService, RecoveryService]:
    database = Database(database_path)
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
    recovery = RecoveryService(service, kernel, recovery_repository)
    return service, kernel, settings, recovery


def make_operation(service: OperationService, name: str = "Recovery lab") -> Operation:
    return service.create(
        name,
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",), excludes=("api.example.com",)),
        objective="Exercise recovery",
    )


def populate_kernel(kernel: KernelService, operation_id: str) -> None:
    kernel.create_plan(
        operation_id,
        "Recovery plan",
        [
            PlanTask(id="recon", title="Map services", risk_class=RiskClass.R1),
            PlanTask(id="enum", title="Enumerate", depends_on=("recon",)),
        ],
        [StageGate(id="recon-done", name="Recon done")],
    )


def test_migration_v3_creates_recovery_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh.db")
    database.initialize()
    with database.connect() as connection:
        versions = {
            row["version"] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"operation_snapshots", "recovery_actions", "app_settings"} <= tables
        indexes = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "recovery_actions_operation_id" in indexes
        assert "recovery_actions_operation_status" in indexes


def test_v2_database_migrates_to_v3(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    legacy = Operation(
        name="Legacy v2",
        mode=OperationMode.CTF_HTB,
        scope=ScopeContract(includes=("example.com",)),
    )
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for version, script in _MIGRATIONS[:2]:
        connection.executescript(script)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    connection.execute(
        """
        INSERT INTO operations(id, name, mode, status, data, version, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            legacy.id,
            legacy.name,
            legacy.mode.value,
            legacy.status.value,
            legacy.model_dump_json(),
            legacy.version,
            legacy.created_at.isoformat(),
            legacy.updated_at.isoformat(),
        ),
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    database.initialize()
    with database.connect() as migrated:
        versions = {
            row["version"] for row in migrated.execute("SELECT version FROM schema_migrations")
        }
        assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
        tables = {
            row["name"]
            for row in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"operation_snapshots", "recovery_actions", "app_settings"} <= tables
    assert OperationRepository(database).get(legacy.id) is not None


def test_snapshots_are_immutable_at_database_level(tmp_path: Path) -> None:
    database_path = tmp_path / "immutable.db"
    service, _, _, recovery = make_stack(database_path)
    operation = make_operation(service)
    snapshot = recovery.create_snapshot(operation.id, label="baseline")

    database = Database(database_path)
    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE operation_snapshots SET label = 'tampered' WHERE id = ?",
                (snapshot.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM operation_snapshots WHERE id = ?",
                (snapshot.id,),
            )


def test_settings_persist_and_env_fallback(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "settings.db"
    monkeypatch.delenv("SAMURAI_LANG", raising=False)
    _, _, settings, _ = make_stack(database_path)
    assert settings.get_locale() is None

    monkeypatch.setenv("SAMURAI_LANG", "ru")
    _, _, settings, _ = make_stack(database_path)
    assert settings.get_locale() == "ru"

    settings.set_locale("en")
    _, _, reopened, _ = make_stack(database_path)
    assert reopened.get_locale() == "en"

    with pytest.raises(InvalidLocaleError):
        reopened.set_locale("de")
    assert reopened.get_locale() == "en"


def test_snapshot_captures_metadata_without_artifact_content(tmp_path: Path) -> None:
    service, kernel, _, recovery = make_stack(tmp_path / "snapshot.db")
    operation = make_operation(service)
    populate_kernel(kernel, operation.id)
    artifact = kernel.register_artifact(
        operation.id, ArtifactKind.NOTE, "secret scanner output", summary="raw output"
    )
    finding = kernel.create_finding(operation.id, "Reflected XSS")
    approval = kernel.request_approval(operation.id, "Active probe", RiskClass.R2)

    snapshot = recovery.create_snapshot(operation.id, label="pre-engagement")
    assert snapshot.operation_id == operation.id
    assert snapshot.label == "pre-engagement"
    data = snapshot.data
    assert data["operation"]["id"] == operation.id
    assert data["plan"]["tasks"][0]["id"] == "recon"
    assert [item["id"] for item in data["approvals"]] == [approval.id]
    assert [item["id"] for item in data["findings"]] == [finding.id]
    assert [item["id"] for item in data["artifacts"]] == [artifact.id]
    assert data["artifacts"][0]["sha256"] == artifact.sha256
    assert "secret scanner output" not in str(data["artifacts"])

    with pytest.raises(SnapshotNotFoundError):
        recovery.get_snapshot("snap_missing")


def test_restore_snapshot_creates_fresh_operation(tmp_path: Path) -> None:
    service, kernel, _, recovery = make_stack(tmp_path / "restore.db")
    operation = make_operation(service)
    populate_kernel(kernel, operation.id)
    kernel.approve_plan(operation.id)
    kernel.start_task(operation.id, "recon")
    kernel.register_artifact(operation.id, ArtifactKind.NOTE, "evidence")
    kernel.create_finding(operation.id, "Finding")
    kernel.request_approval(operation.id, "Active probe", RiskClass.R2)
    snapshot = recovery.create_snapshot(operation.id)

    restored = recovery.restore_snapshot(snapshot.id)
    assert restored.id != operation.id
    assert restored.name == f"{operation.name} (restored)"
    assert restored.status.value == "draft"
    assert restored.scope.includes == operation.scope.includes

    plan = kernel.get_current_plan(restored.id)
    assert plan is not None
    assert not plan.approved
    assert all(task.status == TaskStatus.PENDING for task in plan.tasks)
    assert all(gate.status.value == "pending" for gate in plan.gates)
    assert kernel.kernel.list_artifacts(restored.id) == []
    assert kernel.kernel.list_findings(restored.id) == []
    assert kernel.kernel.list_approvals(restored.id) == []

    original_plan = kernel.get_current_plan(operation.id)
    assert original_plan is not None
    assert original_plan.approved
    assert original_plan.task_by_id("recon").status == TaskStatus.IN_PROGRESS  # type: ignore[union-attr]

    event_types = [event.event_type for event in service.events(restored.id)]
    assert event_types == ["operation.restored", "plan.restored"]


def test_fork_operation_resets_progress_without_artifacts(tmp_path: Path) -> None:
    service, kernel, _, recovery = make_stack(tmp_path / "fork.db")
    operation = make_operation(service)
    populate_kernel(kernel, operation.id)
    kernel.approve_plan(operation.id)
    kernel.start_task(operation.id, "recon")
    kernel.register_artifact(operation.id, ArtifactKind.LOG, "log content")
    kernel.create_finding(operation.id, "Candidate")
    kernel.request_approval(operation.id, "Active probe", RiskClass.R2)

    forked = recovery.fork_operation(operation.id, name="Fork lab")
    assert forked.id != operation.id
    assert forked.name == "Fork lab"

    plan = kernel.get_current_plan(forked.id)
    assert plan is not None
    assert not plan.approved
    assert all(task.status == TaskStatus.PENDING for task in plan.tasks)
    assert kernel.kernel.list_artifacts(forked.id) == []
    assert kernel.kernel.list_findings(forked.id) == []
    assert kernel.kernel.list_approvals(forked.id) == []

    source_plan = kernel.get_current_plan(operation.id)
    assert source_plan is not None
    assert source_plan.task_by_id("recon").status == TaskStatus.IN_PROGRESS  # type: ignore[union-attr]
    assert [event.event_type for event in service.events(forked.id)] == [
        "operation.forked",
        "plan.forked",
    ]


def test_reconcile_marks_in_flight_unknown_without_retry(tmp_path: Path) -> None:
    service, kernel, _, recovery = make_stack(tmp_path / "reconcile.db")
    operation = make_operation(service)
    populate_kernel(kernel, operation.id)
    kernel.start_task(operation.id, "recon")
    approval = kernel.request_approval(operation.id, "Active probe", RiskClass.R2)
    finished = recovery.begin_action(operation.id, "tool-run", task_id="recon")
    stuck = recovery.begin_action(operation.id, "agent-step", description="interrupted")
    recovery.complete_action(finished.id)

    report = recovery.reconcile_operation(operation.id, actor="operator")
    assert report.actions_marked_unknown == (stuck.id,)
    assert report.resumable_tasks == ("recon",)
    assert report.pending_approvals == (approval.id,)

    actions = {
        action.id: action.status
        for action in recovery.recovery.list_actions(operation.id)
    }
    assert actions[stuck.id] == RecoveryActionStatus.UNKNOWN
    assert actions[finished.id] == RecoveryActionStatus.COMPLETED
    assert kernel.kernel.get_approval(approval.id).status == ApprovalStatus.PENDING  # type: ignore[union-attr]

    second = recovery.reconcile_operation(operation.id)
    assert second.actions_marked_unknown == ()
    assert second.resumable_tasks == ("recon",)

    event_types = [event.event_type for event in service.events(operation.id)]
    assert "recovery.action.started" in event_types
    assert "recovery.action.completed" in event_types
    assert event_types.count("recovery.reconciled") == 2


def test_fail_action_records_event(tmp_path: Path) -> None:
    service, _, _, recovery = make_stack(tmp_path / "fail.db")
    operation = make_operation(service)
    action = recovery.begin_action(operation.id, "tool-run")
    failed = recovery.fail_action(action.id, reason="timeout")
    assert failed.status == RecoveryActionStatus.FAILED
    assert failed.version == 2
    events = service.events(operation.id)
    assert events[-1].event_type == "recovery.action.failed"
    assert events[-1].payload["reason"] == "timeout"
