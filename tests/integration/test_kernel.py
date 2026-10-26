import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.kernel import (
    ApprovalStateError,
    ApprovalStatus,
    ArtifactError,
    ArtifactKind,
    FindingSeverity,
    FindingStatus,
    PlanTask,
    RiskClass,
    StageGate,
    TaskStateError,
)
from samurai_ai.domain.operations import Operation, OperationMode, OperationStatus
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import (
    KernelPreconditionError,
    KernelService,
    PolicyViolationError,
)
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository


def make_service(database_path: Path) -> KernelService:
    database = Database(database_path)
    operations = OperationService(OperationRepository(database))
    return KernelService(operations, KernelRepository(database))


def make_operation(service: KernelService) -> Operation:
    return service.operations.create(
        "Authorized lab",
        OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("*.example.com",)),
        objective="Validate the kernel flow",
    )


def test_kernel_happy_path(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    service.transition_operation(operation.id, OperationStatus.PLANNING)

    plan = service.create_plan(
        operation.id,
        "Initial plan",
        tasks=(
            PlanTask(id="recon", title="Recon"),
            PlanTask(
                id="exploit",
                title="Exploit",
                risk_class=RiskClass.R2,
                depends_on=("recon",),
                required_artifacts=("scan-output",),
            ),
        ),
        gates=(
            StageGate(
                id="exec-ready",
                name="Execution readiness",
                required_artifacts=("recon-summary",),
            ),
        ),
    )
    assert plan.is_current
    service.approve_plan(operation.id)

    service.transition_operation(operation.id, OperationStatus.APPROVAL)
    service.transition_operation(operation.id, OperationStatus.EXECUTION)

    with pytest.raises(TaskStateError):
        service.start_task(operation.id, "exploit")

    service.start_task(operation.id, "recon")
    service.complete_task(operation.id, "recon")
    service.start_task(operation.id, "exploit")

    with pytest.raises(ArtifactError):
        service.complete_task(operation.id, "exploit")

    artifact = service.register_artifact(
        operation.id,
        ArtifactKind.COMMAND_OUTPUT,
        "nmap -sV 192.0.2.10",
        summary="Port scan",
        requirement="scan-output",
        task_id="exploit",
    )
    artifact_file = tmp_path / "artifacts" / operation.id / f"{artifact.id}.txt"
    assert artifact_file.read_text(encoding="utf-8") == "nmap -sV 192.0.2.10"
    assert artifact.path == f"artifacts/{operation.id}/{artifact.id}.txt"
    assert artifact.size_bytes == len(b"nmap -sV 192.0.2.10")

    service.complete_task(operation.id, "exploit")
    service.transition_operation(operation.id, OperationStatus.VERIFICATION)

    with pytest.raises(ArtifactError):
        service.pass_gate(operation.id, "exec-ready")

    service.register_artifact(
        operation.id,
        ArtifactKind.NOTE,
        "Recon finished",
        requirement="recon-summary",
        gate_id="exec-ready",
    )
    service.pass_gate(operation.id, "exec-ready")
    service.pass_gate(operation.id, "exec-ready")

    service.transition_operation(operation.id, OperationStatus.REPORTING)

    finding = service.create_finding(
        operation.id,
        "SQL injection in login form",
        severity=FindingSeverity.HIGH,
    )
    evidence = service.register_artifact(
        operation.id,
        ArtifactKind.LOG,
        "payload trace",
        finding_id=finding.id,
    )
    service.attach_evidence(finding.id, evidence.id)
    validated = service.validate_finding(finding.id)
    assert validated.status == FindingStatus.VALIDATED
    reported = service.mark_reported(finding.id)
    assert reported.status == FindingStatus.REPORTED

    service.transition_operation(operation.id, OperationStatus.COMPLETE)

    summary = service.summary(operation.id)
    assert summary.tasks_done == 2
    assert summary.tasks_total == 2
    assert summary.gates_passed == 1
    assert summary.gates_total == 1
    assert summary.pending_approvals == 0
    assert summary.artifacts == 3
    assert summary.findings == 1

    event_types = [event.event_type for event in service.operations.events(operation.id)]
    assert event_types == [
        "operation.created",
        "operation.status.changed",
        "plan.created",
        "plan.approved",
        "operation.status.changed",
        "operation.status.changed",
        "task.started",
        "task.completed",
        "task.started",
        "artifact.registered",
        "task.completed",
        "operation.status.changed",
        "artifact.registered",
        "gate.passed",
        "operation.status.changed",
        "finding.created",
        "artifact.registered",
        "finding.evidence-attached",
        "finding.validated",
        "finding.reported",
        "operation.status.changed",
    ]


def test_execution_is_blocked_by_pending_approval(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    service.transition_operation(operation.id, OperationStatus.PLANNING)
    service.create_plan(operation.id, "Plan", tasks=(), gates=())
    service.approve_plan(operation.id)
    service.transition_operation(operation.id, OperationStatus.APPROVAL)

    approval = service.request_approval(operation.id, "exploit smb share", RiskClass.R2)
    assert approval.status == ApprovalStatus.PENDING

    with pytest.raises(KernelPreconditionError, match="pending approvals"):
        service.transition_operation(operation.id, OperationStatus.EXECUTION)

    decided = service.approve(approval.id, decided_by="operator")
    assert decided.status == ApprovalStatus.APPROVED

    with pytest.raises(ApprovalStateError):
        service.reject(approval.id, decided_by="operator")

    service.transition_operation(operation.id, OperationStatus.EXECUTION)


def test_low_risk_actions_are_auto_approved(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)

    approval = service.request_approval(operation.id, "read banner", RiskClass.R1)
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.auto
    assert approval.decided_at is not None


def test_r4_actions_are_auto_rejected_and_denied(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)

    with pytest.raises(PolicyViolationError):
        service.request_approval(operation.id, "wipe domain controller", RiskClass.R4)

    approvals = service.kernel.list_approvals(operation.id)
    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.REJECTED
    assert approvals[0].auto
    event_types = [event.event_type for event in service.operations.events(operation.id)]
    assert "approval.auto-rejected" in event_types


def test_completion_is_blocked_by_candidate_finding(tmp_path: Path) -> None:
    service = make_service(tmp_path / "samurai.db")
    operation = make_operation(service)
    service.transition_operation(operation.id, OperationStatus.PLANNING)
    service.create_plan(operation.id, "Plan", tasks=(), gates=())
    service.approve_plan(operation.id)
    service.transition_operation(operation.id, OperationStatus.APPROVAL)
    service.transition_operation(operation.id, OperationStatus.EXECUTION)
    service.transition_operation(operation.id, OperationStatus.VERIFICATION)
    service.transition_operation(operation.id, OperationStatus.REPORTING)

    finding = service.create_finding(operation.id, "Unresolved lead")

    with pytest.raises(KernelPreconditionError, match="findings not resolved"):
        service.transition_operation(operation.id, OperationStatus.COMPLETE)

    resolved = service.mark_false_positive(finding.id)
    assert resolved.status == FindingStatus.FALSE_POSITIVE
    service.transition_operation(operation.id, OperationStatus.COMPLETE)


def test_plan_revision_and_state_survive_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    first = make_service(database_path)
    operation = make_operation(first)
    first.transition_operation(operation.id, OperationStatus.PLANNING)
    original = first.create_plan(
        operation.id,
        "Draft plan",
        tasks=(PlanTask(id="recon", title="Recon"),),
        gates=(),
    )

    second = make_service(database_path)
    restored = second.get_current_plan(operation.id)
    assert restored is not None
    assert restored.id == original.id
    assert restored.is_current

    revised = second.revise_plan(
        operation.id,
        "Revised plan",
        tasks=(PlanTask(id="recon-v2", title="Recon v2"),),
        gates=(StageGate(id="exec-ready", name="Execution readiness"),),
    )
    second.start_task(operation.id, "recon-v2")

    third = make_service(database_path)
    current = third.get_current_plan(operation.id)
    assert current is not None
    assert current.id == revised.id
    assert current.title == "Revised plan"

    plans = third.kernel.list_plans(operation.id)
    assert {plan.id for plan in plans} == {original.id, revised.id}
    superseded = third.kernel.get_plan(original.id)
    assert superseded is not None
    assert not superseded.is_current

    summary = third.summary(operation.id)
    assert summary.tasks_total == 1
    assert summary.tasks_done == 0
    assert summary.gates_total == 1


def test_artifacts_are_immutable_at_database_level(tmp_path: Path) -> None:
    database = Database(tmp_path / "samurai.db")
    service = KernelService(
        OperationService(OperationRepository(database)),
        KernelRepository(database),
    )
    operation = make_operation(service)
    artifact = service.register_artifact(operation.id, ArtifactKind.NOTE, "hello")

    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE artifacts SET kind = 'tampered' WHERE id = ?",
                (artifact.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM artifacts WHERE id = ?",
                (artifact.id,),
            )


def test_v1_database_migrates_to_v2(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    legacy = Operation(
        name="Legacy operation",
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
    connection.executescript(_MIGRATIONS[0][1])
    connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
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
        assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}
        tables = {
            row["name"]
            for row in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"plans", "approvals", "artifacts", "findings"} <= tables
        assert {"operation_snapshots", "recovery_actions", "app_settings"} <= tables

    repository = OperationRepository(database)
    restored = repository.get(legacy.id)
    assert restored is not None
    assert restored.name == "Legacy operation"
