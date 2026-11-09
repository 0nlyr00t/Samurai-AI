"""Deleting an operation: the app's close means gone, not hidden."""

import pytest

from samurai_ai.domain.goal import Goal
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.goals import GoalRepository
from samurai_ai.storage.operations import OperationRepository


def make_service(tmp_path) -> tuple[OperationService, Database]:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "delete.db")
    return OperationService(OperationRepository(database)), database


def _count(database: Database, table: str, operation_id: str) -> int:
    with database.connect() as connection:
        return int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE operation_id = ?", (operation_id,)
            ).fetchone()[0]
        )


def test_delete_takes_events_goals_and_the_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The event log refuses DELETE by trigger; a delete has to step around it."""
    service, database = make_service(tmp_path)
    operation = service.create(
        "Doomed", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.5",))
    )
    goal = Goal(operation_id=operation.id, objective="own it")
    GoalRepository(database).add(
        goal,
        OperationEvent(operation_id=operation.id, event_type="goal.created", payload={}),
    )
    assert _count(database, "operation_events", operation.id) > 0

    service.delete(operation.id)

    assert service.repository.get(operation.id) is None
    assert _count(database, "operation_events", operation.id) == 0
    assert _count(database, "goals", operation.id) == 0


def test_the_log_stays_append_only_after_a_delete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, database = make_service(tmp_path)
    first = service.create("One", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.5",)))
    service.delete(first.id)
    second = service.create("Two", OperationMode.CTF_HTB, ScopeContract(includes=("10.0.0.6",)))

    with database.connect() as connection:
        with pytest.raises(Exception, match="append-only"):
            connection.execute(
                "DELETE FROM operation_events WHERE operation_id = ?", (second.id,)
            )
            connection.commit()


def test_deleting_a_missing_operation_says_so(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _database = make_service(tmp_path)
    with pytest.raises(OperationNotFoundError):
        service.delete("op_nope")
