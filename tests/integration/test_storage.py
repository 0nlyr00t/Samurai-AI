import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.operations import OperationEvent, OperationMode, OperationStatus
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationConflictError, OperationRepository


def make_service(database_path: Path) -> OperationService:
    return OperationService(OperationRepository(Database(database_path)))


def test_operation_and_events_survive_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    first = make_service(database_path)
    operation = first.create(
        "Bug bounty lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("*.example.com",), excludes=("api.example.com",)),
        objective="Validate one finding",
    )
    first.transition(operation.id, OperationStatus.PLANNING)

    second = make_service(database_path)
    restored = second.get(operation.id)
    assert restored.name == operation.name
    assert restored.status == OperationStatus.PLANNING
    assert restored.version == 2
    assert restored.scope.excludes == ("api.example.com",)
    assert [event.event_type for event in second.events(operation.id)] == [
        "operation.created",
        "operation.status.changed",
    ]
    assert second.latest().id == operation.id


def test_events_are_append_only_at_database_level(tmp_path: Path) -> None:
    database = Database(tmp_path / "samurai.db")
    service = OperationService(OperationRepository(database))
    operation = service.create(
        "Audit",
        OperationMode.OPERATION_PLAN,
        ScopeContract(includes=("example.com",)),
    )

    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE operation_events SET event_type = 'tampered' WHERE operation_id = ?",
                (operation.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM operation_events WHERE operation_id = ?",
                (operation.id,),
            )


def test_stale_operation_update_is_rejected(tmp_path: Path) -> None:
    repository = OperationRepository(Database(tmp_path / "samurai.db"))
    service = OperationService(repository)
    operation = service.create(
        "Concurrent",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("example.com",)),
    )

    with pytest.raises(OperationConflictError):
        repository.update(
            operation,
            expected_version=999,
            event=OperationEvent(
                operation_id=operation.id,
                event_type="operation.status.changed",
            ),
        )

    assert [event.event_type for event in service.events(operation.id)] == ["operation.created"]
