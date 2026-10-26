import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.planning import (
    KillChainPhase,
    PlannedTest,
    PlanScenario,
    Runbook,
)
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.planning import PlanningConflictError, PlanningRepository


def make_operation(database_path: Path) -> str:
    service = OperationService(OperationRepository(Database(database_path)))
    operation = service.create(
        "Plan",
        OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("lab.local",)),
    )
    return operation.id


def make_event(operation_id: str, event_type: str = "test.event") -> OperationEvent:
    return OperationEvent(operation_id=operation_id, event_type=event_type, payload={})


def test_scenario_survives_reopen_and_conflict_detection(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = PlanningRepository(Database(database_path))
    scenario = PlanScenario(
        operation_id=operation_id, name="Q3 emulation", schedule="weekdays"
    )
    repository.save_scenario(scenario, None, make_event(operation_id, "scenario.set"))

    reopened = PlanningRepository(Database(database_path))
    restored = reopened.get_scenario(operation_id)
    assert restored is not None
    assert restored.name == "Q3 emulation"

    with pytest.raises(PlanningConflictError):
        reopened.save_scenario(restored, expected_version=999, event=make_event(operation_id))


def test_test_case_update_conflict_and_listing(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = PlanningRepository(Database(database_path))
    case = PlannedTest(
        operation_id=operation_id,
        name="phish",
        technique_id="T1566",
        kill_chain_phase=KillChainPhase.DELIVERY,
    )
    repository.add_test_case(case, make_event(operation_id, "test-case.added"))

    case.mark_ready()
    repository.update_test_case(
        case, expected_version=1, event=make_event(operation_id, "test-case.ready")
    )
    restored = repository.get_test_case(case.id)
    assert restored is not None
    assert restored.status.value == "ready"

    with pytest.raises(PlanningConflictError):
        repository.update_test_case(
            restored, expected_version=1, event=make_event(operation_id)
        )
    assert [item.id for item in repository.list_test_cases(operation_id)] == [case.id]


def test_runbooks_are_immutable(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = PlanningRepository(Database(database_path))
    runbook = Runbook(
        operation_id=operation_id,
        format=ReportFormat.MARKDOWN,
        sha256="a" * 64,
        size_bytes=10,
        path=f"reports/{operation_id}/rb_x.md",
        test_case_ids=("tc_1",),
        stop_points=1,
    )
    repository.add_runbook(runbook, make_event(operation_id, "runbook.generated"))

    with repository.database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE runbooks SET sha256 = ? WHERE id = ?", ("b" * 64, runbook.id)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM runbooks WHERE id = ?", (runbook.id,))

    reopened = PlanningRepository(Database(database_path))
    assert reopened.get_runbook(runbook.id) is not None
    assert len(reopened.list_runbooks(operation_id)) == 1


def test_migration_12_applies(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    Database(database_path).initialize()
    with sqlite3.connect(database_path) as connection:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}
