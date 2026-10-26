import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.redteam import AttackStep, Checkpoint, Debrief, RoeDocument
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.redteam import RedTeamConflictError, RedTeamRepository


def make_operation(database_path: Path) -> str:
    service = OperationService(OperationRepository(Database(database_path)))
    operation = service.create(
        "Red",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("corp.local",)),
    )
    return operation.id


def make_event(operation_id: str, event_type: str = "test.event") -> OperationEvent:
    return OperationEvent(operation_id=operation_id, event_type=event_type, payload={})


def test_roe_survives_reopen_and_conflict_detection(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = RedTeamRepository(Database(database_path))
    roe = RoeDocument(
        operation_id=operation_id,
        threat_profile="APT29",
        objectives=("reach domain admin",),
    )
    repository.save_roe(roe, None, make_event(operation_id, "roe.set"))

    reopened = RedTeamRepository(Database(database_path))
    restored = reopened.get_roe(operation_id)
    assert restored is not None
    assert restored.threat_profile == "APT29"

    with pytest.raises(RedTeamConflictError):
        reopened.save_roe(restored, expected_version=999, event=make_event(operation_id))


def test_attack_step_update_conflict_and_listing(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = RedTeamRepository(Database(database_path))
    step = AttackStep(operation_id=operation_id, name="phish", technique_id="T1566")
    repository.add_step(step, make_event(operation_id, "attack-step.added"))

    step.begin_execution()
    repository.update_step(
        step, expected_version=1, event=make_event(operation_id, "attack-step.executing")
    )
    restored = repository.get_step(step.id)
    assert restored is not None
    assert restored.status.value == "executing"

    with pytest.raises(RedTeamConflictError):
        repository.update_step(restored, expected_version=1, event=make_event(operation_id))

    assert [item.id for item in repository.list_steps(operation_id)] == [step.id]


def test_checkpoint_update_conflict(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = RedTeamRepository(Database(database_path))
    checkpoint = Checkpoint(operation_id=operation_id, name="Before DA")
    repository.add_checkpoint(checkpoint, make_event(operation_id, "checkpoint.added"))

    checkpoint.reach("clear")
    repository.update_checkpoint(
        checkpoint, expected_version=1, event=make_event(operation_id, "checkpoint.reached")
    )
    restored = repository.get_checkpoint(checkpoint.id)
    assert restored is not None
    assert restored.status.value == "reached"

    with pytest.raises(RedTeamConflictError):
        repository.update_checkpoint(
            restored, expected_version=1, event=make_event(operation_id)
        )
    assert len(repository.list_checkpoints(operation_id)) == 1


def test_debriefs_are_immutable(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = RedTeamRepository(Database(database_path))
    debrief = Debrief(
        operation_id=operation_id,
        format=ReportFormat.MARKDOWN,
        sha256="a" * 64,
        size_bytes=10,
        path=f"reports/{operation_id}/deb_x.md",
        step_count=3,
        open_gaps=("lateral movement",),
    )
    repository.add_debrief(debrief, make_event(operation_id, "debrief.generated"))

    with repository.database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE debriefs SET sha256 = ? WHERE id = ?", ("b" * 64, debrief.id)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM debriefs WHERE id = ?", (debrief.id,))

    reopened = RedTeamRepository(Database(database_path))
    assert reopened.get_debrief(debrief.id) is not None
    assert len(reopened.list_debriefs(operation_id)) == 1


def test_migration_11_applies(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    Database(database_path).initialize()
    with sqlite3.connect(database_path) as connection:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
