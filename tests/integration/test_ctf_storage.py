import sqlite3
from pathlib import Path

import pytest

from samurai_ai.domain.ctf import (
    ChallengeCategory,
    CtfChallenge,
    CtfWriteup,
    FlagRecord,
    Hypothesis,
)
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.reports import ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.ctf import CtfConflictError, CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository


def make_operation(database_path: Path) -> str:
    service = OperationService(OperationRepository(Database(database_path)))
    operation = service.create(
        "Lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("10.10.10.0/24",)),
    )
    return operation.id


def make_event(operation_id: str, event_type: str = "test.event") -> OperationEvent:
    return OperationEvent(operation_id=operation_id, event_type=event_type, payload={})


def test_challenge_survives_reopen_and_conflict_detection(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = CtfRepository(Database(database_path))
    challenge = CtfChallenge(
        operation_id=operation_id,
        name="BabyRE",
        category=ChallengeCategory.REVERSE,
        flag_format=r"HTB\{.*\}",
    )
    repository.save_challenge(challenge, None, make_event(operation_id, "challenge.set"))

    reopened = CtfRepository(Database(database_path))
    restored = reopened.get_challenge(operation_id)
    assert restored is not None
    assert restored.name == "BabyRE"
    assert restored.category is ChallengeCategory.REVERSE

    with pytest.raises(CtfConflictError):
        reopened.save_challenge(restored, expected_version=999, event=make_event(operation_id))


def test_hypothesis_update_conflict_and_listing(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = CtfRepository(Database(database_path))
    hypothesis = Hypothesis(operation_id=operation_id, title="SQLi in login")
    repository.add_hypothesis(hypothesis, make_event(operation_id, "hypothesis.proposed"))

    hypothesis.begin_testing()
    repository.update_hypothesis(
        hypothesis, expected_version=1, event=make_event(operation_id, "hypothesis.testing")
    )
    restored = repository.get_hypothesis(hypothesis.id)
    assert restored is not None
    assert restored.status.value == "testing"

    with pytest.raises(CtfConflictError):
        repository.update_hypothesis(
            restored, expected_version=1, event=make_event(operation_id)
        )

    assert [item.id for item in repository.list_hypotheses(operation_id)] == [hypothesis.id]


def test_flag_ledger_is_immutable_and_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = CtfRepository(Database(database_path))
    record = FlagRecord(operation_id=operation_id, name="user", value="HTB{u53r}")
    repository.add_flag(record, make_event(operation_id, "flag.captured"))

    with pytest.raises(sqlite3.IntegrityError):
        duplicate = FlagRecord(operation_id=operation_id, name="user", value="HTB{0th3r}")
        repository.add_flag(duplicate, make_event(operation_id))

    with repository.database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE flag_ledger SET name = 'root' WHERE id = ?", (record.id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM flag_ledger WHERE id = ?", (record.id,))

    assert repository.get_flag(operation_id, "user") is not None
    assert len(repository.list_flags(operation_id)) == 1


def test_writeups_are_immutable(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    operation_id = make_operation(database_path)
    repository = CtfRepository(Database(database_path))
    writeup = CtfWriteup(
        operation_id=operation_id,
        format=ReportFormat.MARKDOWN,
        sha256="a" * 64,
        size_bytes=10,
        path=f"reports/{operation_id}/wr_x.md",
        flag_ids=("flag_1",),
        hypothesis_count=2,
    )
    repository.add_writeup(writeup, make_event(operation_id, "writeup.generated"))

    with repository.database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE ctf_writeups SET sha256 = ? WHERE id = ?", ("b" * 64, writeup.id)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM ctf_writeups WHERE id = ?", (writeup.id,))

    reopened = CtfRepository(Database(database_path))
    assert reopened.get_writeup(writeup.id) is not None
    assert len(reopened.list_writeups(operation_id)) == 1


def test_migration_10_applies_on_existing_v9_database(tmp_path: Path) -> None:
    database_path = tmp_path / "samurai.db"
    Database(database_path).initialize()
    with sqlite3.connect(database_path) as connection:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
